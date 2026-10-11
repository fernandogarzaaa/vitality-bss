"""Business operations shared by the JSON API, HTML pages and CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vitality_bss.classify import UNCLASSIFIED
from vitality_bss.config import get_settings
from vitality_bss.extract import Fields, missing_fields
from vitality_bss.models import ROLES, Document, DuplicateUpload, User
from vitality_bss.schemas import DocumentUpdate
from vitality_bss.security import check_password_policy, hash_password, verify_password

MANUAL_MIN_PER_DOC = 4.0
AUTO_MIN_PER_DOC = 1.0
EDITABLE_TEXT_FIELDS = ("doc_type", "patient_id", "patient_name", "claim_number", "service_date", "insurer")

_dummy_hash: str | None = None


def _dummy() -> str:
    """A real hash used to equalise timing between unknown-user and bad-password paths."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("timing-equaliser-not-a-real-password")
    return _dummy_hash


class AuthError(Exception):
    def __init__(self, message: str, locked: bool = False) -> None:
        super().__init__(message)
        self.locked = locked


class UserError(ValueError):
    pass


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def authenticate(db: Session, email: str, password: str, now: datetime | None = None) -> User:
    """Return the user or raise AuthError. Applies lockout after repeated failures."""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        verify_password(password, _dummy())
        raise AuthError("Invalid email or password")
    locked_until = _aware(user.locked_until)
    if locked_until is not None and locked_until > now:
        raise AuthError("Account temporarily locked; try again later", locked=True)
    if not verify_password(password, user.hashed_password) or not user.is_active:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= s.LOGIN_MAX_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=s.LOGIN_LOCKOUT_MINUTES)
            user.failed_login_attempts = 0
        db.commit()
        raise AuthError("Invalid email or password")
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()
    return user


def create_user(db: Session, email: str, password: str, role: str = "member", name: str = "") -> User:
    email = email.strip().lower()
    if role not in ROLES:
        raise UserError(f"role must be one of {', '.join(ROLES)}")
    if "@" not in email:
        raise UserError("invalid email address")
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise UserError("A user with that email already exists")
    try:
        check_password_policy(password)
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    user = User(email=email, name=name.strip(), role=role, hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def set_password(db: Session, user: User, new_password: str) -> None:
    try:
        check_password_policy(new_password)
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    user.hashed_password = hash_password(new_password)
    user.token_version += 1  # invalidate existing sessions
    db.commit()


def admin_count(db: Session) -> int:
    stmt = select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
    return int(db.scalar(stmt) or 0)


def update_user(db: Session, user: User, *, name: str | None = None, role: str | None = None,
                is_active: bool | None = None, password: str | None = None) -> User:
    if role is not None and role not in ROLES:
        raise UserError(f"role must be one of {', '.join(ROLES)}")
    demoting = (role is not None and role != "admin") or is_active is False
    if user.role == "admin" and user.is_active and demoting and admin_count(db) <= 1:
        raise UserError("Cannot remove the last active admin")
    if role is not None:
        user.role = role
    if name is not None:
        user.name = name.strip()
    if is_active is not None:
        if not is_active and user.is_active:
            user.token_version += 1
        user.is_active = is_active
    if password is not None:
        set_password(db, user, password)
    db.commit()
    db.refresh(user)
    return user


def bootstrap_admin(db: Session) -> User | None:
    """Create the first admin from ADMIN_EMAIL/ADMIN_PASSWORD when no users exist."""
    s = get_settings()
    if not (s.ADMIN_EMAIL and s.ADMIN_PASSWORD):
        return None
    if db.scalar(select(func.count()).select_from(User)):
        return None
    return create_user(db, s.ADMIN_EMAIL, s.ADMIN_PASSWORD, role="admin", name="Administrator")


def apply_document_update(doc: Document, update: DocumentUpdate) -> list[str]:
    """Apply reviewer corrections; return the list of changed field names."""
    changed: list[str] = []
    for name in EDITABLE_TEXT_FIELDS:
        value = getattr(update, name)
        if value is None:
            continue
        if name in ("patient_id", "claim_number"):
            value = value.upper()
        if getattr(doc, name) != value:
            setattr(doc, name, value)
            changed.append(name)
    if "billed_amount" in update.model_fields_set:
        amount = update.billed_amount if isinstance(update.billed_amount, float) else None
        if doc.billed_amount != amount:
            doc.billed_amount = amount
            changed.append("billed_amount")
    if update.not_duplicate and doc.near_duplicate_of_id is not None:
        doc.near_duplicate_of_id = None
        doc.near_duplicate_score = None
        doc.review_reasons = [r for r in doc.review_reasons if r != "possible_duplicate"]
        changed.append("near_duplicate_of_id")
    if update.mark_reviewed:
        doc.review_reasons = []
        doc.status = "processed"
        changed.append("status")
    elif changed:
        fields = Fields(
            patient_name=doc.patient_name, patient_id=doc.patient_id, claim_number=doc.claim_number,
            service_date=doc.service_date, billed_amount=doc.billed_amount, insurer=doc.insurer,
        )
        keep = [r for r in doc.review_reasons if not r.startswith("missing_") and r != "unclassified"]
        if doc.doc_type == UNCLASSIFIED:
            keep.append("unclassified")
        doc.review_reasons = keep + [f"missing_{f}" for f in missing_fields(fields, doc.doc_type)]
        doc.status = "needs_review" if doc.review_reasons else "processed"
    return changed


def _count(db: Session, *where: object) -> int:
    stmt = select(func.count()).select_from(Document)
    for clause in where:
        stmt = stmt.where(clause)  # type: ignore[arg-type]
    return int(db.scalar(stmt) or 0)


def stats(db: Session) -> dict[str, object]:
    total_docs = _count(db)
    exact_dups = int(db.scalar(select(func.count()).select_from(DuplicateUpload)) or 0)
    near_dups = _count(db, Document.near_duplicate_of_id.is_not(None))
    processed = _count(db, Document.status == "processed")
    patients = int(
        db.scalar(select(func.count(func.distinct(Document.patient_id))).where(Document.patient_id != "")) or 0
    )
    saved_min = (MANUAL_MIN_PER_DOC - AUTO_MIN_PER_DOC) * total_docs
    hours, minutes = divmod(int(saved_min), 60)
    return {
        "total_uploads": total_docs + exact_dups,
        "documents": total_docs,
        "duplicates_removed": exact_dups,
        "possible_duplicates": near_dups,
        "documents_processed": processed,
        "needs_review": total_docs - processed,
        "patients": patients,
        "time_saved_pct": round((MANUAL_MIN_PER_DOC - AUTO_MIN_PER_DOC) / MANUAL_MIN_PER_DOC * 100),
        "time_saved_label": f"{hours}h {minutes}m" if hours else f"{minutes}m",
        "time_saved_note": (
            f"Estimate: {MANUAL_MIN_PER_DOC:.0f} min manual vs {AUTO_MIN_PER_DOC:.0f} min automated per document"
        ),
    }
