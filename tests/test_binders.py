import io

from pypdf import PdfReader

from tests.conftest import eob_pdf, invoice_pdf
from vitality_bss import binders
from vitality_bss.demo import make_pdf
from vitality_bss.models import Document
from vitality_bss.pipeline import process_upload
from vitality_bss.storage import get_store


def _doc(i, doc_type, pid="PT-1", name="", date="", amount=None, status="processed"):  # type: ignore[no-untyped-def]
    return Document(
        id=i,
        doc_type=doc_type,
        patient_id=pid,
        patient_name=name,
        service_date=date,
        billed_amount=amount,
        status=status,
        filename=f"{i}.pdf",
    )


def test_group_and_order():
    docs = [
        _doc(1, "Invoice", amount=10.0),
        _doc(2, "EOB", name="Ana Cruz", amount=99.0),
        _doc(3, "Lab Report", pid=""),
        _doc(4, "Invoice", pid="PT-0", amount=5.0),
        _doc(5, "Clinical Note"),
        _doc(6, "Invoice", date="2026-01-01", amount=1.5),
    ]
    out = binders.group_documents(docs)
    assert list(out) == ["PT-0", "PT-1", binders.UNASSIGNED]
    b = out["PT-1"]
    assert [d.id for d in b.documents] == [2, 5, 6, 1]
    assert b.patient_name == "Ana Cruz"
    assert b.total_billed == 11.5  # invoices only
    assert b.ready is True
    assert out[binders.UNASSIGNED].ready is False


def test_ready_requires_all_processed():
    b = binders.group_documents([_doc(1, "EOB"), _doc(2, "Invoice", status="needs_review")])["PT-1"]
    assert b.ready is False


def test_sort_key_unknown_type_last():
    assert binders.sort_key(_doc(1, "Weird")) > binders.sort_key(_doc(2, "Unclassified"))


def test_cover_page_count():
    assert binders.cover_page_count(0) == 1
    assert binders.cover_page_count(30) == 1
    assert binders.cover_page_count(31) == 2
    assert binders.cover_page_count(61) == 3


def test_render_binder(session):
    process_upload(session, "inv.pdf", invoice_pdf("PT-7777"))
    process_upload(session, "eob.pdf", eob_pdf())
    process_upload(session, "lab.pdf", make_pdf("LAB REPORT", ["Patient ID: PT-7777", "Specimen ok"] * 40))
    b = binders.load_binders(session)["PT-7777"]
    assert [d.doc_type for d in b.documents] == ["EOB", "Lab Report", "Invoice"]
    pdf = binders.render_binder(b, get_store(), "tester@example.test")
    reader = PdfReader(io.BytesIO(pdf))
    lab_pages = 3
    assert len(reader.pages) == 1 + 1 + lab_pages + 1
    cover = reader.pages[0].extract_text()
    assert "Patient Billing Binder" in cover
    assert "PT-7777" in cover and "Test Person" in cover
    assert "tester@example.test" in cover
    assert "$321.00" in cover
    assert "CONFIDENTIAL" in cover
    titles = [o.title for o in reader.outline]
    assert titles == ["Cover & contents", "EOB: eob.pdf", "Lab Report: lab.pdf", "Invoice: inv.pdf"]
    assert [reader.get_destination_page_number(o) for o in reader.outline] == [0, 1, 2, 5]
    assert reader.metadata is not None and reader.metadata.title == "Billing binder PT-7777"


def test_binder_toc_spans_pages(session):
    docs = [_doc(i, "Invoice") for i in range(1, 32)]
    b = binders.Binder("PT-1", "X Y", docs)
    cover = binders._cover_pdf(b, list(range(3, 34)), "me")
    assert len(PdfReader(io.BytesIO(cover)).pages) == 2


def test_load_binders_excludes_near_duplicates(session):
    process_upload(session, "eob.pdf", eob_pdf())
    process_upload(session, "eob2.pdf", eob_pdf(extra=["copy"]))
    b = binders.load_binders(session)["PT-7777"]
    assert len(b.documents) == 1


def test_render_empty_binder_rejected():
    import pytest

    with pytest.raises(ValueError):
        binders.render_binder(binders.Binder("PT-1", "", []), None, "x")  # type: ignore[arg-type]
