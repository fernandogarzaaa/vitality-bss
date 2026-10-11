"""PDF text extraction and OCR. The OCR test renders a synthetic image-only PDF."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from pypdf import PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from vitality_bss import pdftext
from vitality_bss.demo import make_pdf
from vitality_bss.pdftext import InvalidDocument, extract_text, open_pdf

needs_ocr = pytest.mark.skipif(not pdftext.ocr_available(), reason="tesseract/pdftoppm not installed")


def scanned_pdf(lines: list[str], tmp: Path) -> bytes:
    """A PDF whose only content is a raster image of text (like a scan)."""
    (tmp / "src.pdf").write_bytes(make_pdf("CLINICAL NOTE", lines, banner=False))
    subprocess.run(["pdftoppm", "-r", "200", "-png", "-singlefile", str(tmp / "src.pdf"), str(tmp / "img")],
                   capture_output=True, check=True)
    png = (tmp / "img.png").read_bytes()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawImage(ImageReader(io.BytesIO(png)), 0, 0, width=letter[0], height=letter[1])
    c.save()
    return buf.getvalue()


def test_extract_text_layer():
    pdf = make_pdf("TITLE", ["Patient ID: PT-1234", "Line two"])
    out = extract_text(pdf, ocr_enabled=False)
    assert out.pages == 1
    assert "PT-1234" in out.text
    assert out.ocr_pages == 0
    assert out.page_texts and "TITLE" in out.page_texts[0]
    assert out.ocr_available is False


def test_multi_page():
    pdf = make_pdf("T", [f"line {i}" for i in range(60)])
    out = extract_text(pdf, ocr_enabled=False)
    assert out.pages == 2
    assert "line 59" in out.text


@pytest.mark.parametrize("data", [b"not a pdf", b"", b"%PDX-1.4"])
def test_rejects_non_pdf(data):
    with pytest.raises(InvalidDocument, match="Only PDF"):
        open_pdf(data, 10)


def test_rejects_corrupt_pdf():
    with pytest.raises(InvalidDocument, match="not a readable PDF"):
        open_pdf(b"%PDF-1.4\n garbage garbage", 10)


def test_rejects_too_many_pages():
    pdf = make_pdf("T", [f"line {i}" for i in range(60)])
    with pytest.raises(InvalidDocument, match="limit is 1"):
        open_pdf(pdf, 1)
    open_pdf(pdf, 2)


def test_rejects_encrypted_pdf():
    w = PdfWriter()
    w.add_blank_page(100, 100)
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(InvalidDocument, match="Password-protected"):
        open_pdf(buf.getvalue(), 10)


def test_rejects_zero_pages():
    w = PdfWriter()
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(InvalidDocument):
        open_pdf(buf.getvalue(), 10)


def test_ocr_skipped_when_text_layer_sufficient(monkeypatch):
    monkeypatch.setattr(pdftext, "ocr_available", lambda: True)

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("OCR should not run")

    monkeypatch.setattr(pdftext, "ocr_page", boom)
    out = extract_text(make_pdf("TITLE", ["plenty of text on this page to skip ocr entirely"]), ocr_min_chars=10)
    assert out.ocr_pages == 0


def test_ocr_used_for_blank_page(monkeypatch):
    monkeypatch.setattr(pdftext, "ocr_available", lambda: True)
    monkeypatch.setattr(pdftext, "ocr_page", lambda data, n, **k: f"OCR page {n} Patient ID: PT-5555")
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    out = extract_text(buf.getvalue())
    assert out.ocr_pages == 1
    assert "PT-5555" in out.text


def test_ocr_failure_is_tolerated(monkeypatch):
    monkeypatch.setattr(pdftext, "ocr_available", lambda: True)

    def fail(*a, **k):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired("tesseract", 1)

    monkeypatch.setattr(pdftext, "ocr_page", fail)
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    out = extract_text(buf.getvalue())
    assert out.text == ""
    assert out.ocr_pages == 0


def test_ocr_result_not_used_if_shorter(monkeypatch):
    monkeypatch.setattr(pdftext, "ocr_available", lambda: True)
    monkeypatch.setattr(pdftext, "ocr_page", lambda *a, **k: "")
    out = extract_text(make_pdf("T", []), ocr_min_chars=1000)
    assert out.ocr_pages == 0
    assert "T" in out.text


def test_ocr_disabled_flag(monkeypatch):
    monkeypatch.setattr(pdftext, "ocr_available", lambda: True)
    out = extract_text(make_pdf("T", []), ocr_enabled=False)
    assert out.ocr_available is False


@needs_ocr
def test_real_ocr_on_scanned_pdf(tmp_path):
    pdf = scanned_pdf(["Patient Name: Olivia Bennett", "Patient ID: PT-4242", "Chief Complaint: Cough"], tmp_path)
    plain = extract_text(pdf, ocr_enabled=False)
    assert plain.text.strip() == ""
    out = extract_text(pdf, ocr_dpi=200)
    assert out.ocr_pages == 1
    assert "PT-4242" in out.text
    assert "Olivia" in out.text
