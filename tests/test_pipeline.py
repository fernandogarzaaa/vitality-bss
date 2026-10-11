"""Pipeline integration tests against a real (temporary) SQLite DB and blob store."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.conftest import eob_pdf, invoice_pdf
from vitality_bss import ai, pipeline
from vitality_bss.demo import make_pdf, seed_demo
from vitality_bss.models import Document, DuplicateUpload
from vitality_bss.pdftext import InvalidDocument
from vitality_bss.storage import BlobStore, generate_key


def test_safe_filename():
    assert pipeline.safe_filename("../../etc/passwd") == "passwd"
    assert pipeline.safe_filename("C:\\Users\\x\\scan.pdf") == "scan.pdf"
    assert pipeline.safe_filename('a<b>:"|?*.pdf') == "ab.pdf"
    assert pipeline.safe_filename("bad\x00name.pdf") == "badname.pdf"
    assert pipeline.safe_filename("") == "upload.pdf"
    assert pipeline.safe_filename(None) == "upload.pdf"
    assert pipeline.safe_filename("   ") == "upload.pdf"
    assert len(pipeline.safe_filename("a" * 400 + ".pdf")) == 255


def test_review_reasons():
    full = {"patient_name": "A B", "patient_id": "PT-1", "claim_number": "", "service_date": "2026-01-01",
            "billed_amount": 5.0, "insurer": ""}
    assert pipeline.review_reasons(full, "EOB", 0.9, 100) == []  # type: ignore[arg-type]
    assert pipeline.review_reasons(full, "EOB", 0.49, 100) == ["low_confidence"]  # type: ignore[arg-type]
    assert pipeline.review_reasons(full, "EOB", 0.5, 100) == []  # type: ignore[arg-type]
    empty = dict.fromkeys(full, "") | {"billed_amount": None}
    assert pipeline.review_reasons(empty, "Unclassified", 0.0, 0) == [  # type: ignore[arg-type]
        "no_text", "unclassified", "missing_patient_id", "missing_patient_name"]


def test_process_classifies_extracts_persists(session):
    res = pipeline.process_upload(session, "eob_PT-7777.pdf", eob_pdf())
    assert res.duplicate is False
    doc = res.document
    assert (doc.doc_type, doc.patient_id, doc.patient_name) == ("EOB", "PT-7777", "Test Person")
    assert (doc.claim_number, doc.service_date, doc.billed_amount, doc.insurer) == (
        "CLM-2026-0777", "2026-09-05", 500.0, "Humana")
    assert doc.status == "processed"
    assert doc.review_reasons == []
    assert doc.pages == 1
    assert doc.size_bytes == len(eob_pdf())
    assert doc.text_chars > 50
    assert doc.embedding and len(doc.embedding) == 512
    assert doc.encrypted is False
    assert doc.classifier == "rules"
    assert doc.classification_confidence == 1.0


def test_exact_duplicate_not_double_counted(session):
    pipeline.process_upload(session, "a.pdf", eob_pdf())
    res = pipeline.process_upload(session, "b.pdf", eob_pdf())
    assert res.duplicate is True
    assert res.duplicate_upload is not None
    assert res.duplicate_upload.document_id == res.document.id
    assert res.duplicate_upload.filename == "b.pdf"
    assert len(session.scalars(select(Document)).all()) == 1
    assert len(session.scalars(select(DuplicateUpload)).all()) == 1


def test_near_duplicate_flagged(session):
    first = pipeline.process_upload(session, "eob.pdf", eob_pdf()).document
    rescan = eob_pdf(extra=["Printed copy"])
    second = pipeline.process_upload(session, "eob_rescan.pdf", rescan).document
    assert second.id != first.id
    assert second.near_duplicate_of_id == first.id
    assert second.near_duplicate_score is not None and second.near_duplicate_score >= 0.97
    assert "possible_duplicate" in second.review_reasons
    assert second.status == "needs_review"


def test_near_duplicate_disabled(session, set_env):
    set_env(NEAR_DUPLICATE_THRESHOLD="0")
    pipeline.process_upload(session, "eob.pdf", eob_pdf())
    second = pipeline.process_upload(session, "eob2.pdf", eob_pdf(extra=["x"])).document
    assert second.near_duplicate_of_id is None


def test_different_documents_not_near_duplicates(session):
    pipeline.process_upload(session, "eob.pdf", eob_pdf())
    inv = pipeline.process_upload(session, "inv.pdf", invoice_pdf()).document
    assert inv.near_duplicate_of_id is None
    assert inv.status == "processed"


def test_missing_fields_need_review(session):
    pdf = make_pdf("INVOICE", ["Please remit payment", "Amount due soon"])
    doc = pipeline.process_upload(session, "invoice.pdf", pdf).document
    assert doc.doc_type == "Invoice"
    assert doc.status == "needs_review"
    assert {"missing_patient_id", "missing_patient_name", "missing_billed_amount"} <= set(doc.review_reasons)


def test_rejects_non_pdf_and_oversize(session, set_env):
    with pytest.raises(InvalidDocument):
        pipeline.process_upload(session, "note.txt", b"not a pdf")
    set_env(MAX_UPLOAD_MB="1")
    with pytest.raises(InvalidDocument, match="1 MB"):
        pipeline.process_upload(session, "big.pdf", b"%PDF-" + b"0" * (1024 * 1024))
    assert session.scalars(select(Document)).all() == []


def test_encryption_at_rest(session, env, set_env):
    key = generate_key()
    set_env(ENCRYPTION_KEYS=key)
    doc = pipeline.process_upload(session, "eob.pdf", eob_pdf()).document
    assert doc.encrypted is True
    raw = (env / "data" / "uploads" / f"{doc.storage_key}.bin").read_bytes()
    assert b"PT-7777" not in raw and not raw.startswith(b"%PDF")
    assert BlobStore(env / "data" / "uploads", [key]).get(doc.storage_key, True) == eob_pdf()


def test_mock_ai_mode(session, set_env):
    set_env(AI_MODE="mock")
    doc = pipeline.process_upload(session, "eob.pdf", eob_pdf()).document
    assert doc.classifier == "rules+mock"
    assert doc.classification_confidence == 1.0
    assert doc.patient_id == "PT-7777"


def test_ai_failure_falls_back_to_rules(session, monkeypatch):
    class Broken:
        name = "broken"

        def complete_json(self, system, user):  # type: ignore[no-untyped-def]
            raise ai.LLMError("down")

    monkeypatch.setattr(ai, "get_client", lambda: Broken())
    doc = pipeline.process_upload(session, "eob.pdf", eob_pdf()).document
    assert doc.patient_id == "PT-7777"
    assert "ai_unavailable" in doc.review_reasons
    assert doc.status == "needs_review"


def test_ai_not_called_without_text(session, monkeypatch, set_env):
    set_env(OCR_ENABLED="false")

    class Explode:
        name = "x"

        def complete_json(self, system, user):  # type: ignore[no-untyped-def]
            raise AssertionError("should not be called")

    monkeypatch.setattr(ai, "get_client", lambda: Explode())
    from pypdf import PdfWriter
    import io

    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    doc = pipeline.process_upload(session, "blank.pdf", buf.getvalue()).document
    assert {"no_text", "ocr_unavailable", "unclassified"} <= set(doc.review_reasons)


def test_reprocess_updates_fields(session, set_env):
    doc = pipeline.process_upload(session, "eob.pdf", eob_pdf()).document
    doc.patient_id = "WRONG"
    doc.status = "needs_review"
    session.commit()
    set_env(AI_MODE="mock")
    out = pipeline.reprocess(session, doc)
    assert out.patient_id == "PT-7777"
    assert out.status == "processed"
    assert out.classifier == "rules+mock"


def test_reprocess_keeps_duplicate_flag(session):
    first = pipeline.process_upload(session, "eob.pdf", eob_pdf()).document
    second = pipeline.process_upload(session, "eob2.pdf", eob_pdf(extra=["copy"])).document
    assert second.near_duplicate_of_id == first.id
    out = pipeline.reprocess(session, second)
    assert "possible_duplicate" in out.review_reasons


def test_analysis_summary_has_no_text():
    info = pipeline.analyze("eob.pdf", eob_pdf())
    summary = info.summary()
    assert "text" not in summary
    assert summary["doc_type"] == "EOB"
    assert summary["fields"]["patient_id"] == "PT-7777"  # type: ignore[index]


def test_seed_demo_is_idempotent(session):
    counts = seed_demo(session)
    assert counts == {"documents": 6, "duplicates": 1}
    assert seed_demo(session) == counts
