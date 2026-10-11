"""Rule-based document classification (deterministic, no model calls)."""

from __future__ import annotations

from dataclasses import dataclass

UNCLASSIFIED = "Unclassified"
DOC_TYPES: tuple[str, ...] = ("EOB", "Clinical Note", "Lab Report", "Invoice", "Insurance Form")
ALL_TYPES: tuple[str, ...] = (*DOC_TYPES, UNCLASSIFIED)

#: Canonical order inside a patient binder.
BINDER_ORDER: dict[str, int] = {t: i for i, t in enumerate(ALL_TYPES)}

KEYWORDS: dict[str, tuple[str, ...]] = {
    "EOB": (
        "explanation of benefits", "claim number", "patient responsibility", "allowed amount",
        "benefit summary", "deductible", "coinsurance", "copay", "this is not a bill",
    ),
    "Clinical Note": (
        "clinical note", "chief complaint", "history of present illness", "assessment and plan",
        "soap note", "vital signs", "physical exam", "follow-up visit", "outpatient visit",
        "progress note",
    ),
    "Lab Report": (
        "lab report", "laboratory", "test result", "reference range", "specimen", "pathology",
        "hemoglobin", "glucose", "cholesterol",
    ),
    "Invoice": (
        "invoice", "balance due", "amount due", "payment due", "billing statement", "please remit",
        "patient billing",
    ),
    "Insurance Form": (
        "enrollment", "prior authorization", "policy number", "subscriber", "group number",
        "beneficiary", "coverage start",
    ),
}

FILENAME_HINTS: dict[str, tuple[str, ...]] = {
    "EOB": ("eob", "benefit"),
    "Clinical Note": ("clinical", "note", "visit", "soap"),
    "Lab Report": ("lab", "pathology"),
    "Invoice": ("invoice", "bill", "statement"),
    "Insurance Form": ("insurance", "enrollment", "policy", "authorization"),
}

FILENAME_WEIGHT = 4


@dataclass(frozen=True)
class Classification:
    doc_type: str
    confidence: float
    scores: dict[str, int]


def score_document(filename: str, text: str) -> dict[str, int]:
    fname = (filename or "").lower()
    body = (text or "").lower()
    scores: dict[str, int] = {}
    for doc_type in DOC_TYPES:
        score = sum(FILENAME_WEIGHT for hint in FILENAME_HINTS[doc_type] if hint in fname)
        score += sum(1 for kw in KEYWORDS[doc_type] if kw in body)
        scores[doc_type] = score
    return scores


def classify(filename: str, text: str) -> Classification:
    """Classify into DOC_TYPES, or UNCLASSIFIED when nothing matches.

    Filename hints weigh 4x; each content keyword hit weighs 1x. Ties break toward
    the canonical binder order. Confidence is the winner's share of all points.
    """
    scores = score_document(filename, text)
    best = UNCLASSIFIED
    best_score = 0
    for doc_type in DOC_TYPES:
        if scores[doc_type] > best_score:
            best, best_score = doc_type, scores[doc_type]
    total = sum(scores.values())
    confidence = round(best_score / total, 3) if total else 0.0
    return Classification(doc_type=best, confidence=confidence, scores=scores)


def classify_document(filename: str, text: str) -> str:
    """Backward-compatible helper returning only the type label."""
    return classify(filename, text).doc_type
