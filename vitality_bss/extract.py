"""Deterministic field extraction from document text (regex based)."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import TypedDict

INSURERS: tuple[str, ...] = (
    "BlueCross BlueShield", "Blue Cross Blue Shield", "Aetna", "Cigna", "UnitedHealthcare",
    "United Healthcare", "Humana", "Kaiser Permanente", "Anthem", "Centene", "Molina",
    "Medicare", "Medicaid", "Tricare", "PhilHealth",
)

FIELD_NAMES: tuple[str, ...] = (
    "patient_name", "patient_id", "claim_number", "service_date", "billed_amount", "insurer",
)


class Fields(TypedDict):
    patient_name: str
    patient_id: str
    claim_number: str
    service_date: str
    billed_amount: float | None
    insurer: str


_PATIENT_ID_PATTERNS = [
    re.compile(r"\b(PT-\d{3,})\b"),
    re.compile(
        r"(?i)\b(?:patient\s*id|mrn|medical\s+record\s+(?:number|no\.?|#)|member\s*id|account\s*(?:number|no\.?|#))"
        r"\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-]{2,31})\b"
    ),
]
_CLAIM_PATTERNS = [
    re.compile(r"\b(CLM-\d{4}-\d+)\b"),
    re.compile(r"(?i)\bclaim\s*(?:number|no\.?|#|id)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-]{3,31})\b"),
]
_NAME_PATTERNS = [
    re.compile(
        r"(?:patient|member|subscriber|insured)\s+name\s*[:\-]\s*"
        r"([A-Z][A-Za-z.'\-]+(?:[ \t]+[A-Z][A-Za-z.'\-]+){1,3})",
        re.I,
    ),
    re.compile(
        r"(?:patient|member)\s*[:\-]\s*([A-Z][A-Za-z.'\-]+(?:[ \t]+[A-Z][A-Za-z.'\-]+){1,3})",
        re.I,
    ),
]
_DATE = r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})"
_DATE_PATTERNS = [
    re.compile(r"(?i)(?:service\s+date|date\s+of\s+service|dos)\s*[:\-]\s*" + _DATE),
    re.compile(r"(?i)(?:specimen\s+collected|date\s+of\s+visit|visit\s+date)\s*[:\-]\s*" + _DATE),
]
_ANY_DATE_RE = re.compile(_DATE)
_DOB_RE = re.compile(r"(?i)(?:dob|date\s+of\s+birth|birth\s*date)\s*[:\-]\s*" + _DATE)
_AMOUNT_PATTERNS = [
    re.compile(r"(?i)billed\s+amount\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})"),
    re.compile(r"(?i)total\s+(?:billed|charges|due)\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})"),
    re.compile(r"(?i)(?:amount|balance)\s+due\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})"),
]
_ANY_AMOUNT_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")
_NAME_STOPWORDS = {"id", "number", "dob", "date"}


def _first_group(patterns: list[re.Pattern[str]], text: str) -> str:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return ""


def normalize_date(raw: str) -> str:
    """Return an ISO date for m/d/Y, m/d/y or Y-m-d input; '' when invalid."""
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_amount(raw: str | float | int | None) -> float | None:
    """Parse '$1,234.50' / '1234.5' / 12 into a non-negative float rounded to cents."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        cleaned = raw.replace("$", "").replace(",", "").strip()
        if not cleaned:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(value, 2)


def _clean_name(raw: str) -> str:
    words = raw.split()
    while words and words[-1].lower().strip(".") in _NAME_STOPWORDS:
        words.pop()
    return " ".join(words) if len(words) >= 2 else ""


def _service_date(text: str) -> str:
    labeled = _first_group(_DATE_PATTERNS, text)
    if labeled:
        return normalize_date(labeled)
    dob = _DOB_RE.search(text)
    dob_value = dob.group(1) if dob else None
    for m in _ANY_DATE_RE.finditer(text):
        if m.group(1) != dob_value:
            return normalize_date(m.group(1))
    return ""


def extract_fields(text: str) -> Fields:
    """Extract patient/claim/billing fields from raw document text."""
    text = text or ""
    amount_raw = _first_group(_AMOUNT_PATTERNS, text)
    if not amount_raw:
        m = _ANY_AMOUNT_RE.search(text)
        amount_raw = m.group(1) if m else ""
    lowered = text.lower()
    insurer = next((name for name in INSURERS if name.lower() in lowered), "")
    return Fields(
        patient_name=_clean_name(_first_group(_NAME_PATTERNS, text)),
        patient_id=_first_group(_PATIENT_ID_PATTERNS, text).upper(),
        claim_number=_first_group(_CLAIM_PATTERNS, text).upper(),
        service_date=_service_date(text),
        billed_amount=parse_amount(amount_raw),
        insurer=insurer,
    )


def missing_fields(fields: Fields, doc_type: str) -> list[str]:
    """Fields a reviewer should fill in before the document is binder-ready."""
    required = ["patient_id", "patient_name"]
    if doc_type in ("EOB", "Invoice"):
        required += ["service_date", "billed_amount"]
    values: dict[str, object] = dict(fields)
    return [f for f in required if values[f] in ("", None)]


def is_valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True
