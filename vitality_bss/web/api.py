"""JSON API (all routes require authentication except login and health)."""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from vitality_bss import __version__, audit, services
from vitality_bss.binders import UNASSIGNED, load_binders, render_binder
from vitality_bss.config import get_settings
from vitality_bss.db import current_revision, get_db, ping
from vitality_bss.models import AuditEvent, Document, DuplicateUpload, User
from vitality_bss.pdftext import InvalidDocument
from vitality_bss.pipeline import process_upload, reprocess, safe_filename
from vitality_bss.schemas import (
    AuditOut,
    BinderOut,
    DocumentList,
    DocumentOut,
    DocumentUpdate,
    GenerateBinderIn,
    LoginIn,
    PasswordChange,
    TokenOut,
    UploadItem,
    UploadOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from vitality_bss.security import create_access_token, csrf_token_for, verify_password
from vitality_bss.storage import StorageError, get_store
from vitality_bss.web.deps import COOKIE_NAME, Auth, api_auth, api_user, client_ip, require_role

router = APIRouter(prefix="/api")
health_router = APIRouter()

WRITERS = ("admin", "member")


# ------------------------------------------------------------------ health ---


@health_router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@health_router.get("/readyz")
@health_router.get("/api/health")
def readyz() -> JSONResponse:
    """Readiness: the database answers and migrations are applied."""
    db_ok = ping()
    revision = current_revision() if db_ok else None
    ok = db_ok and revision is not None
    body = {
        "status": "ok" if ok else "degraded",
        "version": __version__,
        "database": "ok" if db_ok else "unreachable",
        "schema_revision": revision,
        "ai_mode": get_settings().AI_MODE,
    }
    return JSONResponse(body, status_code=200 if ok else 503)


# -------------------------------------------------------------------- auth ---


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=s.cookie_secure, samesite="strict",
        max_age=s.ACCESS_TOKEN_EXPIRE_MINUTES * 60, path="/",
    )


def do_login(db: Session, request: Request, email: str, password: str) -> tuple[User | None, str]:
    """Authenticate + audit. Returns (user, error_message)."""
    try:
        user = services.authenticate(db, email, password)
    except services.AuthError as exc:
        audit.record(db, "auth.login", actor_email=email.strip().lower()[:255], success=False,
                     ip=client_ip(request), details={"locked": exc.locked})
        return None, str(exc)
    audit.record(db, "auth.login", actor=user, ip=client_ip(request))
    return user, ""


@router.post("/auth/login", response_model=TokenOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> TokenOut:
    user, error = do_login(db, request, payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error)
    token = create_access_token(user.id, user.token_version)
    set_session_cookie(response, token)
    return TokenOut(access_token=token, expires_in=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60)


def revoke_sessions(db: Session, request: Request, user_id: int) -> None:
    user = db.get(User, user_id)
    assert user is not None
    user.token_version += 1  # revokes every outstanding token for this user
    audit.record(db, "auth.logout", actor=user, ip=client_ip(request))


@router.post("/auth/logout")
def logout(request: Request, response: Response, auth: Auth = Depends(api_auth),
           db: Session = Depends(get_db)) -> dict[str, bool]:
    revoke_sessions(db, request, auth.user.id)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(auth: Auth = Depends(api_auth)) -> dict[str, object]:
    out = UserOut.model_validate(auth.user).model_dump(mode="json")
    out["csrf_token"] = csrf_token_for(auth.token)
    return out


@router.post("/auth/change-password")
def change_password(payload: PasswordChange, request: Request, user: User = Depends(api_user),
                    db: Session = Depends(get_db)) -> dict[str, bool]:
    db_user = db.get(User, user.id)
    assert db_user is not None
    if not verify_password(payload.current_password, db_user.hashed_password):
        audit.record(db, "auth.change_password", actor=db_user, success=False, ip=client_ip(request))
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    try:
        services.set_password(db, db_user, payload.new_password)
    except services.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.record(db, "auth.change_password", actor=db_user, ip=client_ip(request))
    return {"ok": True}


# ------------------------------------------------------------------- users ---


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_role("admin"))) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id)))


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, request: Request, db: Session = Depends(get_db),
                admin: User = Depends(require_role("admin"))) -> User:
    try:
        user = services.create_user(db, payload.email, payload.password, payload.role, payload.name)
    except services.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.record(db, "user.create", actor=admin, resource_type="user", resource_id=user.id,
                 ip=client_ip(request), details={"role": user.role})
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, request: Request, db: Session = Depends(get_db),
                admin: User = Depends(require_role("admin"))) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        services.update_user(db, user, name=payload.name, role=payload.role,
                             is_active=payload.is_active, password=payload.password)
    except services.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.record(db, "user.update", actor=admin, resource_type="user", resource_id=user.id,
                 ip=client_ip(request),
                 details={"fields": sorted(payload.model_dump(exclude_unset=True, exclude={"password"})),
                          "password_reset": payload.password is not None})
    return user


