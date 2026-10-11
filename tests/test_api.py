"""HTTP API integration tests (FastAPI TestClient, temporary DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import ADMIN, PASSWORD, eob_pdf, invoice_pdf, login, make_user, upload
from vitality_bss import db as dbmod
from vitality_bss.models import User
from vitality_bss.security import create_access_token

# ------------------------------------------------------------------ health ---


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"
    assert body["database"] == "ok"
    assert body["schema_revision"] == "0001_initial"
    assert client.get("/api/health").json()["status"] == "ok"


def test_readyz_degraded_when_db_down(client, monkeypatch):
    from vitality_bss.web import api

    monkeypatch.setattr(api, "ping", lambda: False)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["database"] == "unreachable"


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert len(r.headers["x-request-id"]) == 32
    assert "strict-transport-security" not in r.headers
    assert client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    assert (
        "cache-control" not in client.get("/static/app.css").headers
        or client.get("/static/app.css").headers.get("cache-control") != "no-store"
    )


def test_hsts_when_secure(client, set_env):
    set_env(COOKIE_SECURE="true")
    assert "max-age" in client.get("/healthz").headers["strict-transport-security"]


def test_body_too_large(client, admin_headers):
    r = client.post("/api/documents", headers={**admin_headers, "content-length": str(10**12)}, content=b"x")
    assert r.status_code == 413


# -------------------------------------------------------------------- auth ---


def test_login_and_me(client):
    r = client.post("/api/auth/login", json={"email": "ADMIN@example.test ", "password": ADMIN[1]})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 480 * 60
    cookie = r.headers["set-cookie"]
    assert "vbss_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}).json()
    assert me["email"] == ADMIN[0]
    assert me["role"] == "admin"
    assert len(me["csrf_token"]) == 64
    assert "hashed_password" not in me


def test_login_failures(client):
    assert client.post("/api/auth/login", json={"email": ADMIN[0], "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "ghost@example.test", "password": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "not-an-email", "password": "x"}).status_code == 422
    assert client.post("/api/auth/login", json={"email": ADMIN[0]}).status_code == 422


def test_validation_errors_do_not_echo_input(client):
    r = client.post("/api/auth/login", json={"email": "PT-1234-not-email", "password": ""})
    assert r.status_code == 422
    assert "PT-1234" not in r.text


def test_lockout_after_repeated_failures(client, set_env):
    set_env(LOGIN_MAX_ATTEMPTS="3")
    for _ in range(3):
        assert client.post("/api/auth/login", json={"email": ADMIN[0], "password": "bad"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 401
    assert "locked" in r.json()["detail"]
    with dbmod.new_session() as s:
        user = s.query(User).filter_by(email=ADMIN[0]).one()
        user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    assert client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).status_code == 200


def test_unauthenticated_api_is_401(client):
    for method, path in [
        ("get", "/api/documents"),
        ("get", "/api/documents/1"),
        ("get", "/api/binders"),
        ("get", "/api/stats"),
        ("get", "/api/audit"),
        ("get", "/api/users"),
        ("get", "/api/documents/1/file"),
        ("get", "/api/duplicates"),
        ("get", "/api/auth/me"),
    ]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"] == "Bearer"


def test_invalid_and_stale_tokens(client, admin_headers):
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer "}).status_code == 401
    stale = create_access_token(1, 99)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {stale}"}).status_code == 401
    ghost = create_access_token(999, 0)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {ghost}"}).status_code == 401


def test_logout_revokes_tokens(client, admin_headers):
    assert client.post("/api/auth/logout", headers=admin_headers).json() == {"ok": True}
    assert client.get("/api/auth/me", headers=admin_headers).status_code == 401


def test_cookie_auth_requires_csrf_for_writes(client):
    r = client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200
    # cookie is now set on the client
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    csrf = me.json()["csrf_token"]
    files = [("files", ("eob.pdf", eob_pdf(), "application/pdf"))]
    assert client.post("/api/documents", files=files).status_code == 403
    assert client.post("/api/documents", files=files, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert client.post("/api/documents", files=files, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_change_password(client, admin_headers):
    r = client.post(
        "/api/auth/change-password", headers=admin_headers, json={"current_password": "wrong", "new_password": PASSWORD}
    )
    assert r.status_code == 400
    r = client.post(
        "/api/auth/change-password", headers=admin_headers, json={"current_password": ADMIN[1], "new_password": "weak"}
    )
    assert r.status_code == 422
    r = client.post(
        "/api/auth/change-password",
        headers=admin_headers,
        json={"current_password": ADMIN[1], "new_password": PASSWORD},
    )
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=admin_headers).status_code == 401  # sessions revoked
    login(client, ADMIN[0], PASSWORD)


# ------------------------------------------------------------------- users ---


def test_user_management_and_roles(client, admin_headers):
    member = make_user(client, admin_headers, "member@example.test", "member")
    viewer = make_user(client, admin_headers, "viewer@example.test", "viewer")
    users = client.get("/api/users", headers=admin_headers).json()
    assert [u["email"] for u in users] == [ADMIN[0], "member@example.test", "viewer@example.test"]
    assert client.get("/api/users", headers=member).status_code == 403

    assert upload(client, viewer, "eob.pdf", eob_pdf()).status_code == 403
    r = upload(client, member, "eob.pdf", eob_pdf())
    assert r.status_code == 200
    doc_id = r.json()["uploaded"][0]["document"]["id"]
    assert client.get(f"/api/documents/{doc_id}", headers=viewer).status_code == 200
    assert client.patch(f"/api/documents/{doc_id}", headers=viewer, json={"insurer": "Aetna"}).status_code == 403
    assert client.delete(f"/api/documents/{doc_id}", headers=member).status_code == 403


def test_create_user_validation(client, admin_headers):
    body = {"email": "x@example.test", "password": "weak", "role": "member"}
    assert client.post("/api/users", headers=admin_headers, json=body).status_code == 422
    body["password"] = PASSWORD
    body["role"] = "superuser"
    assert client.post("/api/users", headers=admin_headers, json=body).status_code == 422
    body["role"] = "member"
    assert client.post("/api/users", headers=admin_headers, json=body).status_code == 201
    r = client.post("/api/users", headers=admin_headers, json=body)
    assert r.status_code == 422
    assert "already exists" in r.json()["detail"]


def test_update_user(client, admin_headers):
    make_user(client, admin_headers, "m@example.test", "member")
    users = client.get("/api/users", headers=admin_headers).json()
    uid = users[1]["id"]
    r = client.patch(f"/api/users/{uid}", headers=admin_headers, json={"role": "viewer", "name": " Mo "})
    assert r.json()["role"] == "viewer"
    assert r.json()["name"] == "Mo"
    r = client.patch(f"/api/users/{uid}", headers=admin_headers, json={"is_active": False})
    assert r.json()["is_active"] is False
    assert client.post("/api/auth/login", json={"email": "m@example.test", "password": PASSWORD}).status_code == 401
    r = client.patch(f"/api/users/{uid}", headers=admin_headers, json={"is_active": True, "password": "N3w-Passw0rd!!"})
    assert r.status_code == 200
    login(client, "m@example.test", "N3w-Passw0rd!!")
    assert client.patch("/api/users/999", headers=admin_headers, json={"name": "x"}).status_code == 404


def test_cannot_remove_last_admin(client, admin_headers):
    r = client.patch("/api/users/1", headers=admin_headers, json={"role": "member"})
    assert r.status_code == 422
    assert "last active admin" in r.json()["detail"]
    r = client.patch("/api/users/1", headers=admin_headers, json={"is_active": False})
    assert r.status_code == 422


# --------------------------------------------------------------- documents ---


def test_upload_list_get_update_delete(client, admin_headers):
    r = upload(client, admin_headers, "../eob_PT-7777.pdf", eob_pdf())
    item = r.json()["uploaded"][0]
    assert item["ok"] is True
    assert item["filename"] == "eob_PT-7777.pdf"
    doc = item["document"]
    assert doc["doc_type"] == "EOB"
    assert doc["patient_id"] == "PT-7777"
    assert doc["billed_amount"] == 500.0
    doc_id = doc["id"]

    dup = upload(client, admin_headers, "copy.pdf", eob_pdf()).json()["uploaded"][0]
    assert dup["duplicate"] is True
    assert dup["document"]["id"] == doc_id

    bad = upload(client, admin_headers, "note.txt", b"hello").json()["uploaded"][0]
    assert bad["ok"] is False
    assert "PDF" in bad["error"]

    upload(client, admin_headers, "inv.pdf", invoice_pdf())
    lst = client.get("/api/documents", headers=admin_headers).json()
    assert lst["total"] == 2
    assert client.get("/api/documents?patient_id=pt-7777", headers=admin_headers).json()["total"] == 1
    assert client.get("/api/documents?doc_type=Invoice", headers=admin_headers).json()["total"] == 1
    assert client.get("/api/documents?status=needs_review", headers=admin_headers).json()["total"] == 0
    assert client.get("/api/documents?q=Api", headers=admin_headers).json()["total"] == 1
    page = client.get("/api/documents?limit=1&offset=1", headers=admin_headers).json()
    assert (page["total"], page["limit"], page["offset"], len(page["documents"])) == (2, 1, 1, 1)
    assert client.get("/api/documents?limit=0", headers=admin_headers).status_code == 422

    assert client.get(f"/api/documents/{doc_id}", headers=admin_headers).json()["id"] == doc_id
    assert client.get("/api/documents/999", headers=admin_headers).status_code == 404

    r = client.patch(
        f"/api/documents/{doc_id}",
        headers=admin_headers,
        json={"patient_name": " Edited Name ", "billed_amount": "$1,000.50", "patient_id": "pt-1"},
    )
    assert r.status_code == 200
    assert r.json()["patient_name"] == "Edited Name"
    assert r.json()["billed_amount"] == 1000.5
    assert r.json()["patient_id"] == "PT-1"
    assert (
        client.patch(f"/api/documents/{doc_id}", headers=admin_headers, json={"service_date": "09/01/2026"}).status_code
        == 422
    )
    assert (
        client.patch(f"/api/documents/{doc_id}", headers=admin_headers, json={"doc_type": "Recipe"}).status_code == 422
    )
    assert (
        client.patch(f"/api/documents/{doc_id}", headers=admin_headers, json={"billed_amount": "-5"}).status_code == 422
    )
    assert client.patch(f"/api/documents/{doc_id}", headers=admin_headers, json={"unknown": "x"}).status_code == 422
    r = client.patch(f"/api/documents/{doc_id}", headers=admin_headers, json={"billed_amount": None})
    assert r.json()["billed_amount"] is None
    assert r.json()["status"] == "needs_review"
    assert "missing_billed_amount" in r.json()["review_reasons"]
    r = client.patch(f"/api/documents/{doc_id}", headers=admin_headers, json={"mark_reviewed": True})
    assert r.json()["status"] == "processed"
    assert r.json()["review_reasons"] == []

    f = client.get(f"/api/documents/{doc_id}/file", headers=admin_headers)
    assert f.status_code == 200
    assert f.content == eob_pdf()
    assert f.headers["content-type"] == "application/pdf"

    dups = client.get("/api/duplicates", headers=admin_headers).json()
    assert len(dups["exact"]) == 1 and dups["exact"][0]["document_id"] == doc_id

    assert client.delete(f"/api/documents/{doc_id}", headers=admin_headers).status_code == 204
    assert client.get(f"/api/documents/{doc_id}", headers=admin_headers).status_code == 404
    assert client.delete(f"/api/documents/{doc_id}", headers=admin_headers).status_code == 404
    assert client.get("/api/duplicates", headers=admin_headers).json()["exact"] == []


def test_delete_clears_near_duplicate_links(client, admin_headers):
    a = upload(client, admin_headers, "a.pdf", eob_pdf()).json()["uploaded"][0]["document"]
    b = upload(client, admin_headers, "b.pdf", eob_pdf(extra=["copy"])).json()["uploaded"][0]["document"]
    assert b["near_duplicate_of_id"] == a["id"]
    assert client.get("/api/duplicates", headers=admin_headers).json()["near"][0]["document_id"] == b["id"]
    client.delete(f"/api/documents/{a['id']}", headers=admin_headers)
    assert client.get(f"/api/documents/{b['id']}", headers=admin_headers).json()["near_duplicate_of_id"] is None


def test_not_duplicate_flag(client, admin_headers):
    upload(client, admin_headers, "a.pdf", eob_pdf())
    b = upload(client, admin_headers, "b.pdf", eob_pdf(extra=["copy"])).json()["uploaded"][0]["document"]
    r = client.patch(f"/api/documents/{b['id']}", headers=admin_headers, json={"not_duplicate": True})
    assert r.json()["near_duplicate_of_id"] is None
    assert "possible_duplicate" not in r.json()["review_reasons"]
    assert r.json()["status"] == "processed"


def test_too_many_files(client, admin_headers, set_env):
    set_env(MAX_FILES_PER_REQUEST="1")
    files = [("files", ("a.pdf", eob_pdf(), "application/pdf")), ("files", ("b.pdf", invoice_pdf(), "application/pdf"))]
    assert client.post("/api/documents", headers=admin_headers, files=files).status_code == 413


def test_legacy_upload_route(client, admin_headers):
    r = client.post(
        "/api/documents/upload", headers=admin_headers, files=[("files", ("eob.pdf", eob_pdf(), "application/pdf"))]
    )
    assert r.status_code == 200


def test_reprocess_endpoint(client, admin_headers, set_env):
    doc = upload(client, admin_headers, "eob.pdf", eob_pdf()).json()["uploaded"][0]["document"]
    set_env(AI_MODE="mock")
    r = client.post(f"/api/documents/{doc['id']}/reprocess", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["classifier"] == "rules+mock"


def test_missing_blob_is_409(client, admin_headers, env):
    doc = upload(client, admin_headers, "eob.pdf", eob_pdf()).json()["uploaded"][0]["document"]
    for f in (env / "data" / "uploads").glob("*.bin"):
        f.unlink()
    assert client.get(f"/api/documents/{doc['id']}/file", headers=admin_headers).status_code == 409
    assert client.post(f"/api/documents/{doc['id']}/reprocess", headers=admin_headers).status_code == 409
    assert client.get("/api/binders/PT-7777/pdf", headers=admin_headers).status_code == 409


# ----------------------------------------------------------- binders/stats ---


def test_binders_and_stats(client, admin_headers):
    upload(client, admin_headers, "eob.pdf", eob_pdf())
    upload(client, admin_headers, "copy.pdf", eob_pdf())
    upload(client, admin_headers, "inv.pdf", invoice_pdf("PT-7777"))
    upload(client, admin_headers, "inv2.pdf", invoice_pdf("PT-8888"))
    binders = client.get("/api/binders", headers=admin_headers).json()
    assert [b["patient_id"] for b in binders] == ["PT-7777", "PT-8888"]
    assert binders[0]["total_billed"] == 321.0
    assert binders[0]["ready"] is True
    assert [d["doc_type"] for d in binders[0]["documents"]] == ["EOB", "Invoice"]

    r = client.get("/api/binders/pt-7777/pdf", headers=admin_headers)
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    assert 'filename="binder_PT-7777.pdf"' in r.headers["content-disposition"]
    r = client.post("/api/binders/generate", headers=admin_headers, json={"patient_id": "PT-8888"})
    assert r.status_code == 200
    assert client.get("/api/binders/PT-0000/pdf", headers=admin_headers).status_code == 404
    assert client.post("/api/binders/generate", headers=admin_headers, json={"patient_id": ""}).status_code == 422

    stats = client.get("/api/stats", headers=admin_headers).json()
    assert stats["total_uploads"] == 4
    assert stats["documents"] == 3
    assert stats["duplicates_removed"] == 1
    assert stats["documents_processed"] == 3
    assert stats["needs_review"] == 0
    assert stats["patients"] == 2
    assert stats["time_saved_pct"] == 75
    assert stats["time_saved_label"] == "9m"


# ------------------------------------------------------------------- audit ---


def test_audit_trail(client, admin_headers):
    doc = upload(client, admin_headers, "eob.pdf", eob_pdf()).json()["uploaded"][0]["document"]
    client.get(f"/api/documents/{doc['id']}", headers=admin_headers)
    client.post("/api/auth/login", json={"email": ADMIN[0], "password": "bad"})
    events = client.get("/api/audit?limit=50", headers=admin_headers).json()
    actions = [e["action"] for e in events]
    assert "document.upload" in actions
    assert "document.view" in actions
    assert "auth.login" in actions
    failed = [e for e in events if e["action"] == "auth.login" and not e["success"]]
    assert failed and failed[0]["actor_email"] == ADMIN[0]
    view = next(e for e in events if e["action"] == "document.view")
    assert view["resource_id"] == str(doc["id"])
    assert view["actor_email"] == ADMIN[0]
    assert len(view["request_id"]) == 32
    only = client.get("/api/audit?action=document.view", headers=admin_headers).json()
    assert {e["action"] for e in only} == {"document.view"}
    # audit details never contain PHI from the document
    assert "PT-7777" not in str(events)
    assert "Test Person" not in str(events)


def test_openapi_available(client):
    spec = client.get("/api/openapi.json").json()
    assert spec["info"]["version"] == "1.0.0"
    assert "/api/documents" in spec["paths"]
