"""CLI, migrations, logging, startup and service-layer tests."""

from __future__ import annotations

import io
import json
import logging

import pytest
from sqlalchemy import inspect, select

from tests.conftest import PASSWORD, eob_pdf
from vitality_bss import cli, services
from vitality_bss import db as dbmod
from vitality_bss.logging_setup import JsonFormatter, RedactingFilter, configure_logging, request_id_var
from vitality_bss.models import AuditEvent, Document, User
from vitality_bss.schemas import DocumentUpdate
from vitality_bss.storage import generate_key

# --------------------------------------------------------------- migrations ---


def test_migrations_create_schema(migrated):
    insp = inspect(dbmod.get_engine())
    assert {"users", "documents", "duplicate_uploads", "audit_events", "alembic_version"} <= set(insp.get_table_names())
    assert dbmod.current_revision() == "0001_initial"
    cols = {c["name"] for c in insp.get_columns("documents")}
    assert {"near_duplicate_of_id", "encrypted", "storage_key", "review_reasons", "embedding"} <= cols


def test_migration_downgrade_and_reupgrade(migrated):
    from alembic import command

    command.downgrade(dbmod.alembic_config(), "base")
    assert "documents" not in inspect(dbmod.get_engine()).get_table_names()
    dbmod.run_migrations()
    assert dbmod.current_revision() == "0001_initial"


def test_current_revision_before_migration(env):
    assert dbmod.current_revision() is None
    assert dbmod.ping() is True


def test_ping_failure(set_env):
    set_env(DATABASE_URL="postgresql+psycopg2://x:y@127.0.0.1:1/none")
    dbmod.reset_engine()
    try:
        assert dbmod.ping() is False
    finally:
        dbmod.reset_engine()


# ---------------------------------------------------------------------- cli ---


def test_cli_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "1.0.0" in capsys.readouterr().out


def test_cli_migrate_seed_create_user(env, capsys, monkeypatch):
    assert cli.main(["migrate"]) == 0
    assert "0001_initial" in capsys.readouterr().out
    assert cli.main(["seed-demo"]) == 0
    assert json.loads(capsys.readouterr().out) == {"documents": 6, "duplicates": 1}
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["create-user", "--email", "Ops@Example.test", "--role", "admin", "--password-stdin"]) == 0
    assert "created admin user" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("weak\n"))
    assert cli.main(["create-user", "--email", "x@example.test", "--password-stdin"]) == 1
    with dbmod.new_session() as s:
        user = s.scalar(select(User).where(User.email == "ops@example.test"))
        assert user is not None and user.role == "admin"
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "user.create")) is not None


def test_cli_process(tmp_path, capsys):
    good = tmp_path / "eob.pdf"
    good.write_bytes(eob_pdf())
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    assert cli.main(["process", str(good), str(bad), str(tmp_path / "missing.pdf")]) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["doc_type"] == "EOB"
    assert lines[0]["fields"]["patient_id"] == "PT-7777"
    assert "text" not in lines[0]
    assert "error" in lines[1] and "error" in lines[2]
    assert cli.main(["process", str(good)]) == 0


