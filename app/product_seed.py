"""Seed demo data: generate real PDFs with reportlab and run them through the
real upload pipeline (process_upload), so seeded data matches real behavior."""

from __future__ import annotations

import io

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app import models


def make_pdf(title: str, lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
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
    c.save()
    return buf.getvalue()


SAMPLES: list[dict] = [
    {
        "title": "EXPLANATION OF BENEFITS",
        "filenames": ["eob_PT-1001.pdf"],
        "lines": [
            "Insurer: BlueCross BlueShield",
            "Patient Name: John Carter",
            "Patient ID: PT-1001",
            "Claim Number: CLM-2026-0042",
            "Service Date: 08/12/2026",
            "Provider: Riverside Medical Center",
            "Billed Amount: $1,250.00",
            "Allowed Amount: $980.00",
            "Patient Responsibility: $196.00",
            "This is not a bill. This statement explains how your claim was processed.",
        ],
    },
    {
        "title": "CLINICAL NOTE - Outpatient Visit",
        "filenames": ["clinical-note_PT-1001.pdf"],
        "lines": [
            "Patient Name: John Carter",
            "Patient ID: PT-1001",
            "Date of Visit: 08/12/2026",
            "Chief Complaint: Persistent lower back pain",
            "History of Present Illness: Patient reports dull lumbar pain for three weeks,",
            "worse after prolonged sitting. No radiating pain or numbness.",
            "Vital Signs: BP 128/82, HR 74, Temp 98.6F",
            "Physical Exam: Lumbar paraspinal tenderness, full range of motion.",
            "Assessment and Plan: Lumbar strain. Prescribed physical therapy,",
            "NSAIDs as needed. Follow-up visit in two weeks.",
        ],
    },
    {
        "title": "LABORATORY TEST REPORT",
        "filenames": ["lab-report_PT-1001.pdf"],
        "lines": [
            "Patient Name: John Carter",
            "Patient ID: PT-1001",
            "Specimen Collected: 08/13/2026",
            "Ordering Physician: Dr. Elena Ruiz",
            "Test Results:",
            "Hemoglobin A1C: 5.6% (Reference Range: 4.0-5.6%)",
            "Fasting Glucose: 98 mg/dL (Reference Range: 70-99 mg/dL)",
            "Total Cholesterol: 182 mg/dL (Reference Range: <200 mg/dL)",
            "Specimen adequate for analysis. Results reviewed by lab director.",
        ],
    },
    {
        "title": "PATIENT BILLING STATEMENT - INVOICE",
        # Same bytes uploaded under two filenames: the second is the
        # intentional duplicate and must be flagged, not double-counted.
        "filenames": ["invoice_PT-1001.pdf", "invoice_PT-1001_copy.pdf"],
        "lines": [
            "Patient Name: John Carter",
            "Patient ID: PT-1001",
            "Claim Number: CLM-2026-0042",
            "Service Date: 08/12/2026",
            "Billed Amount: $245.00",
            "Amount Due: $245.00",
            "Payment Due: 09/30/2026",
            "Please remit payment to Riverside Medical Center, Billing Dept.",
        ],
    },
    {
        "title": "EXPLANATION OF BENEFITS",
        "filenames": ["eob_PT-2002.pdf"],
        "lines": [
            "Insurer: Aetna",
            "Patient Name: Maria Santos",
            "Patient ID: PT-2002",
            "Claim Number: CLM-2026-0077",
            "Service Date: 08/20/2026",
            "Provider: Bayview Clinic",
            "Billed Amount: $2,340.00",
            "Allowed Amount: $1,870.00",
            "Patient Responsibility: $468.00",
            "This is not a bill. This statement explains how your claim was processed.",
        ],
    },
    {
        "title": "CLINICAL NOTE - Annual Physical",
        "filenames": ["clinical-note_PT-2002.pdf"],
        "lines": [
            "Patient Name: Maria Santos",
            "Patient ID: PT-2002",
            "Date of Visit: 08/20/2026",
            "Chief Complaint: Annual physical examination",
            "Vital Signs: BP 118/76, HR 68, Temp 98.4F",
            "Physical Exam: Unremarkable. Lungs clear, heart regular rhythm.",
            "Assessment and Plan: Healthy adult. Routine labs ordered.",
            "Follow-up visit in twelve months or sooner if concerns arise.",
        ],
    },
    {
        "title": "HEALTH INSURANCE ENROLLMENT FORM",
        "filenames": ["insurance-form_PT-2002.pdf"],
        "lines": [
            "Subscriber Name: Maria Santos",
            "Patient ID: PT-2002",
            "Insurer: Aetna",
            "Policy Number: AET-884210",
            "Group Number: G-55201",
            "Coverage Start Date: 01/01/2026",
            "Please complete all fields and return this form to Human Resources.",
            "Coverage includes medical, dental, and vision benefits.",
        ],
    },
]


def seed_product(db) -> dict:
    """Idempotent: skip when documents already exist."""
    if db.query(models.Document).count() > 0:
        return {
            "documents": db.query(models.Document).count(),
            "duplicates": db.query(models.DuplicateUpload).count(),
        }
    from app.product import process_upload

    for sample in SAMPLES:
        pdf = make_pdf(sample["title"], sample["lines"])
        for filename in sample["filenames"]:
            process_upload(db, filename, pdf)
    return {
        "documents": db.query(models.Document).count(),
        "duplicates": db.query(models.DuplicateUpload).count(),
    }
