"""LLM layer tests. Network is never used: an httpx.MockTransport plays the provider."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from vitality_bss import ai
from vitality_bss.classify import UNCLASSIFIED, Classification, classify
from vitality_bss.extract import Fields, extract_fields

TEXT = (
    "Patient Name: Jane Doe\nPatient ID: PT-9001\nClaim Number: CLM-2026-0099\n"
    "Service Date: 09/01/2026\nBilled Amount: $1,234.56\nInsurer: Cigna\nExplanation of benefits"
)


def _reply(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


def _client(handler: Any, retries: int = 0) -> ai.OpenAICompatibleClient:
    return ai.OpenAICompatibleClient(
        "http://llm.local/v1/", "k", "m", max_retries=retries, transport=httpx.MockTransport(handler), backoff_seconds=0
    )


class FakeLLM:
    name = "fake"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        return self.payload


def _empty_fields() -> Fields:
    return Fields(patient_name="", patient_id="", claim_number="", service_date="", billed_amount=None, insurer="")


# ------------------------------------------------------------ parse json ---


def test_parse_json_variants():
    assert ai.parse_json_object('{"a": 1}') == {"a": 1}
    assert ai.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai.parse_json_object('Sure! {"a": 1} hope that helps') == {"a": 1}
    for bad in ("no json here", "[1, 2]", "{broken", "prefix {not: json} suffix"):
        with pytest.raises(ai.LLMError):
            ai.parse_json_object(bad)


# -------------------------------------------------------------- http client ---


def test_client_success_request_shape():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _reply('{"doc_type": "EOB"}')

    out = _client(handler).complete_json("sys", "usr")
    assert out == {"doc_type": "EOB"}
    assert seen["url"] == "http://llm.local/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "m"
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]


def test_client_without_api_key_sends_no_auth_header():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _reply("{}")

    c = ai.OpenAICompatibleClient("http://localhost:11434/v1", "", "llama3", transport=httpx.MockTransport(handler))
    c.complete_json("s", "u")
    c.close()
    assert seen["auth"] is None


def test_client_retries_without_response_format_on_400():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "unsupported"})
        return _reply('{"ok": true}')

    assert _client(handler).complete_json("s", "u") == {"ok": True}
    assert len(bodies) == 2
    assert "response_format" not in bodies[1]


def test_client_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else _reply("{}")

    assert _client(handler, retries=2).complete_json("s", "u") == {}
    assert calls["n"] == 3


def test_client_gives_up_after_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429)

    with pytest.raises(ai.LLMError, match="after 2 attempts"):
        _client(handler, retries=1).complete_json("s", "u")
    assert calls["n"] == 2


def test_client_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(ai.LLMError):
        _client(handler).complete_json("s", "u")


@pytest.mark.parametrize("status", [401, 404])
def test_client_4xx_is_error(status):
    with pytest.raises(ai.LLMError, match=str(status)):
        _client(lambda r: httpx.Response(status)).complete_json("s", "u")


def test_client_persistent_400_is_error():
    with pytest.raises(ai.LLMError, match="400"):
        _client(lambda r: httpx.Response(400)).complete_json("s", "u")


@pytest.mark.parametrize(
    "payload",
    [{"choices": []}, {"nope": 1}, {"choices": [{"message": {"content": None}}]}, "notjson"],
)
def test_client_bad_shapes(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(200, content=payload)
        return httpx.Response(200, json=payload)

    with pytest.raises(ai.LLMError):
        _client(handler).complete_json("s", "u")


# ------------------------------------------------------------------ enrich ---


def test_prompt_contains_filename_and_truncated_text():
    p = ai.build_user_prompt("a.pdf", "x" * 50, 10)
    assert p == "Filename: a.pdf\n---\n" + "x" * 10


def test_enrich_rules_win_when_present():
    rules = classify("eob.pdf", TEXT)
    fields = extract_fields(TEXT)
    llm = FakeLLM({"doc_type": "Invoice", "patient_name": "Someone Else", "billed_amount": 1})
    out = ai.enrich(llm, "eob.pdf", TEXT, rules, fields)
    assert out.fields == fields
    assert out.doc_type == "EOB"
    assert out.classifier == "rules"
    assert out.accepted == []
    assert llm.calls[0][0] == ai.SYSTEM_PROMPT


def test_enrich_fills_blanks_only_with_values_in_text():
    text = "Name on file is Jane  Doe. Ref X-77. Charge 1,234.56 Aetna on 2026-09-01"
    rules = Classification(UNCLASSIFIED, 0.0, {})
    llm = FakeLLM(
        {
            "doc_type": "Invoice",
            "patient_name": "jane doe",
            "patient_id": "x-77",
            "claim_number": "INVENTED",
            "service_date": "09/01/2026",
            "billed_amount": "1234.56",
            "insurer": "Aetna",
        }
    )
    out = ai.enrich(llm, "scan.pdf", text, rules, _empty_fields())
    assert out.fields["patient_name"] == "jane doe"
    assert out.fields["patient_id"] == "X-77"
    assert out.fields["claim_number"] == ""
    assert out.fields["service_date"] == "2026-09-01"
    assert out.fields["billed_amount"] == 1234.56
    assert out.fields["insurer"] == "Aetna"
    assert "claim_number" in out.rejected
    assert set(out.accepted) == {"patient_name", "patient_id", "service_date", "billed_amount", "insurer"}
    assert out.doc_type == "Invoice"
    assert out.classifier == "fake"
    assert out.confidence == ai.LLM_TYPE_OVERRIDE_BELOW


def test_enrich_rejects_hallucinated_amount_and_bad_date():
    llm = FakeLLM({"billed_amount": 999.99, "service_date": "not a date", "patient_name": 5})
    out = ai.enrich(llm, "x.pdf", "charge 10.00", Classification(UNCLASSIFIED, 0.0, {}), _empty_fields())
    assert out.fields["billed_amount"] is None
    assert out.fields["service_date"] == ""
    assert {"billed_amount", "service_date"} <= set(out.rejected)
    assert "patient_name" not in out.rejected


def test_enrich_integer_amount_forms():
    llm = FakeLLM({"billed_amount": 1250})
    out = ai.enrich(llm, "x.pdf", "total 1,250 owed", Classification(UNCLASSIFIED, 0.0, {}), _empty_fields())
    assert out.fields["billed_amount"] == 1250.0


def test_enrich_unknown_doc_type_rejected():
    out = ai.enrich(
        FakeLLM({"doc_type": "Recipe"}), "x.pdf", "t", Classification(UNCLASSIFIED, 0.0, {}), _empty_fields()
    )
    assert out.doc_type == UNCLASSIFIED
    assert "doc_type" in out.rejected


def test_enrich_llm_unclassified_does_not_override():
    rules = Classification("Invoice", 0.4, {})
    out = ai.enrich(FakeLLM({"doc_type": UNCLASSIFIED}), "x.pdf", "t", rules, _empty_fields())
    assert out.doc_type == "Invoice"
    assert out.classifier == "rules"
    assert out.confidence == 0.4


def test_enrich_agreement_boosts_confidence():
    rules = Classification("Invoice", 0.4, {})
    out = ai.enrich(FakeLLM({"doc_type": "Invoice"}), "x.pdf", "t", rules, _empty_fields())
    assert out.classifier == "rules+fake"
    assert out.confidence == 0.9


def test_enrich_confident_rules_not_overridden():
    rules = Classification("Invoice", 0.8, {})
    out = ai.enrich(FakeLLM({"doc_type": "EOB"}), "x.pdf", "t", rules, _empty_fields())
    assert out.doc_type == "Invoice"
    assert out.confidence == 0.8


def test_enrich_low_confidence_rules_overridden():
    rules = Classification("Invoice", 0.59, {})
    out = ai.enrich(FakeLLM({"doc_type": "EOB"}), "x.pdf", "t", rules, _empty_fields())
    assert out.doc_type == "EOB"
    assert out.confidence == 0.6


def test_mock_client_matches_rules():
    out = ai.MockLLMClient().complete_json(ai.SYSTEM_PROMPT, ai.build_user_prompt("eob.pdf", TEXT, 10000))
    assert out["doc_type"] == "EOB"
    assert out["patient_id"] == "PT-9001"


def test_get_client_modes(set_env):
    set_env(AI_MODE="off")
    assert ai.get_client() is None
    set_env(AI_MODE="mock")
    assert isinstance(ai.get_client(), ai.MockLLMClient)
    set_env(AI_MODE="llm", LLM_BASE_URL="http://localhost:11434/v1", LLM_MODEL="llama3", LLM_MAX_RETRIES="3")
    c = ai.get_client()
    assert isinstance(c, ai.OpenAICompatibleClient)
    assert c.model == "llama3"
    assert c.max_retries == 3
    assert c.base_url == "http://localhost:11434/v1"
    set_env(AI_MODE="off")


def test_client_retries_on_500_exactly():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 1 else _reply('{"x": 1}')

    assert _client(handler, retries=1).complete_json("s", "u") == {"x": 1}
    assert calls["n"] == 2


def test_client_499_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(499)

    with pytest.raises(ai.LLMError, match="499"):
        _client(handler, retries=3).complete_json("s", "u")
    assert calls["n"] == 1


def test_client_backoff_schedule(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(ai.time, "sleep", sleeps.append)
    client = ai.OpenAICompatibleClient(
        "http://x/v1", "", "m", max_retries=2, transport=httpx.MockTransport(lambda r: httpx.Response(503))
    )
    with pytest.raises(ai.LLMError, match="after 3 attempts"):
        client.complete_json("s", "u")
    assert sleeps == [0.5, 1.0]


def test_client_default_settings():
    c = ai.OpenAICompatibleClient("http://x/v1/", "", "m")
    assert c.max_retries == 2
    assert c.backoff_seconds == 0.5
    assert c.base_url == "http://x/v1"
    assert c._client.timeout.read == 30.0


def test_enrich_threshold_boundary_not_overridden():
    rules = Classification("Invoice", 0.6, {})
    out = ai.enrich(FakeLLM({"doc_type": "EOB"}), "x.pdf", "t", rules, _empty_fields())
    assert out.doc_type == "Invoice"
    assert out.classifier == "rules"


def test_parse_json_embedded_multiline():
    assert ai.parse_json_object('Result:\n{"a":\n 1}\nDone') == {"a": 1}


def test_enrich_default_truncation_and_rounding():
    llm = FakeLLM({})
    out = ai.enrich(llm, "x.pdf", "y" * 13000, Classification("Invoice", 0.123456, {}), _empty_fields())
    sent = llm.calls[0][1].split("\n---\n", 1)[1]
    assert len(sent) == 12000
    assert out.confidence == 0.123


def test_network_error_is_chained_cause():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(ai.LLMError) as exc:
        _client(handler).complete_json("s", "u")
    assert isinstance(exc.value.__cause__, httpx.ConnectError)


def test_close_closes_http_client():
    c = _client(lambda r: _reply("{}"))
    assert c._client.is_closed is False
    c.close()
    assert c._client.is_closed is True
