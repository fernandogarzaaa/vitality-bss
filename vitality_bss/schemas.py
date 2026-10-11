"""Pydantic request/response models for the JSON API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from vitality_bss.classify import ALL_TYPES
from vitality_bss.extract import is_valid_iso_date, parse_amount

Role = Literal["admin", "member", "viewer"]

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def normalize_email(v: str) -> str:
    v = v.strip().lower()
    if len(v) > 255 or not _EMAIL_RE.match(v):
        raise ValueError("invalid email address")
    return v


EmailStr = Annotated[str, AfterValidator(normalize_email)]


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=255)
    password: str = Field(min_length=1, max_length=1024)
    role: Role = "member"


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=1024)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    sha256: str
    size_bytes: int
    doc_type: str
    classification_confidence: float
    classifier: str
    patient_id: str
    patient_name: str
    claim_number: str
    service_date: str
    billed_amount: float | None
    insurer: str
    status: str
    review_reasons: list[str]
    pages: int
    ocr_pages: int
    near_duplicate_of_id: int | None
    near_duplicate_score: float | None
    encrypted: bool
    created_at: datetime
    updated_at: datetime


class DocumentUpdate(BaseModel):
    """Reviewer corrections. Only provided fields change."""

    model_config = ConfigDict(extra="forbid")

    doc_type: str | None = None
    patient_id: str | None = Field(default=None, max_length=64)
    patient_name: str | None = Field(default=None, max_length=255)
    claim_number: str | None = Field(default=None, max_length=64)
    service_date: str | None = Field(default=None, max_length=32)
    billed_amount: float | str | None = None
    insurer: str | None = Field(default=None, max_length=128)
    mark_reviewed: bool = False
    not_duplicate: bool = False

    @field_validator("doc_type")
    @classmethod
    def _doc_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ALL_TYPES:
            raise ValueError(f"doc_type must be one of {', '.join(ALL_TYPES)}")
        return v

    @field_validator("service_date")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v and not is_valid_iso_date(v):
            raise ValueError("service_date must be YYYY-MM-DD")
        return v

    @field_validator("billed_amount")
    @classmethod
    def _amount(cls, v: float | str | None) -> float | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        parsed = parse_amount(v)
        if parsed is None:
            raise ValueError("billed_amount must be a non-negative number")
        return parsed

    @field_validator("patient_id", "patient_name", "claim_number", "insurer")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None


class UploadItem(BaseModel):
    filename: str
    ok: bool
    duplicate: bool = False
    error: str | None = None
    document: DocumentOut | None = None


class UploadOut(BaseModel):
    uploaded: list[UploadItem]


class DocumentList(BaseModel):
    total: int
    limit: int
    offset: int
    documents: list[DocumentOut]


class BinderOut(BaseModel):
    patient_id: str
    patient_name: str
    total_billed: float
    ready: bool
    documents: list[DocumentOut]


class GenerateBinderIn(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64)


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    actor_id: int | None
    actor_email: str
    action: str
    resource_type: str
    resource_id: str
    success: bool
    ip: str
    request_id: str
    details: dict[str, object]
