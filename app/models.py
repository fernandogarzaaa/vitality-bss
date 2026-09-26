from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), default="", nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(32), default="member", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


# --- Product models are appended below by each repo's builder ---


class Document(Base):
    """A processed healthcare document. sha256 is unique: a re-uploaded file
    creates a DuplicateUpload row instead of a second Document (not double-counted)."""

    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    filename = Column(String(512), nullable=False)
    sha256 = Column(String(64), unique=True, index=True, nullable=False)
    doc_type = Column(String(64), nullable=False, default="")
    patient_id = Column(String(64), index=True, default="", nullable=False)
    patient_name = Column(String(255), default="", nullable=False)
    claim_number = Column(String(64), default="", nullable=False)
    service_date = Column(String(32), default="", nullable=False)
    billed_amount = Column(Float, nullable=True)
    insurer = Column(String(128), default="", nullable=False)
    status = Column(String(32), default="processed", nullable=False)
    is_duplicate = Column(Boolean, default=False, nullable=False)
    pages = Column(Integer, default=1, nullable=False)
    storage_path = Column(String(1024), default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class DuplicateUpload(Base):
    """A re-upload whose content hash already existed; linked to the original."""

    __tablename__ = "duplicate_uploads"

    id = Column(Integer, primary_key=True)
    filename = Column(String(512), nullable=False)
    sha256 = Column(String(64), index=True, nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
