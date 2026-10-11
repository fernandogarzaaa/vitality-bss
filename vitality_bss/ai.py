"""Optional LLM enrichment through any OpenAI-compatible chat-completions API.

Works with hosted providers (Groq, OpenRouter, OpenAI, Together, ...) and local
servers (Ollama ``http://localhost:11434/v1``, llama.cpp, vLLM, LM Studio).

``AI_MODE=mock`` swaps in :class:`MockLLMClient`, a deterministic offline stand-in
that exercises the exact same merge/validation path without network access.

LLM output is never trusted blindly: string fields are accepted only when they
literally occur in the document text, amounts only when the number occurs, and the
document type only when it is one of the known labels.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from vitality_bss.classify import DOC_TYPES, UNCLASSIFIED, Classification, classify
from vitality_bss.extract import Fields, extract_fields, normalize_date, parse_amount
from vitality_bss.logging_setup import get_logger

log = get_logger("vitality_bss.ai")

SYSTEM_PROMPT = (
    "You extract structured data from healthcare billing documents. "
    "Respond with a single JSON object and nothing else, with keys: "
    '"doc_type" (one of ' + ", ".join(f'"{t}"' for t in DOC_TYPES) + ' or "Unclassified"), '
    '"patient_name", "patient_id", "claim_number", "service_date" (YYYY-MM-DD), '
    '"billed_amount" (number or null), "insurer". Use "" for unknown strings. '
    "Copy identifiers exactly as written. Never invent values."
)

#: Below this rule-confidence the LLM's document type is preferred.
LLM_TYPE_OVERRIDE_BELOW = 0.6
STRING_FIELDS: tuple[str, ...] = ("patient_name", "patient_id", "claim_number", "service_date", "insurer")
UPPERCASE_FIELDS = frozenset({"patient_id", "claim_number"})


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    name: str

    def complete_json(self, system: str, user: str) -> dict[str, Any]: ...


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating code fences / prose."""
    stripped = _FENCE_RE.sub("", content.strip())
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("model response was not JSON") from None
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("model response was not JSON") from exc
    if not isinstance(value, dict):
        raise LLMError("model response was not a JSON object")
    return value


class OpenAICompatibleClient:
    name = "llm"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code < 500 and resp.status_code != 429:
                    return resp
                last_exc = LLMError(f"provider returned HTTP {resp.status_code}")
            if attempt < self.max_retries:
                time.sleep(self.backoff_seconds * (2**attempt))
        raise LLMError(f"LLM request failed after {self.max_retries + 1} attempts") from last_exc

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        resp = self._post(body)
        if resp.status_code == 400:
            # Some OpenAI-compatible servers reject response_format; retry without it.
            body.pop("response_format")
            resp = self._post(body)
        if resp.status_code >= 400:
            raise LLMError(f"provider returned HTTP {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("unexpected provider response shape") from exc
        if not isinstance(content, str):
            raise LLMError("unexpected provider response shape")
        return parse_json_object(content)

    def close(self) -> None:
        self._client.close()


class MockLLMClient:
    """Deterministic offline LLM: answers from the rule engine, in LLM JSON shape."""

    name = "mock"

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        header, _, text = user.partition("\n---\n")
        filename = header.removeprefix("Filename: ").strip()
        cls = classify(filename, text)
        return {"doc_type": cls.doc_type, **extract_fields(text)}


@dataclass
class Enrichment:
    doc_type: str
    confidence: float
    classifier: str
    fields: Fields
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _amount_occurs(amount: float, text: str) -> bool:
    forms = {f"{amount:.2f}", f"{amount:,.2f}"}
    if amount == int(amount):
        forms |= {str(int(amount)), f"{int(amount):,}"}
    return any(f in text for f in forms)


def build_user_prompt(filename: str, text: str, max_chars: int) -> str:
    return f"Filename: {filename}\n---\n{text[:max_chars]}"


def enrich(
    client: LLMClient,
    filename: str,
    text: str,
    rules: Classification,
    fields: Fields,
    *,
    max_chars: int = 12000,
) -> Enrichment:
    """Merge LLM output with rule results. Rule values win when present; the LLM
    fills blanks only with values verifiably present in the text."""
    raw = client.complete_json(SYSTEM_PROMPT, build_user_prompt(filename, text, max_chars))
    values: dict[str, Any] = dict(fields)
    accepted: list[str] = []
    rejected: list[str] = []
    text_norm = _norm(text)

    for name in STRING_FIELDS:
        candidate = raw.get(name)
        if values[name] or not isinstance(candidate, str) or not candidate.strip():
            continue
        candidate = candidate.strip()
        if name == "service_date":
            candidate = normalize_date(candidate)
            ok = bool(candidate)
        else:
            ok = _norm(candidate) in text_norm
        if ok:
            values[name] = candidate.upper() if name in UPPERCASE_FIELDS else candidate
            accepted.append(name)
        else:
            rejected.append(name)

    raw_amount = raw.get("billed_amount")
    if values["billed_amount"] is None and raw_amount not in (None, ""):
        amount = parse_amount(raw_amount)
        if amount is not None and _amount_occurs(amount, text):
            values["billed_amount"] = amount
            accepted.append("billed_amount")
        else:
            rejected.append("billed_amount")

    doc_type, confidence, classifier = rules.doc_type, rules.confidence, "rules"
    llm_type = raw.get("doc_type")
    if isinstance(llm_type, str) and llm_type in (*DOC_TYPES, UNCLASSIFIED):
        weak = rules.doc_type == UNCLASSIFIED or rules.confidence < LLM_TYPE_OVERRIDE_BELOW
        if llm_type == rules.doc_type and llm_type != UNCLASSIFIED:
            classifier = f"rules+{client.name}"
            confidence = max(confidence, 0.9)
        elif weak and llm_type != UNCLASSIFIED:
            doc_type, classifier = llm_type, client.name
            confidence = max(rules.confidence, LLM_TYPE_OVERRIDE_BELOW)
    elif llm_type is not None:
        rejected.append("doc_type")
    merged = Fields(
        patient_name=values["patient_name"], patient_id=values["patient_id"],
        claim_number=values["claim_number"], service_date=values["service_date"],
        billed_amount=values["billed_amount"], insurer=values["insurer"],
    )
    return Enrichment(doc_type, round(confidence, 3), classifier, merged, accepted, rejected)


def get_client() -> LLMClient | None:
    from vitality_bss.config import get_settings

    s = get_settings()
    if s.AI_MODE == "mock":
        return MockLLMClient()
    if s.AI_MODE == "llm":
        return OpenAICompatibleClient(
            s.LLM_BASE_URL,
            s.LLM_API_KEY,
            s.LLM_MODEL,
            timeout=s.LLM_TIMEOUT_SECONDS,
            max_retries=s.LLM_MAX_RETRIES,
        )
    return None