def test_cli_generate_secrets(capsys):
    assert cli.main(["generate-secrets"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert len(out["SECRET_KEY"]) >= 48
    from cryptography.fernet import Fernet

    Fernet(out["ENCRYPTION_KEYS"].encode())


def test_cli_check_config(capsys, set_env):
    assert cli.main(["check-config"]) == 0
    assert "configuration ok" in capsys.readouterr().out
    set_env(APP_ENV="production", SECRET_KEY="short")
    assert cli.main(["check-config"]) == 1
    assert "SECRET_KEY" in capsys.readouterr().err
    set_env(APP_ENV="production", SECRET_KEY="x" * 40)
    assert cli.main(["check-config"]) == 0
    assert "warning: ENCRYPTION_KEYS" in capsys.readouterr().out


def test_cli_rotate_keys(session, set_env, capsys, env):
    from vitality_bss.pipeline import process_upload

    doc = process_upload(session, "eob.pdf", eob_pdf()).document
    assert doc.encrypted is False
    assert cli.main(["rotate-keys"]) == 1  # no keys configured
    key = generate_key()
    set_env(ENCRYPTION_KEYS=key)
    assert cli.main(["rotate-keys"]) == 0
    assert "re-encrypted 1" in capsys.readouterr().out
    with dbmod.new_session() as s:
        d = s.get(Document, doc.id)
        assert d is not None and d.encrypted is True
    assert b"PT-7777" not in (env / "data" / "uploads" / f"{doc.storage_key}.bin").read_bytes()


# ------------------------------------------------------------------ logging ---


def _record(msg: str, *args: object, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_redacting_filter_and_json():
    token = request_id_var.set("rid-1")
    try:
        rec = _record("upload for %s failed", "Patient Name: Jane Doe", patient_ref="PT-1234", count=3)
        assert RedactingFilter().filter(rec) is True
        out = json.loads(JsonFormatter().format(rec))
    finally:
        request_id_var.reset(token)
    assert "Jane Doe" not in out["msg"]
    assert out["patient_ref"] == "[REDACTED]"
    assert out["count"] == 3
    assert out["request_id"] == "rid-1"
    assert out["level"] == "INFO"


def test_redacting_filter_bad_args_and_exceptions():
    rec = _record("value %d", "not-an-int")
    RedactingFilter().filter(rec)
    assert rec.getMessage() == "value %d"
    try:
        raise ValueError("bad PT-9999")
    except ValueError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", None, sys.exc_info())
    RedactingFilter().filter(rec)
    assert rec.exc_text and "PT-9999" not in rec.exc_text
    out = json.loads(JsonFormatter().format(rec))
    assert "exc" in out


def test_configure_logging_idempotent(capsys):
    configure_logging("INFO", "json")
    configure_logging("INFO", "json")
    root = logging.getLogger()
    assert sum(1 for h in root.handlers if h.get_name() == "vitality") == 1
    logging.getLogger("x").info("hello PT-4444")
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["msg"] == "hello [REDACTED]"
    configure_logging("WARNING", "text")


# ------------------------------------------------------------------ startup ---


def test_startup_refuses_unsafe_production(env, set_env):
    from fastapi.testclient import TestClient

    from vitality_bss.config import ConfigError
    from vitality_bss.web.app import create_app

    set_env(APP_ENV="production", SECRET_KEY="change-me-in-production")
    with pytest.raises(ConfigError), TestClient(create_app()):
        pass


def test_startup_seeds_demo(env, set_env):
    from fastapi.testclient import TestClient

    from vitality_bss.web.app import create_app

    set_env(SEED_DEMO_DATA="true")
    with TestClient(create_app()):
        pass
    with dbmod.new_session() as s:
        assert len(s.scalars(select(Document)).all()) == 6


def test_bootstrap_admin_only_once(session, set_env):
    admin = services.bootstrap_admin(session)
    assert admin is not None and admin.role == "admin"
    assert services.bootstrap_admin(session) is None
    set_env(ADMIN_PASSWORD="")
    assert services.bootstrap_admin(session) is None


def test_unhandled_error_returns_500_without_details(client, admin_headers, monkeypatch):
    from vitality_bss import services as svc

    def boom(db):  # type: ignore[no-untyped-def]
        raise RuntimeError("secret PT-1234 detail")

    monkeypatch.setattr(svc, "stats", boom)
    r = client.get("/api/stats", headers=admin_headers)
    assert r.status_code == 500
    assert "PT-1234" not in r.text
    assert r.json()["request_id"]


# ----------------------------------------------------------------- services ---


def test_authenticate_inactive_user_counts_failure(session):
    u = services.create_user(session, "a@example.test", PASSWORD)
    services.update_user(session, u, is_active=False)
    with pytest.raises(services.AuthError):
        services.authenticate(session, "a@example.test", PASSWORD)
    session.refresh(u)
    assert u.failed_login_attempts == 1


def test_create_user_errors(session):
    with pytest.raises(services.UserError, match="role"):
        services.create_user(session, "a@example.test", PASSWORD, role="root")
    with pytest.raises(services.UserError, match="email"):
        services.create_user(session, "nope", PASSWORD)
    with pytest.raises(services.UserError):
        services.update_user(session, services.create_user(session, "b@example.test", PASSWORD), role="root")


def test_apply_document_update_recomputes_reasons(session):
    from vitality_bss.pipeline import process_upload

    doc = process_upload(session, "eob.pdf", eob_pdf()).document
    changed = services.apply_document_update(doc, DocumentUpdate(patient_id=""))
    assert changed == ["patient_id"]
    assert doc.status == "needs_review"
    assert doc.review_reasons == ["missing_patient_id"]
    assert services.apply_document_update(doc, DocumentUpdate(patient_id="")) == []
    services.apply_document_update(doc, DocumentUpdate(patient_id="pt-5", doc_type="Unclassified"))
    assert doc.patient_id == "PT-5"
    assert "unclassified" in doc.review_reasons
    services.apply_document_update(doc, DocumentUpdate(doc_type="EOB"))
    assert doc.review_reasons == []
    assert doc.status == "processed"


def test_stats_empty(session):
    s = services.stats(session)
    assert s["total_uploads"] == 0
    assert s["time_saved_label"] == "0m"


def test_stats_hours_label(session):
    for i in range(25):
        session.add(Document(filename=f"{i}.pdf", sha256=f"{i:064d}", status="processed", review_reasons=[]))
    session.commit()
    assert services.stats(session)["time_saved_label"] == "1h 15m"
