"""PostgreSQL integration. Runs only when VBSS_TEST_DATABASE_URL is set (CI service container)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import eob_pdf, invoice_pdf, login, upload
from vitality_bss import db as dbmod

PG_URL = os.environ.get("VBSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="VBSS_TEST_DATABASE_URL not set")


@pytest.fixture
def pg_client(env, set_env):  # type: ignore[no-untyped-def]
    set_env(DATABASE_URL=PG_URL)
    dbmod.reset_engine()
    with dbmod.get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    from vitality_bss.web.app import create_app

    with TestClient(create_app()) as c:
        yield c
    dbmod.reset_engine()


def test_postgres_end_to_end(pg_client):
    assert pg_client.get("/readyz").json()["schema_revision"] == "0001_initial"
    headers = login(pg_client)
    doc = upload(pg_client, headers, "eob.pdf", eob_pdf()).json()["uploaded"][0]["document"]
    assert doc["patient_id"] == "PT-7777"
    assert upload(pg_client, headers, "dup.pdf", eob_pdf()).json()["uploaded"][0]["duplicate"] is True
    upload(pg_client, headers, "inv.pdf", invoice_pdf("PT-7777"))
    r = pg_client.patch(f"/api/documents/{doc['id']}", headers=headers, json={"insurer": "Aetna"})
    assert r.json()["insurer"] == "Aetna"
    assert pg_client.get("/api/documents?q=eob", headers=headers).json()["total"] == 1
    assert pg_client.get("/api/binders/PT-7777/pdf", headers=headers).content[:4] == b"%PDF"
    assert pg_client.get("/api/stats", headers=headers).json()["duplicates_removed"] == 1
    assert pg_client.delete(f"/api/documents/{doc['id']}", headers=headers).status_code == 204
    assert len(pg_client.get("/api/audit", headers=headers).json()) > 5
