"""Audit trail helpers. Every PHI read/write and every auth event is recorded."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from vitality_bss.logging_setup import get_logger, request_id_var
from vitality_bss.models import AuditEvent, User
from vitality_bss.redact import redact_value

log = get_logger("vitality_bss.audit")


def record(
    db: Session,
    action: str,
    *,
    actor: User | None = None,
    actor_email: str = "",
    resource_type: str = "",
    resource_id: str | int = "",
    success: bool = True,
    ip: str = "",
    details: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else actor_email,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        success=success,
        ip=ip[:64],
        request_id=request_id_var.get(),
        details=redact_value(details or {}),
    )
    db.add(event)
    if commit:
        db.commit()
    log.info(
        "audit",
        extra={
            "audit_action": action,
            "audit_resource": f"{resource_type}:{resource_id}",
            "audit_success": success,
            "actor_id": event.actor_id,
        },
    )
    return event
