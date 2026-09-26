"""Vitality BSS: upload PDFs -> classify -> extract -> dedupe -> patient binders."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader, PdfWriter
from sqlalchemy.orm import Session

from app import models
from app.classify import BINDER_ORDER, classify_document
from app.core.deps import get_current_user, get_db, page_or_login, page_user
from app.extract import extract_fields

APP_NAME = "Vitality BSS"

router = APIRouter()
templates = Jinja2Templates(directory="templates")
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Honest estimate inputs for the Time Saved stat (labeled as estimate in the UI).
MANUAL_MIN_PER_DOC = 4.0
AUTO_MIN_PER_DOC = 1.0

EDITABLE_FIELDS = (
    "patient_name",
    "patient_id",
    "claim_number",
    "service_date",
    "insurer",
    "doc_type",
)


# ---------------------------------------------------------------- pipeline ---


def extract_pdf_text(data: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n".join(parts), len(reader.pages)


def process_upload(db: Session, filename: str, data: bytes) -> dict:
    """Run one PDF through the full pipeline: hash -> dedupe -> classify ->
    extract -> persist. Duplicate content creates a DuplicateUpload row linked
    to the original Document (never double-counted). Raises ValueError on
    non-PDF input."""
    if not data[:5].startswith(b"%PDF"):
        raise ValueError("Only PDF files are supported")
    sha = hashlib.sha256(data).hexdigest()
    existing = db.query(models.Document).filter(models.Document.sha256 == sha).first()
    text, pages = extract_pdf_text(data)
    doc_type = classify_document(filename, text)
    fields = extract_fields(text)

    if existing:
        dup = models.DuplicateUpload(
            filename=filename, sha256=sha, document_id=existing.id
        )
        db.add(dup)
        db.commit()
        db.refresh(dup)
        return {"duplicate": True, "document": existing, "upload": dup}

    storage_path = str(UPLOAD_DIR / f"{sha}.pdf")
    with open(storage_path, "wb") as fh:
        fh.write(data)
    doc = models.Document(
        filename=filename,
        sha256=sha,
        doc_type=doc_type,
        patient_id=fields["patient_id"],
        patient_name=fields["patient_name"],
        claim_number=fields["claim_number"],
        service_date=fields["service_date"],
        billed_amount=fields["billed_amount"],
        insurer=fields["insurer"],
        status="processed",
        is_duplicate=False,
        pages=pages,
        storage_path=storage_path,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return {"duplicate": False, "document": doc, "upload": None}


# ----------------------------------------------------------------- helpers ---


def _page_ctx(request: Request, user, **extra) -> dict:
    ctx = {"request": request, "app_name": APP_NAME, "user": user}
    ctx.update(extra)
    return ctx


def _login_redirect_or_user(request: Request, db: Session):
    auth = page_or_login(request, db)
    if isinstance(auth, RedirectResponse):
        return auth, None
    return None, auth


def _doc_to_dict(doc: models.Document) -> dict:
    return {
        "id": doc.id,
        "filename": doc.filename,
        "doc_type": doc.doc_type,
        "patient_id": doc.patient_id,
        "patient_name": doc.patient_name,
        "claim_number": doc.claim_number,
        "service_date": doc.service_date,
        "billed_amount": doc.billed_amount,
        "insurer": doc.insurer,
        "status": doc.status,
        "is_duplicate": doc.is_duplicate,
        "pages": doc.pages,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _stats(db: Session) -> dict:
    total_docs = db.query(models.Document).count()
    duplicates = db.query(models.DuplicateUpload).count()
    processed = (
        db.query(models.Document)
        .filter(models.Document.status == "processed")
        .count()
    )
    patients = (
        db.query(models.Document.patient_id)
        .filter(models.Document.patient_id != "")
        .distinct()
        .count()
    )
    reduction_pct = round(
        (MANUAL_MIN_PER_DOC - AUTO_MIN_PER_DOC) / MANUAL_MIN_PER_DOC * 100
    )
    saved_min = (MANUAL_MIN_PER_DOC - AUTO_MIN_PER_DOC) * processed
    hours, minutes = divmod(int(saved_min), 60)
    return {
        "total_documents": total_docs + duplicates,
        "duplicates_removed": duplicates,
        "documents_processed": processed,
        "patients": patients,
        "time_saved_pct": reduction_pct,
        "time_saved_label": f"{hours}h {minutes}m" if hours else f"{minutes}m",
        "time_saved_note": (
            f"Estimate: {MANUAL_MIN_PER_DOC:.0f} min manual vs "
            f"{AUTO_MIN_PER_DOC:.0f} min automated per document"
        ),
    }


def _binder_groups(db: Session) -> dict[str, list[models.Document]]:
    docs = (
        db.query(models.Document)
        .filter(models.Document.is_duplicate.is_(False))
        .order_by(models.Document.created_at)
        .all()
    )
    groups: dict[str, list[models.Document]] = {}
    for doc in docs:
        key = doc.patient_id or "(unassigned)"
        groups.setdefault(key, []).append(doc)
    for key in groups:
        groups[key].sort(
            key=lambda d: (
                BINDER_ORDER.get(d.doc_type, 99),
                d.created_at or d.id,
            )
        )
    return groups


# ------------------------------------------------------------------- pages ---


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    redirect, user = _login_redirect_or_user(request, db)
    if redirect:
        return redirect
    stats = _stats(db)
    recent = (
        db.query(models.Document)
        .order_by(models.Document.created_at.desc())
        .limit(10)
        .all()
    )
    preview = recent[0] if recent else None
    steps = [
        ("Upload", stats["total_documents"], "Documents received"),
        ("AI Processing", stats["documents_processed"], "Classified + fields extracted"),
        ("Remove Duplicates", stats["duplicates_removed"], "Identical content flagged"),
        ("Organize", stats["patients"], "Patient binders auto-grouped"),
        ("Generate Binder", stats["patients"], "Merged PDFs ready"),
    ]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _page_ctx(request, user, stats=stats, recent=recent, preview=preview, steps=steps),
    )


@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request, db: Session = Depends(get_db)):
    redirect, user = _login_redirect_or_user(request, db)
    if redirect:
        return redirect
    docs = (
        db.query(models.Document)
        .order_by(models.Document.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "documents.html", _page_ctx(request, user, documents=docs)
    )


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_detail(doc_id: int, request: Request, db: Session = Depends(get_db)):
    redirect, user = _login_redirect_or_user(request, db)
    if redirect:
        return redirect
    doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
    if not doc:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return templates.TemplateResponse(
        request, "document_detail.html", _page_ctx(request, user, doc=doc)
    )


@router.get("/documents/{doc_id}/preview", response_class=HTMLResponse)
def document_preview(doc_id: int, request: Request, db: Session = Depends(get_db)):
    user = page_user(request, db)
    doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
    if not doc:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return templates.TemplateResponse(
        request, "partials/preview.html", _page_ctx(request, user, doc=doc)
    )


@router.get("/binders", response_class=HTMLResponse)
def binders_page(request: Request, db: Session = Depends(get_db)):
    redirect, user = _login_redirect_or_user(request, db)
    if redirect:
        return redirect
    groups = _binder_groups(db)
    return templates.TemplateResponse(
        request, "binders.html", _page_ctx(request, user, groups=groups)
    )


# --------------------------------------------------------------------- API ---


@router.post("/api/documents/upload")
async def upload_documents(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    results: list[dict] = []
    for upload in files:
        data = await upload.read()
        filename = upload.filename or "upload.pdf"
        try:
            res = process_upload(db, filename, data)
        except ValueError as exc:
            results.append({"filename": filename, "error": str(exc)})
            continue
        doc = res["document"]
        results.append(
            {
                "filename": filename,
                "duplicate": res["duplicate"],
                "document_id": doc.id,
                "doc_type": doc.doc_type,
                "patient_id": doc.patient_id,
                "patient_name": doc.patient_name,
                "claim_number": doc.claim_number,
                "service_date": doc.service_date,
                "billed_amount": doc.billed_amount,
                "insurer": doc.insurer,
            }
        )
    return {"uploaded": results}


@router.get("/api/documents")
def list_documents(db: Session = Depends(get_db), user=Depends(get_current_user)):
    docs = (
        db.query(models.Document)
        .order_by(models.Document.created_at.desc())
        .all()
    )
    return {"documents": [_doc_to_dict(d) for d in docs]}


@router.get("/api/documents/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
    if not doc:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return _doc_to_dict(doc)


@router.post("/api/documents/{doc_id}/fields")
async def update_fields(
    doc_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    doc = db.query(models.Document).filter(models.Document.id == doc_id).first()
    if not doc:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        payload: dict = await request.json()
        wants_redirect = False
    else:
        payload = dict(await request.form())
        wants_redirect = True
    for key in EDITABLE_FIELDS:
        if key in payload and payload[key] is not None:
            setattr(doc, key, str(payload[key]).strip())
    if "billed_amount" in payload:
        raw = str(payload["billed_amount"]).replace("$", "").replace(",", "").strip()
        doc.billed_amount = float(raw) if raw and raw.lower() != "none" else None
    db.commit()
    db.refresh(doc)
    if wants_redirect:
        return RedirectResponse(f"/documents/{doc.id}", status_code=303)
    return {"ok": True, "document": _doc_to_dict(doc)}


@router.get("/api/binders")
def list_binders(db: Session = Depends(get_db), user=Depends(get_current_user)):
    groups = _binder_groups(db)
    return {
        "binders": [
            {
                "patient_id": pid,
                "patient_name": docs[0].patient_name,
                "documents": [_doc_to_dict(d) for d in docs],
            }
            for pid, docs in groups.items()
        ]
    }


@router.post("/api/binders/generate")
async def generate_binder(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        payload: dict = await request.json()
    else:
        payload = dict(await request.form())
    patient_id = str(payload.get("patient_id", "")).strip()
    if not patient_id:
        return JSONResponse({"detail": "patient_id is required"}, status_code=400)
    docs = [
        d
        for d in _binder_groups(db).get(patient_id, [])
        if not d.is_duplicate
    ]
    if not docs:
        return JSONResponse({"detail": "No documents for patient"}, status_code=404)
    writer = PdfWriter()
    for doc in docs:
        reader = PdfReader(doc.storage_path)
        for page in reader.pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    filename = f"binder_{patient_id}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
