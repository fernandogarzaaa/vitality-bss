"""Server-rendered HTML pages (cookie session + CSRF-protected forms)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from vitality_bss import __version__, audit, services
from vitality_bss.binders import load_binders
from vitality_bss.classify import ALL_TYPES
from vitality_bss.config import get_settings
from vitality_bss.db import get_db
from vitality_bss.models import ROLES, AuditEvent, Document, User
from vitality_bss.schemas import DocumentUpdate
from vitality_bss.security import create_access_token, csrf_token_for, verify_password
from vitality_bss.web import api
from vitality_bss.web.deps import COOKIE_NAME, Auth, check_form_csrf, client_ip, page_auth, page_role

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(include_in_schema=False)


def base_context(request: Request, auth: Auth | None) -> dict[str, Any]:
    return {
        "app_name": get_settings().APP_NAME,
        "version": __version__,
        "user": auth.user if auth else None,
        "csrf_token": csrf_token_for(auth.token) if auth else "",
        "doc_types": ALL_TYPES,
        "flash": request.query_params.get("msg", "")[:200],
    }


def render(request: Request, name: str, auth: Auth | None, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    context = base_context(request, auth)
    context.update(ctx)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _recent_docs(db: Session, status_filter: str = "", limit: int = 500) -> list[Document]:
    stmt = select(Document)
    if status_filter in ("processed", "needs_review"):
        stmt = stmt.where(Document.status == status_filter)
    return list(db.scalars(stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit)))


# -------------------------------------------------------------------- auth ---


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return render(request, "login.html", None, error=request.query_params.get("error", "")[:200])


@router.post("/login")
def login_form(
    request: Request,
    email: str = Form(..., max_length=255),
    password: str = Form(..., max_length=1024),
    db: Session = Depends(get_db),
) -> Response:
    user, error = api.do_login(db, request, email, password)
    if user is None:
        return render(request, "login.html", None, status_code=401, error=error)
    resp = redirect("/")
    api.set_session_cookie(resp, create_access_token(user.id, user.token_version))
    return resp


@router.post("/logout")
def logout_form(
    request: Request, csrf_token: str = Form(""), auth: Auth = Depends(page_auth), db: Session = Depends(get_db)
) -> Response:
    check_form_csrf(auth, csrf_token)
    api.revoke_sessions(db, request, auth.user.id)
    resp = redirect("/login")
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, auth: Auth = Depends(page_auth)) -> HTMLResponse:
    return render(request, "account.html", auth, error="")


@router.post("/account/password")
def account_password(
    request: Request,
    current_password: str = Form(..., max_length=1024),
    new_password: str = Form(..., max_length=1024),
    csrf_token: str = Form(""),
    auth: Auth = Depends(page_auth),
    db: Session = Depends(get_db),
) -> Response:
    check_form_csrf(auth, csrf_token)
    user = db.get(User, auth.user.id)
    assert user is not None
    if not verify_password(current_password, user.hashed_password):
        audit.record(db, "auth.change_password", actor=user, success=False, ip=client_ip(request))
        return render(request, "account.html", auth, status_code=400, error="Current password is incorrect")
    try:
        services.set_password(db, user, new_password)
    except services.UserError as exc:
        return render(request, "account.html", auth, status_code=422, error=str(exc))
    audit.record(db, "auth.change_password", actor=user, ip=client_ip(request))
    resp = redirect("/login?error=Password+changed.+Please+sign+in+again.")
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --------------------------------------------------------------- dashboard ---


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)) -> HTMLResponse:
    stats = services.stats(db)
    steps = [
        ("Upload", stats["total_uploads"], "Files received"),
        ("AI Processing", stats["documents"], "Classified + fields extracted"),
        ("Remove Duplicates", stats["duplicates_removed"], "Identical content flagged"),
        ("Review", stats["needs_review"], "Need a human check"),
        ("Generate Binder", stats["patients"], "Patient binders"),
    ]
    audit.record(db, "dashboard.view", actor=auth.user, ip=client_ip(request))
    return render(request, "dashboard.html", auth, stats=stats, recent=_recent_docs(db, limit=10), steps=steps)


# --------------------------------------------------------------- documents ---


@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)) -> HTMLResponse:
    status_filter = request.query_params.get("status", "")
    docs = _recent_docs(db, status_filter)
    audit.record(
        db,
        "document.list",
        actor=auth.user,
        resource_type="document",
        ip=client_ip(request),
        details={"count": len(docs)},
    )
    return render(
        request,
        "documents.html",
        auth,
        documents=docs,
        status_filter=status_filter,
        max_mb=get_settings().MAX_UPLOAD_MB,
        results=None,
    )


@router.post("/documents/upload", response_class=HTMLResponse)
async def documents_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    csrf_token: str = Form(""),
    auth: Auth = Depends(page_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    check_form_csrf(auth, csrf_token)
    page_role(auth, *api.WRITERS)
    results = await api.handle_uploads(files, request, db, auth.user)
    return render(
        request,
        "documents.html",
        auth,
        documents=_recent_docs(db),
        status_filter="",
        max_mb=get_settings().MAX_UPLOAD_MB,
        results=results,
    )


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_detail(
    doc_id: int, request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)
) -> HTMLResponse:
    doc = api.get_doc_or_404(db, doc_id)
    audit.record(
        db, "document.view", actor=auth.user, resource_type="document", resource_id=doc.id, ip=client_ip(request)
    )
    original = db.get(Document, doc.near_duplicate_of_id) if doc.near_duplicate_of_id else None
    return render(request, "document_detail.html", auth, doc=doc, original=original, error="")


@router.post("/documents/{doc_id}/edit")
async def document_edit(
    doc_id: int, request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    check_form_csrf(auth, str(form.get("csrf_token", "")))
    page_role(auth, *api.WRITERS)
    doc = api.get_doc_or_404(db, doc_id)
    data: dict[str, Any] = {
        k: str(form[k])
        for k in ("doc_type", "patient_id", "patient_name", "claim_number", "service_date", "billed_amount", "insurer")
        if k in form
    }
    data["mark_reviewed"] = form.get("mark_reviewed") == "1"
    data["not_duplicate"] = form.get("not_duplicate") == "1"
    try:
        update = DocumentUpdate(**data)
    except ValidationError as exc:
        msg = "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        return render(request, "document_detail.html", auth, status_code=422, doc=doc, original=None, error=msg)
    changed = services.apply_document_update(doc, update)
    db.commit()
    audit.record(
        db,
        "document.update",
        actor=auth.user,
        resource_type="document",
        resource_id=doc.id,
        ip=client_ip(request),
        details={"fields": changed},
    )
    return redirect(f"/documents/{doc.id}?msg=Saved")


@router.get("/documents/{doc_id}/file")
def document_file(
    doc_id: int, request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)
) -> Response:
    return api.document_file_response(db, request, auth.user, doc_id)


@router.post("/documents/{doc_id}/delete")
def document_delete(
    doc_id: int,
    request: Request,
    csrf_token: str = Form(""),
    auth: Auth = Depends(page_auth),
    db: Session = Depends(get_db),
) -> Response:
    check_form_csrf(auth, csrf_token)
    page_role(auth, "admin")
    api.do_delete(db, request, auth.user, doc_id)
    return redirect("/documents?msg=Document+deleted")


@router.post("/documents/{doc_id}/reprocess")
def document_reprocess(
    doc_id: int,
    request: Request,
    csrf_token: str = Form(""),
    auth: Auth = Depends(page_auth),
    db: Session = Depends(get_db),
) -> Response:
    check_form_csrf(auth, csrf_token)
    page_role(auth, *api.WRITERS)
    api.do_reprocess(db, request, auth.user, doc_id)
    return redirect(f"/documents/{doc_id}?msg=Reprocessed")


# ----------------------------------------------------------------- binders ---


@router.get("/binders", response_class=HTMLResponse)
def binders_page(request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)) -> HTMLResponse:
    binders = load_binders(db)
    audit.record(
        db,
        "binder.list",
        actor=auth.user,
        resource_type="binder",
        ip=client_ip(request),
        details={"count": len(binders)},
    )
    return render(request, "binders.html", auth, binders=list(binders.values()))


@router.post("/binders/generate")
def binders_generate(
    request: Request,
    patient_id: str = Form(..., max_length=64),
    csrf_token: str = Form(""),
    auth: Auth = Depends(page_auth),
    db: Session = Depends(get_db),
) -> Response:
    check_form_csrf(auth, csrf_token)
    return api.binder_pdf_response(db, request, auth.user, patient_id)


# ------------------------------------------------------------------- admin ---


def _users_page(request: Request, auth: Auth, db: Session, status_code: int = 200, error: str = "") -> HTMLResponse:
    users = list(db.scalars(select(User).order_by(User.id)))
    return render(request, "users.html", auth, status_code=status_code, users=users, roles=ROLES, error=error)


@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)) -> HTMLResponse:
    page_role(auth, "admin")
    return _users_page(request, auth, db)


@router.post("/admin/users")
def users_create(
    request: Request,
    email: str = Form(..., max_length=255),
    name: str = Form("", max_length=255),
    password: str = Form(..., max_length=1024),
    role: str = Form("member"),
    csrf_token: str = Form(""),
    auth: Auth = Depends(page_auth),
    db: Session = Depends(get_db),
) -> Response:
    check_form_csrf(auth, csrf_token)
    page_role(auth, "admin")
    try:
        user = services.create_user(db, email, password, role, name)
    except services.UserError as exc:
        return _users_page(request, auth, db, 422, str(exc))
    audit.record(
        db,
        "user.create",
        actor=auth.user,
        resource_type="user",
        resource_id=user.id,
        ip=client_ip(request),
        details={"role": user.role},
    )
    return redirect("/admin/users?msg=User+created")


@router.post("/admin/users/{user_id}")
def users_update(
    user_id: int,
    request: Request,
    role: str = Form(...),
    is_active: str = Form("0"),
    csrf_token: str = Form(""),
    auth: Auth = Depends(page_auth),
    db: Session = Depends(get_db),
) -> Response:
    check_form_csrf(auth, csrf_token)
    page_role(auth, "admin")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        services.update_user(db, user, role=role, is_active=is_active == "1")
    except services.UserError as exc:
        return _users_page(request, auth, db, 422, str(exc))
    audit.record(
        db,
        "user.update",
        actor=auth.user,
        resource_type="user",
        resource_id=user.id,
        ip=client_ip(request),
        details={"role": role, "is_active": is_active == "1"},
    )
    return redirect("/admin/users?msg=User+updated")


@router.get("/admin/audit", response_class=HTMLResponse)
def audit_page(request: Request, auth: Auth = Depends(page_auth), db: Session = Depends(get_db)) -> HTMLResponse:
    page_role(auth, "admin")
    events = list(db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(300)))
    return render(request, "audit.html", auth, events=events)
