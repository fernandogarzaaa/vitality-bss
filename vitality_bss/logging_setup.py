"""Structured logging with PHI redaction applied to every record."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from vitality_bss.redact import redact, redact_value

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class RedactingFilter(logging.Filter):
    """Render the message, redact it, and redact any structured extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 malformed %-args; keep the raw template
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        for key, value in list(vars(record).items()):
            if key not in _RESERVED:
                setattr(record, key, redact_value(value))
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")


def configure_logging(level: str = "INFO", fmt: str = "json") -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.set_name("vitality")
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == "vitality":
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
