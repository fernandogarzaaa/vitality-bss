"""Synthetic demo documents. Every name, ID and amount here is fictional.

The PDFs are generated with reportlab and pushed through the real pipeline so the
demo behaves exactly like production uploads.
"""

from __future__ import annotations

import io

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vitality_bss.models import Document, DuplicateUpload

SYNTHETIC_BANNER = "SYNTHETIC TEST DOCUMENT - NOT REAL PATIENT DATA"


def make_pdf(title: str, lines: list[str], *, banner: bool = True) -> bytes:
    """Render a simple text PDF. Output is byte-for-byte deterministic for equal input."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, 740, title)
    c.setFont("Helvetica", 11)
    y = 712
    for line in lines:
        if y < 72:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = 740
        c.drawString(72, y, line)
        y -= 17
    if banner:
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(72, 40, SYNTHETIC_BANNER)
    c.save()
    return buf.getvalue()


SAMPLES: list[tuple[str, list[str], list[str]]] = [
    ("EXPLANATION OF BENEFITS", ["eob_PT-1001.pdf"], [
        "Insurer: BlueCross BlueShield", "Patient Name: John Carter", "Patient ID: PT-1001",
        "Claim Number: CLM-2026-0042", "Service Date: 08/12/2026", "Provider: Riverside Medical Center",
        "Billed Amount: $1,250.00", "Allowed Amount: $980.00", "Patient Responsibility: $196.00",
        "This is not a bill. This statement explains how your claim was processed."]),
    ("CLINICAL NOTE - Outpatient Visit", ["clinical-note_PT-1001.pdf"], [
        "Patient Name: John Carter", "Patient ID: PT-1001", "Date of Visit: 08/12/2026",
        "Chief Complaint: Persistent lower back pain",
        "History of Present Illness: Patient reports dull lumbar pain for three weeks.",
        "Vital Signs: BP 128/82, HR 74, Temp 98.6F",
        "Physical Exam: Lumbar paraspinal tenderness, full range of motion.",
        "Assessment and Plan: Lumbar strain. Physical therapy. Follow-up visit in two weeks."]),
    ("LABORATORY TEST REPORT", ["lab-report_PT-1001.pdf"], [
        "Patient Name: John Carter", "Patient ID: PT-1001", "Specimen Collected: 08/13/2026",
        "Test Results:", "Hemoglobin A1C: 5.6% (Reference Range: 4.0-5.6%)",
        "Fasting Glucose: 98 mg/dL (Reference Range: 70-99 mg/dL)",
        "Total Cholesterol: 182 mg/dL (Reference Range: <200 mg/dL)"]),
    ("PATIENT BILLING STATEMENT - INVOICE", ["invoice_PT-1001.pdf", "invoice_PT-1001_copy.pdf"], [
        "Patient Name: John Carter", "Patient ID: PT-1001", "Claim Number: CLM-2026-0042",
        "Service Date: 08/12/2026", "Billed Amount: $245.00", "Amount Due: $245.00",
        "Payment Due: 09/30/2026", "Please remit payment to Riverside Medical Center, Billing Dept."]),
    ("EXPLANATION OF BENEFITS", ["eob_PT-2002.pdf"], [
        "Insurer: Aetna", "Patient Name: Maria Santos", "Patient ID: PT-2002", "Claim Number: CLM-2026-0077",
        "Service Date: 08/20/2026", "Billed Amount: $2,340.00", "Allowed Amount: $1,870.00",
        "Patient Responsibility: $468.00", "This is not a bill."]),
    ("HEALTH INSURANCE ENROLLMENT FORM", ["insurance-form_PT-2002.pdf"], [
        "Subscriber Name: Maria Santos", "Patient ID: PT-2002", "Insurer: Aetna", "Policy Number: AET-884210",
        "Group Number: G-55201", "Coverage Start Date: 01/01/2026",
        "Coverage includes medical, dental, and vision benefits."]),
]


def seed_demo(db: Session) -> dict[str, int]:
    """Load the synthetic samples. Idempotent: does nothing when documents exist."""
    from vitality_bss.pipeline import process_upload

    if not db.scalar(select(func.count()).select_from(Document)):
        for title, filenames, lines in SAMPLES:
            pdf = make_pdf(title, lines)
            for filename in filenames:
                process_upload(db, filename, pdf)
    return {
        "documents": int(db.scalar(select(func.count()).select_from(Document)) or 0),
        "duplicates": int(db.scalar(select(func.count()).select_from(DuplicateUpload)) or 0),
    }