# --------------------------------------------------------------- documents ---


def get_doc_or_404(db: Session, doc_id: int) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


async def handle_uploads(files: list[UploadFile], request: Request, db: Session, user: User) -> list[UploadItem]:
    s = get_settings()
    if len(files) > s.MAX_FILES_PER_REQUEST:
        raise HTTPException(status_code=413, detail=f"At most {s.MAX_FILES_PER_REQUEST} files per request")
    items: list[UploadItem] = []
    for upload in files:
        name = safe_filename(upload.filename)
        data = await upload.read(s.max_upload_bytes + 1)
        try:
            result = process_upload(db, name, data, user=user)
        except InvalidDocument as exc:
            audit.record(db, "document.upload", actor=user, resource_type="document", success=False,
                         ip=client_ip(request), details={"error": str(exc)})
            items.append(UploadItem(filename=name, ok=False, error=str(exc)))
            continue
        doc = result.document
        audit.record(db, "document.duplicate" if result.duplicate else "document.upload", actor=user,
                     resource_type="document", resource_id=doc.id, ip=client_ip(request),
                     details={"doc_type": doc.doc_type, "status": doc.status})
        items.append(UploadItem(filename=name, ok=True, duplicate=result.duplicate,
                                document=DocumentOut.model_validate(doc)))
    return items


@router.post("/documents", response_model=UploadOut)
@router.post("/documents/upload", response_model=UploadOut, include_in_schema=False)
async def upload_documents(request: Request, files: list[UploadFile] = File(...),
                           db: Session = Depends(get_db),
                           user: User = Depends(require_role(*WRITERS))) -> UploadOut:
    return UploadOut(uploaded=await handle_uploads(files, request, db, user))


@router.get("/documents", response_model=DocumentList)
def list_documents(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(api_user),
    patient_id: str | None = Query(default=None, max_length=64),
    doc_type: str | None = Query(default=None, max_length=64),
    status_: str | None = Query(default=None, alias="status", max_length=32),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> DocumentList:
    stmt = select(Document)
    if patient_id:
        stmt = stmt.where(Document.patient_id == patient_id.strip().upper())
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if status_:
        stmt = stmt.where(Document.status == status_)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Document.filename.ilike(like), Document.patient_name.ilike(like),
                              Document.patient_id.ilike(like), Document.claim_number.ilike(like)))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    docs = list(db.scalars(stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).offset(offset)))
    audit.record(db, "document.list", actor=user, resource_type="document", ip=client_ip(request),
                 details={"count": len(docs)})
    return DocumentList(total=total, limit=limit, offset=offset,
                        documents=[DocumentOut.model_validate(d) for d in docs])


@router.get("/documents/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(api_user)) -> Document:
    doc = get_doc_or_404(db, doc_id)
    audit.record(db, "document.view", actor=user, resource_type="document", resource_id=doc.id,
                 ip=client_ip(request))
    return doc


@router.patch("/documents/{doc_id}", response_model=DocumentOut)
def update_document(doc_id: int, payload: DocumentUpdate, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_role(*WRITERS))) -> Document:
    doc = get_doc_or_404(db, doc_id)
    changed = services.apply_document_update(doc, payload)
    db.commit()
    db.refresh(doc)
    audit.record(db, "document.update", actor=user, resource_type="document", resource_id=doc.id,
                 ip=client_ip(request), details={"fields": changed})
    return doc


def do_reprocess(db: Session, request: Request, user: User, doc_id: int) -> Document:
    doc = get_doc_or_404(db, doc_id)
    try:
        reprocess(db, doc)
    except StorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.record(db, "document.reprocess", actor=user, resource_type="document", resource_id=doc.id,
                 ip=client_ip(request))
    return doc


@router.post("/documents/{doc_id}/reprocess", response_model=DocumentOut)
def reprocess_document(doc_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_role(*WRITERS))) -> Document:
    return do_reprocess(db, request, user, doc_id)


