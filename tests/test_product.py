"""Vitality BSS product tests: classification, extraction, pipeline, API."""

from fastapi.testclient import TestClient

from app import models
from app.classify import BINDER_ORDER, DOC_TYPES, classify_document
from app.core.db import SessionLocal
from app.extract import extract_fields
from app.main import app
from app.product import process_upload
from app.product_seed import make_pdf

client = TestClient(app)


def _login():
    r = client.post(
        "/api/auth/login",
        json={"email": "admin@clusterx.local", "password": "ChangeMe123!"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------- classification ---


def test_doc_types_complete():
    assert set(DOC_TYPES) == {
        "EOB",
        "Clinical Note",
        "Lab Report",
        "Invoice",
        "Insurance Form",
    }


def test_classify_eob():
    text = "Explanation of Benefits. Claim Number: CLM-2026-0001. Patient responsibility $50."
    assert classify_document("scan.pdf", text) == "EOB"


def test_classify_clinical_note():
    text = "Chief complaint: cough. Vital signs stable. Assessment and plan: rest."
    assert classify_document("visit.pdf", text) == "Clinical Note"


def test_classify_lab_report():
    text = "Laboratory test results. Specimen adequate. Reference range shown."
    assert classify_document("results.pdf", text) == "Lab Report"


def test_classify_invoice():
    text = "Patient billing statement. Amount due $100. Please remit payment."
    assert classify_document("stmt.pdf", text) == "Invoice"


def test_classify_insurance_form():
    text = "Enrollment form. Policy number P-1. Group number G-2. Subscriber details."
    assert classify_document("form.pdf", text) == "Insurance Form"


def test_classify_filename_hint_wins():
    assert classify_document("eob_march.pdf", "random unrelated text") == "EOB"


def test_binder_order():
    assert [t for t in sorted(DOC_TYPES, key=lambda t: BINDER_ORDER[t])] == [
        "EOB",
        "Clinical Note",
        "Lab Report",
        "Invoice",
        "Insurance Form",
    ]


# --------------------------------------------------------------- extraction ---


def test_extract_fields():
    text = (
        "Patient Name: Jane Doe\nPatient ID: PT-9001\nClaim Number: CLM-2026-0099\n"
        "Service Date: 09/01/2026\nBilled Amount: $1,234.56\nInsurer: Cigna\n"
    )
    f = extract_fields(text)
    assert f["patient_name"] == "Jane Doe"
    assert f["patient_id"] == "PT-9001"
    assert f["claim_number"] == "CLM-2026-0099"
    assert f["service_date"] == "2026-09-01"
    assert f["billed_amount"] == 1234.56
    assert f["insurer"] == "Cigna"


def test_extract_fields_empty():
    f = extract_fields("nothing to see here")
    assert f["patient_id"] == ""
    assert f["billed_amount"] is None
    assert f["insurer"] == ""


# ----------------------------------------------------------------- pipeline ---


def _sample_pdf_bytes() -> bytes:
    # Cached: reportlab embeds a per-generation ID, so only identical bytes
    # dedupe (same as a real file re-uploaded twice).
    global _SAMPLE_CACHE
    try:
        return _SAMPLE_CACHE
    except NameError:
        _SAMPLE_CACHE = make_pdf(
            "EXPLANATION OF BENEFITS",
            [
                "Insurer: Humana",
                "Patient Name: Test Person",
                "Patient ID: PT-7777",
                "Claim Number: CLM-2026-0777",
                "Service Date: 09/05/2026",
                "Billed Amount: $500.00",
                "Patient Responsibility: $100.00",
            ],
        )
        return _SAMPLE_CACHE


def test_pipeline_classify_extract_persist():
    db = SessionLocal()
    try:
        res = process_upload(db, "eob_PT-7777.pdf", _sample_pdf_bytes())
        assert res["duplicate"] is False
        doc = res["document"]
        assert doc.doc_type == "EOB"
        assert doc.patient_id == "PT-7777"
        assert doc.patient_name == "Test Person"
        assert doc.claim_number == "CLM-2026-0777"
        assert doc.service_date == "2026-09-05"
        assert doc.billed_amount == 500.00
        assert doc.insurer == "Humana"
        assert doc.pages >= 1
        assert doc.is_duplicate is False
    finally:
        db.close()


def test_pipeline_duplicate_flagged_not_double_counted():
    db = SessionLocal()
    try:
        before_docs = db.query(models.Document).count()
        before_dups = db.query(models.DuplicateUpload).count()
        data = _sample_pdf_bytes()
        res = process_upload(db, "eob_PT-7777_again.pdf", data)
        assert res["duplicate"] is True
        assert db.query(models.Document).count() == before_docs
        assert db.query(models.DuplicateUpload).count() == before_dups + 1
    finally:
        db.close()


def test_pipeline_rejects_non_pdf():
    db = SessionLocal()
    try:
        try:
            process_upload(db, "note.txt", b"not a pdf")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------- API ---


def test_seed_produced_expected_counts():
    db = SessionLocal()
    try:
        assert db.query(models.Document).count() >= 7
        assert db.query(models.DuplicateUpload).count() >= 1
    finally:
        db.close()


def test_dashboard_renders():
    headers = _login()
    r = client.get("/", headers=headers)
    assert r.status_code == 200
    for needle in (
        "Total Documents",
        "Duplicates Removed",
        "Documents Processed",
        "Time Saved",
        "Processing Workflow",
        "Recent Documents",
        "Document Preview",
        "Extracted Information",
    ):
        assert needle in r.text, needle


def test_upload_api_and_binder_pdf():
    headers = _login()
    pdf = make_pdf(
        "PATIENT BILLING STATEMENT - INVOICE",
        [
            "Patient Name: Api Test",
            "Patient ID: PT-8888",
            "Service Date: 09/06/2026",
            "Billed Amount: $321.00",
            "Amount Due: $321.00",
        ],
    )
    r = client.post(
        "/api/documents/upload",
        headers=headers,
        files=[("files", ("invoice_PT-8888.pdf", pdf, "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    item = r.json()["uploaded"][0]
    assert item["duplicate"] is False
    assert item["doc_type"] == "Invoice"
    assert item["patient_id"] == "PT-8888"
    assert item["billed_amount"] == 321.00

    r = client.post(
        "/api/documents/upload",
        headers=headers,
        files=[("files", ("invoice_PT-8888_copy.pdf", pdf, "application/pdf"))],
    )
    assert r.json()["uploaded"][0]["duplicate"] is True

    r = client.post(
        "/api/binders/generate",
        headers=headers,
        json={"patient_id": "PT-8888"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


def test_document_detail_and_field_edit():
    headers = _login()
    docs = client.get("/api/documents", headers=headers).json()["documents"]
    doc_id = docs[0]["id"]

    r = client.get(f"/api/documents/{doc_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["id"] == doc_id

    r = client.post(
        f"/api/documents/{doc_id}/fields",
        headers=headers,
        json={"patient_name": "Edited Name"},
    )
    assert r.status_code == 200
    assert r.json()["document"]["patient_name"] == "Edited Name"

    # restore original value to keep other tests stable
    client.post(
        f"/api/documents/{doc_id}/fields",
        headers=headers,
        json={"patient_name": docs[0]["patient_name"]},
    )

    r = client.get(f"/documents/{doc_id}", headers=headers)
    assert r.status_code == 200
    assert "Edit Fields" in r.text


def test_binders_page_renders():
    headers = _login()
    r = client.get("/binders", headers=headers)
    assert r.status_code == 200
    assert "Patient Binders" in r.text
