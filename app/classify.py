"""Document classification: filename + content keyword heuristics.

Pure functions, no model calls. Returns one of the five known types.
"""

DOC_TYPES = ("EOB", "Clinical Note", "Lab Report", "Invoice", "Insurance Form")

#: Canonical order inside a patient binder.
BINDER_ORDER = {
    "EOB": 0,
    "Clinical Note": 1,
    "Lab Report": 2,
    "Invoice": 3,
    "Insurance Form": 4,
}

_KEYWORDS: dict[str, list[str]] = {
    "EOB": [
        "explanation of benefits",
        "claim number",
        "patient responsibility",
        "allowed amount",
        "benefit summary",
        "deductible",
        "coinsurance",
        "copay",
    ],
    "Clinical Note": [
        "clinical note",
        "chief complaint",
        "history of present illness",
        "assessment and plan",
        "soap note",
        "vital signs",
        "physical exam",
        "follow-up visit",
        "outpatient visit",
    ],
    "Lab Report": [
        "lab report",
        "laboratory",
        "test result",
        "reference range",
        "specimen",
        "pathology",
        "hemoglobin",
        "glucose",
        "cholesterol",
    ],
    "Invoice": [
        "invoice",
        "balance due",
        "amount due",
        "payment due",
        "billing statement",
        "please remit",
        "patient billing",
    ],
    "Insurance Form": [
        "enrollment",
        "prior authorization",
        "policy number",
        "subscriber",
        "group number",
        "beneficiary",
        "coverage start",
    ],
}

_FILENAME_HINTS: dict[str, list[str]] = {
    "EOB": ["eob", "benefit"],
    "Clinical Note": ["clinical", "note", "visit", "soap"],
    "Lab Report": ["lab", "pathology"],
    "Invoice": ["invoice", "bill", "statement"],
    "Insurance Form": ["insurance", "enrollment", "policy", "authorization"],
}


def classify_document(filename: str, text: str) -> str:
    """Classify a document into one of DOC_TYPES.

    Filename hints weigh 4x; content keyword hits weigh 1x each.
    Ties break toward the canonical binder order.
    """
    fname = (filename or "").lower()
    body = (text or "").lower()
    best = DOC_TYPES[0]
    best_score = -1
    for doc_type in DOC_TYPES:
        score = sum(4 for hint in _FILENAME_HINTS[doc_type] if hint in fname)
        score += sum(1 for kw in _KEYWORDS[doc_type] if kw in body)
        if score > best_score:
            best, best_score = doc_type, score
    return best