def document_file_response(db: Session, request: Request, user: User, doc_id: int) -> Response:
    doc = get_doc_or_404(db, doc_id)
    try:
        data = get_store().get(doc.storage_key, doc.encrypted)
    except StorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.record(db, "document.download", actor=user, resource_type="document", resource_id=doc.id,
                 ip=client_ip(request))
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="document-{doc.id}.pdf"'})


@router.get("/documents/{doc_id}/file")
def download_document(doc_id: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(api_user)) -> Response:
    return document_file_response(db, request, user, doc_id)


def do_delete(db: Session, request: Request, admin: User, doc_id: int) -> None:
    doc = get_doc_or_404(db, doc_id)
    key = doc.storage_key
    for dup in db.scalars(select(DuplicateUpload).where(DuplicateUpload.document_id == doc.id)):
        db.delete(dup)
    for other in db.scalars(select(Document).where(Document.near_duplicate_of_id == doc.id)):
        other.near_duplicate_of_id = None
        other.near_duplicate_score = None
    db.delete(doc)
    db.commit()
    get_store().delete(key)
    audit.record(db, "document.delete", actor=admin, resource_type="document", resource_id=doc_id,
                 ip=client_ip(request))


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, request: Request, db: Session = Depends(get_db),
                    admin: User = Depends(require_role("admin"))) -> Response:
    do_delete(db, request, admin, doc_id)
    return Response(status_code=204)


@router.get("/duplicates")
def list_duplicates(db: Session = Depends(get_db), _: User = Depends(api_user)) -> dict[str, object]:
    exact = db.scalars(select(DuplicateUpload).order_by(DuplicateUpload.id.desc()).limit(500))
    near = db.scalars(select(Document).where(Document.near_duplicate_of_id.is_not(None)))
    return {
        "exact": [{"id": d.id, "filename": d.filename, "document_id": d.document_id,
                   "created_at": d.created_at.isoformat()} for d in exact],
        "near": [{"document_id": d.id, "near_duplicate_of_id": d.near_duplicate_of_id,
                  "score": d.near_duplicate_score} for d in near],
    }


@router.get("/stats")
def get_stats(db: Session = Depends(get_db), _: User = Depends(api_user)) -> dict[str, object]:
    return services.stats(db)


# ----------------------------------------------------------------- binders ---


@router.get("/binders", response_model=list[BinderOut])
def list_binders(request: Request, db: Session = Depends(get_db), user: User = Depends(api_user)) -> list[BinderOut]:
    binders = load_binders(db)
    audit.record(db, "binder.list", actor=user, resource_type="binder", ip=client_ip(request),
                 details={"count": len(binders)})
    return [
        BinderOut(patient_id=b.patient_id, patient_name=b.patient_name, total_billed=b.total_billed,
                  ready=b.ready, documents=[DocumentOut.model_validate(d) for d in b.documents])
        for b in binders.values()
    ]


def binder_pdf_response(db: Session, request: Request, user: User, patient_id: str) -> StreamingResponse:
    pid = patient_id.strip()
    pid = pid if pid == UNASSIGNED else pid.upper()
    binder = load_binders(db).get(pid)
    if binder is None:
        raise HTTPException(status_code=404, detail="No documents for patient")
    try:
        pdf = render_binder(binder, get_store(), generated_by=user.email)
    except StorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.record(db, "binder.generate", actor=user, resource_type="binder", resource_id=pid,
                 ip=client_ip(request), details={"documents": len(binder.documents)})
    safe = "".join(ch for ch in pid if ch.isalnum() or ch in "-_") or "unassigned"
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="binder_{safe}.pdf"'})


@router.get("/binders/{patient_id}/pdf")
def binder_pdf(patient_id: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(api_user)) -> StreamingResponse:
    return binder_pdf_response(db, request, user, patient_id)


@router.post("/binders/generate")
def generate_binder(payload: GenerateBinderIn, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(api_user)) -> StreamingResponse:
    return binder_pdf_response(db, request, user, payload.patient_id)


# ------------------------------------------------------------------- audit ---


@router.get("/audit", response_model=list[AuditOut])
def list_audit(db: Session = Depends(get_db), _: User = Depends(require_role("admin")),
               action: str | None = Query(default=None, max_length=64),
               limit: int = Query(default=100, ge=1, le=1000),
               offset: int = Query(default=0, ge=0)) -> list[AuditEvent]:
    stmt = select(AuditEvent)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    return list(db.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit).offset(offset)))
