"""Patient billing binders: group documents per patient and render a merged PDF
with a cover page, table of contents, totals and PDF bookmarks."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import Session

from vitality_bss.classify import BINDER_ORDER
from vitality_bss.models import Document
from vitality_bss.storage import BlobStore

UNASSIGNED = "(unassigned)"
TOC_ROWS_PER_PAGE = 30
COLUMNS = (72, 100, 230, 340, 450)


@dataclass
class Binder:
    patient_id: str
    patient_name: str
    documents: list[Document]

    @property
    def total_billed(self) -> float:
        return float(round(sum(d.billed_amount or 0.0 for d in self.documents if d.doc_type == "Invoice"), 2))

    @property
    def ready(self) -> bool:
        return self.patient_id != UNASSIGNED and all(d.status == "processed" for d in self.documents)


def sort_key(doc: Document) -> tuple[int, str, int]:
    return (BINDER_ORDER.get(doc.doc_type, len(BINDER_ORDER)), doc.service_date or "9999", doc.id or 0)


def group_documents(docs: list[Document]) -> dict[str, Binder]:
    groups: dict[str, list[Document]] = {}
    for doc in docs:
        groups.setdefault(doc.patient_id or UNASSIGNED, []).append(doc)
    binders: dict[str, Binder] = {}
    for pid in sorted(groups, key=lambda p: (p == UNASSIGNED, p)):
        members = sorted(groups[pid], key=sort_key)
        name = next((d.patient_name for d in members if d.patient_name), "")
        binders[pid] = Binder(patient_id=pid, patient_name=name, documents=members)
    return binders


def load_binders(db: Session) -> dict[str, Binder]:
    """Binders from all documents except unresolved near-duplicates."""
    stmt = select(Document).where(Document.near_duplicate_of_id.is_(None)).order_by(Document.id)
    return group_documents(list(db.scalars(stmt)))


def cover_page_count(n_docs: int) -> int:
    return max(1, -(-n_docs // TOC_ROWS_PER_PAGE))


def _cover_pdf(binder: Binder, page_starts: list[int], generated_by: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Billing binder {binder.patient_id}")
    height = float(letter[1])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def header() -> float:
        c.setFont("Helvetica-Bold", 18)
        c.drawString(72, height - 72, "Patient Billing Binder")
        c.setFont("Helvetica", 11)
        c.drawString(72, height - 96, f"Patient: {binder.patient_name or 'Unknown'}   ID: {binder.patient_id}")
        c.drawString(72, height - 112, f"Generated {stamp} by {generated_by}")
        c.drawString(
            72, height - 128, f"Documents: {len(binder.documents)}   Invoiced total: ${binder.total_billed:,.2f}"
        )
        c.setFont("Helvetica-Bold", 11)
        for x, label in zip(COLUMNS, ("#", "Type", "Service date", "Amount", "Page"), strict=True):
            c.drawString(x, height - 160, label)
        c.setFont("Helvetica", 10)
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(72, 48, "CONFIDENTIAL: contains protected health information. Handle per policy.")
        c.setFont("Helvetica", 10)
        return height - 178

    y = header()
    for idx, (doc, start) in enumerate(zip(binder.documents, page_starts, strict=True), start=1):
        if idx > 1 and (idx - 1) % TOC_ROWS_PER_PAGE == 0:
            c.showPage()
            y = header()
        amount = f"${doc.billed_amount:,.2f}" if doc.billed_amount is not None else "-"
        row = (str(idx), doc.doc_type[:24], doc.service_date or "-", amount, str(start))
        for x, value in zip(COLUMNS, row, strict=True):
            c.drawString(x, y, value)
        y -= 15
    c.save()
    return buf.getvalue()


def render_binder(binder: Binder, store: BlobStore, generated_by: str) -> bytes:
    if not binder.documents:
        raise ValueError("binder has no documents")
    readers = [PdfReader(io.BytesIO(store.get(d.storage_key, d.encrypted))) for d in binder.documents]
    starts: list[int] = []
    page = cover_page_count(len(binder.documents)) + 1
    for reader in readers:
        starts.append(page)
        page += len(reader.pages)
    writer = PdfWriter()
    for p in PdfReader(io.BytesIO(_cover_pdf(binder, starts, generated_by))).pages:
        writer.add_page(p)
    writer.add_outline_item("Cover & contents", 0)
    for doc, reader, start in zip(binder.documents, readers, starts, strict=True):
        for p in reader.pages:
            writer.add_page(p)
        writer.add_outline_item(f"{doc.doc_type}: {doc.filename}", start - 1)
    writer.add_metadata({"/Title": f"Billing binder {binder.patient_id}", "/Producer": "Vitality BSS"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
