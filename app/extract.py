"""Field extraction from document text via regex. Pure functions."""

import re
from datetime import datetime

INSURERS = [
    "BlueCross BlueShield",
    "Blue Cross Blue Shield",
    "Aetna",
    "Cigna",
    "UnitedHealthcare",
    "United Healthcare",
    "Humana",
    "Kaiser Permanente",
    "Anthem",
    "Centene",
    "Molina",
]

_PATIENT_ID_RE = re.compile(r"\b(PT-\d{3,})\b")
_CLAIM_RE = re.compile(r"\b(CLM-\d{4}-\d+)\b")
_NAME_PATTERNS = [
    re.compile(
        r"(?:patient|member|subscriber)\s+name\s*[:\-]\s*"
        r"([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,3})",
        re.I,
    ),
    re.compile(
        r"(?:patient|member)\s*[:\-]\s*"
        r"([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,3})",
        re.I,
    ),
]
_DATE_PATTERNS = [
    re.compile(r"(?:service\s+date|date\s+of\s+service|dos)\s*[:\-]\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I),
    re.compile(r"(?:specimen\s+collected|date\s+of\s+visit)\s*[:\-]\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I),
]
_ANY_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})")
_AMOUNT_PATTERNS = [
    re.compile(r"billed\s+amount\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})", re.I),
    re.compile(r"total\s+(?:billed|due)\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})", re.I),
    re.compile(r"(?:amount|balance)\s+due\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})", re.I),
]
_ANY_AMOUNT_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")


def _first_group(patterns: list[re.Pattern], text: str) -> str:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return ""


def _normalize_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def _to_amount(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def extract_fields(text: str) -> dict:
    """Extract patient/claim/billing fields from raw document text.

    Returns dict with keys: patient_name, patient_id, claim_number,
    service_date (ISO or raw), billed_amount (float|None), insurer.
    """
    text = text or ""
    patient_id = _first_group([_PATIENT_ID_RE], text)
    claim_number = _first_group([_CLAIM_RE], text)
    patient_name = _first_group(_NAME_PATTERNS, text)

    service_date = _first_group(_DATE_PATTERNS, text)
    if not service_date:
        m = _ANY_DATE_RE.search(text)
        service_date = m.group(1) if m else ""
    service_date = _normalize_date(service_date) if service_date else ""

    amount_raw = _first_group(_AMOUNT_PATTERNS, text)
    if not amount_raw:
        m = _ANY_AMOUNT_RE.search(text)
        amount_raw = m.group(1) if m else ""
    billed_amount = _to_amount(amount_raw) if amount_raw else None

    insurer = ""
    lowered = text.lower()
    for name in INSURERS:
        if name.lower() in lowered:
            insurer = name
            break

    return {
        "patient_name": patient_name,
        "patient_id": patient_id,
        "claim_number": claim_number,
        "service_date": service_date,
        "billed_amount": billed_amount,
        "insurer": insurer,
    }
