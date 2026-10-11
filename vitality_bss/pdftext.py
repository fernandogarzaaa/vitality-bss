"""PDF validation, text extraction and OCR fallback for scanned pages.

OCR uses the local ``pdftoppm`` (poppler-utils) and ``tesseract`` binaries, so no
document ever leaves the host for OCR. When either binary is missing, OCR is
skipped and the document is flagged for review if it has no text layer.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from vitality_bss.logging_setup import get_logger

log = get_logger("vitality_bss.pdftext")


class InvalidDocument(ValueError):
    """The upload is not a readable PDF (or violates limits)."""


@dataclass
class PdfText:
    text: str
    pages: int
    ocr_pages: int = 0
    page_texts: list[str] = field(default_factory=list)
    ocr_available: bool = True


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None and shutil.which("pdftoppm") is not None


def open_pdf(data: bytes, max_pages: int) -> PdfReader:
    if not data.startswith(b"%PDF-"):
        raise InvalidDocument("Only PDF files are supported")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise InvalidDocument("Password-protected PDFs are not supported")
        pages = len(reader.pages)
    except InvalidDocument:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise InvalidDocument("The file is not a readable PDF") from exc
    if pages == 0:
        raise InvalidDocument("The PDF has no pages")
    if pages > max_pages:
        raise InvalidDocument(f"The PDF has {pages} pages; the limit is {max_pages}")
    return reader


def ocr_page(data: bytes, page_number: int, *, lang: str, dpi: int, timeout: int) -> str:
    """OCR one 1-based page of a PDF with pdftoppm + tesseract."""
    with tempfile.TemporaryDirectory(prefix="vbss-ocr-") as tmp:
        tmpdir = Path(tmp)
        pdf_path = tmpdir / "in.pdf"
        pdf_path.write_bytes(data)
        prefix = tmpdir / "page"
        subprocess.run(
            [
                "pdftoppm",
                "-r",
                str(dpi),
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-png",
                "-singlefile",
                str(pdf_path),
                str(prefix),
            ],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        result = subprocess.run(
            ["tesseract", str(prefix) + ".png", "stdout", "-l", lang],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        return result.stdout.decode("utf-8", errors="replace")


def extract_text(
    data: bytes,
    *,
    max_pages: int = 500,
    ocr_enabled: bool = True,
    ocr_min_chars: int = 25,
    ocr_lang: str = "eng",
    ocr_dpi: int = 300,
    ocr_timeout: int = 120,
) -> PdfText:
    reader = open_pdf(data, max_pages)
    can_ocr = ocr_enabled and ocr_available()
    page_texts: list[str] = []
    ocr_count = 0
    for idx, page in enumerate(reader.pages, start=1):
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001 pypdf can raise many things on malformed content streams
            txt = ""
        if can_ocr and len(txt.strip()) < ocr_min_chars:
            try:
                ocr_txt = ocr_page(data, idx, lang=ocr_lang, dpi=ocr_dpi, timeout=ocr_timeout)
            except (subprocess.SubprocessError, OSError) as exc:
                log.warning("ocr failed for page", extra={"page": idx, "error": type(exc).__name__})
            else:
                if len(ocr_txt.strip()) > len(txt.strip()):
                    txt = ocr_txt
                    ocr_count += 1
        page_texts.append(txt)
    return PdfText(
        text="\n".join(page_texts),
        pages=len(page_texts),
        ocr_pages=ocr_count,
        page_texts=page_texts,
        ocr_available=can_ocr,
    )
