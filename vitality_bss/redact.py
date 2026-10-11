"""PHI redaction for anything that leaves the process as text (logs, audit details).

This is defence in depth: the application never logs document text on purpose.
Redaction catches identifiers that slip into messages anyway (exception strings,
query parameters, filenames).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Labeled names: "Patient Name: Jane Doe" -> "Patient Name: [REDACTED]"
    (
        re.compile(
            r"(?i)\b((?:patient|member|subscriber|insured|guarantor)\s*(?:full\s+)?name\s*[:=]\s*)"
            r"[^\n,;|]+"
        ),
        r"\1" + REDACTED,
    ),
    # Labeled date of birth
    (re.compile(r"(?i)\b((?:dob|date\s+of\s+birth|birth\s*date)\s*[:=]\s*)[^\n,;|]+"), r"\1" + REDACTED),
    # US SSN
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), REDACTED),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), REDACTED),
    # Phone numbers (NANP-ish and international with separators)
    (re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"), REDACTED),
    # Medical record / patient / member / claim identifiers
    (re.compile(r"\bPT-\d{3,}\b"), REDACTED),
    (re.compile(r"\bCLM-\d{4}-\d+\b"), REDACTED),
    (
        re.compile(
            r"(?i)\b((?:mrn|patient\s*id|member\s*id|policy\s*(?:number|no\.?|#)|claim\s*(?:number|no\.?|#))"
            r"\s*[:=#]?\s*)[A-Za-z0-9-]{3,}"
        ),
        r"\1" + REDACTED,
    ),
]


def redact(text: str) -> str:
    """Return ``text`` with known PHI patterns replaced by ``[REDACTED]``."""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside dicts/lists/tuples; other values pass through."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value
