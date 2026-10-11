"""Upload pipeline: validate -> hash -> exact dedupe -> text/OCR -> classify ->
extract -> (optional LLM enrichment) -> near-dup check -> encrypt + store -> persist."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vitality_bss import ai, dedupe
from vitality_bss.classify import UNCLASSIFIED, classify
from vitality_bss.config import Settings, get_settings
from vitality_bss.extract import Fields, extract_fields, missing_fields
from vitality_bss.logging_setup import get_logger
from vitality_bss.models import Document, DuplicateUpload, User
from vitality_bss.pdftext import InvalidDocument, extract_text
from vitality_bss.storage import BlobStore, get_store

log = get_logger("vitality_bss.pipeline")

LOW_CONFIDENCE = 0.5
MAX_FILENAME = 255


@dataclass
class UploadResult:
    document: Document
    duplicate: bool
    duplicate_upload: DuplicateUpload | None = None


@dataclass
class Analysis:
    text: str
    pages: int
    ocr_pages: int
    doc_type: str
    confidence: float
    classifier: str
    fields: Fields
    review_reasons: list[str]
    text_chars: int

    def summary(self) -> dict[str, object]:
        """JSON-friendly view without the raw text."""
        return {
            "pages": self.pages, "ocr_pages": self.ocr_pages, "doc_type": self.doc_type,
            "confidence": self.confidence, "classifier": self.classifier, "fields": dict(self.fields),
            "review_reasons": self.review_reasons, "text_chars": self.text_chars,
        }


def safe_filename(name: str | None) -> str:
    """Strip any path components and control characters from a client filename."""
    base = PurePath((name or "").replace("\\", "/")).name
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*').strip()
    return (cleaned or "upload.pdf")[:MAX_FILENAME]


def review_reasons(fields: Fields, doc_type: str, confidence: float, text_chars: int) -> list[str]:
    reasons: list[str] = []
    if text_chars == 0:
        reasons.append("no_text")
    if doc_type == UNCLASSIFIED:
        reasons.append("unclassified")
    elif confidence < LOW_CONFIDENCE:
        reasons.append("low_confidence")
    reasons += [f"missing_{f}" for f in missing_fields(fields, doc_type)]
    return reasons


def analyze(filename: str, data: bytes, settings: Settings | None = None) -> Analysis:
    """Run text extraction + classification + extraction without persisting."""
    s = settings or get_settings()
    pdf = extract_text(
        data,
        max_pages=s.MAX_PAGES_PER_DOCUMENT,
        ocr_enabled=s.OCR_ENABLED,
        ocr_min_chars=s.OCR_MIN_TEXT_CHARS,
        ocr_lang=s.OCR_LANG,
        ocr_dpi=s.OCR_DPI,
        ocr_timeout=s.OCR_TIMEOUT_SECONDS,
    )
    rules = classify(filename, pdf.text)
    fields = extract_fields(pdf.text)
    doc_type, confidence, classifier = rules.doc_type, rules.confidence, "rules"
    extra: list[str] = []
    client = ai.get_client()
    if client is not None and pdf.text.strip():
        try:
            result = ai.enrich(client, filename, pdf.text, rules, fields, max_chars=s.LLM_MAX_INPUT_CHARS)
        except ai.LLMError as exc:
            log.warning("llm enrichment failed; using rules", extra={"error": str(exc)})
            extra.append("ai_unavailable")
        else:
            doc_type, confidence, classifier, fields = (
                result.doc_type, result.confidence, result.classifier, result.fields,
            )
    text_chars = len(pdf.text.strip())
    if text_chars == 0 and not pdf.ocr_available:
        extra.append("ocr_unavailable")
    return Analysis(
        text=pdf.text, pages=pdf.pages, ocr_pages=pdf.ocr_pages, doc_type=doc_type, confidence=confidence,
        classifier=classifier, fields=fields,
        review_reasons=review_reasons(fields, doc_type, confidence, text_chars) + extra,
        text_chars=text_chars,
    )


def _record_duplicate(db: Session, existing: Document, filename: str, sha: str, user: User | None) -> UploadResult:
    dup = DuplicateUpload(filename=filename, sha256=sha, document_id=existing.id,
                          uploaded_by_id=user.id if user else None)
    db.add(dup)
    db.commit()
    log.info("exact duplicate upload", extra={"document_id": existing.id})
    return UploadResult(document=existing, duplicate=True, duplicate_upload=dup)


def process_upload(
    db: Session,
    filename: str,
    data: bytes,
    *,
    user: User | None = None,
    store: BlobStore | None = None,
    settings: Settings | None = None,
) -> UploadResult:
    """Process one uploaded PDF. Raises InvalidDocument for bad input."""
    s = settings or get_settings()
    filename = safe_filename(filename)
    if len(data) > s.max_upload_bytes:
        raise InvalidDocument(f"File exceeds the {s.MAX_UPLOAD_MB} MB limit")
    sha = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(Document).where(Document.sha256 == sha))
    if existing is not None:
        return _record_duplicate(db, existing, filename, sha, user)

    info = analyze(filename, data, s)
    fields = info.fields
    fp = dedupe.fingerprint(info.text)
    near = None
    if fp is not None:
        # Only compare within the same patient and document type: billing documents share
        # templates, so two patients' invoices can be textually near-identical.
        rows = db.execute(
            select(Document.id, Document.embedding).where(
                Document.embedding.is_not(None),
                Document.patient_id == fields["patient_id"],
                Document.doc_type == info.doc_type,
            )
        ).all()
        near = dedupe.best_match(fp, ((r[0], r[1]) for r in rows), s.NEAR_DUPLICATE_THRESHOLD)
    reasons = list(info.review_reasons)
    if near is not None:
        reasons.append("possible_duplicate")

    blob_store = store or get_store()
    key, encrypted = blob_store.put(data)
    doc = Document(
        filename=filename,
        sha256=sha,
        size_bytes=len(data),
        doc_type=info.doc_type,
        classification_confidence=info.confidence,
        classifier=info.classifier,
        patient_id=fields["patient_id"],
        patient_name=fields["patient_name"],
        claim_number=fields["claim_number"],
        service_date=fields["service_date"],
        billed_amount=fields["billed_amount"],
        insurer=fields["insurer"],
        status="needs_review" if reasons else "processed",
        review_reasons=reasons,
        pages=info.pages,
        text_chars=info.text_chars,
        ocr_pages=info.ocr_pages,
        embedding=fp,
        near_duplicate_of_id=near[0] if near else None,
        near_duplicate_score=round(near[1], 4) if near else None,
        storage_key=key,
        encrypted=encrypted,
        uploaded_by_id=user.id if user else None,
    )
    db.add(doc)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request stored the same bytes first: treat as duplicate.
        db.rollback()
        blob_store.delete(key)
        existing = db.scalar(select(Document).where(Document.sha256 == sha))
        if existing is None:  # pragma: no cover - defensive
            raise
        return _record_duplicate(db, existing, filename, sha, user)
    db.refresh(doc)
    log.info(
        "document processed",
        extra={"document_id": doc.id, "doc_type": doc.doc_type, "status": doc.status,
               "pages": doc.pages, "ocr_pages": doc.ocr_pages},
    )
    return UploadResult(document=doc, duplicate=False)


def reprocess(db: Session, doc: Document, *, store: BlobStore | None = None,
              settings: Settings | None = None) -> Document:
    """Re-run classification/extraction on a stored document (e.g. after enabling OCR/LLM)."""
    s = settings or get_settings()
    data = (store or get_store()).get(doc.storage_key, doc.encrypted)
    info = analyze(doc.filename, data, s)
    fields = info.fields
    doc.doc_type = info.doc_type
    doc.classification_confidence = info.confidence
    doc.classifier = info.classifier
    doc.patient_id = fields["patient_id"]
    doc.patient_name = fields["patient_name"]
    doc.claim_number = fields["claim_number"]
    doc.service_date = fields["service_date"]
    doc.insurer = fields["insurer"]
    doc.billed_amount = fields["billed_amount"]
    reasons = list(info.review_reasons)
    if doc.near_duplicate_of_id:
        reasons.append("possible_duplicate")
    doc.review_reasons = reasons
    doc.status = "needs_review" if reasons else "processed"
    doc.text_chars = info.text_chars
    doc.ocr_pages = info.ocr_pages
    doc.embedding = dedupe.fingerprint(info.text)
    db.commit()
    db.refresh(doc)
    return doc
