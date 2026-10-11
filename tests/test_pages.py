"""HTML UI tests: cookie sessions, CSRF-protected forms, role gating."""

from __future__ import annotations

import re

from tests.conftest import ADMIN, PASSWORD, eob_pdf, make_user, upload
from vitality_bss.security import csrf_token_for

CSRF_RE = re.compile(r'name="csrf_token" value="([0-9a-f]{64})"')


def browser_login(client, email=ADMIN[0], password=ADMIN[1]):  # type: ignore[no-untyped-def]
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/"
    token = client.cookies.get("vbss_session")
    assert token
    return csrf_token_for(token)


def test_login_page_and_redirects(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
    for path in ("/", "/documents", "/binders", "/admin/users", "/admin/audit", "/account", "/documents/1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/login"


def test_bad_login_shows_error(client):
    r = client.post("/login", data={"email": ADMIN[0], "password": "bad"})
    assert r.status_code == 401
    assert "Invalid email or password" in r.text
    assert "vbss_session" not in client.cookies


def test_all_pages_render_for_admin(client):
    browser_login(client)
    for path, needle in [
        ("/", "Processing Workflow"),
        ("/documents", "All Documents"),
        ("/binders", "Patient Binders"),
        ("/admin/users", "Add user"),
        ("/admin/audit", "Audit log"),
        ("/account", "Change password"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert needle in r.text
        assert "cdn." not in r.text and "unpkg" not in r.text  # no third-party assets
        assert CSRF_RE.search(r.text), path


def test_dashboard_content(client):
    browser_login(client)
    r = client.get("/")
    for needle in (
        "Total Documents",
        "Duplicates Removed",
        "Documents Processed",
        "Time Saved",
        "Recent Documents",
        "No documents yet",
    ):
        assert needle in r.text


def test_upload_edit_binder_flow(client):
    csrf = browser_login(client)
    files = [
        ("files", ("eob_PT-7777.pdf", eob_pdf(), "application/pdf")),
        ("files", ("copy.pdf", eob_pdf(), "application/pdf")),
        ("files", ("bad.txt", b"nope", "text/plain")),
    ]
    assert client.post("/documents/upload", data={"csrf_token": "x"}, files=files).status_code == 403
    r = client.post("/documents/upload", data={"csrf_token": csrf}, files=files)
    assert r.status_code == 200
    assert "Upload results" in r.text
    assert "duplicate of #1" in r.text
    assert "Only PDF files are supported" in r.text

    r = client.get("/documents/1")
    assert "Test Person" in r.text and "Edit Fields" in r.text
    assert client.get("/documents/999", headers={"accept": "text/html"}).status_code == 404

    r = client.post(
        "/documents/1/edit",
        data={
            "csrf_token": csrf,
            "patient_name": "New Name",
            "doc_type": "EOB",
            "billed_amount": "12.50",
            "service_date": "2026-01-01",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/documents/1?msg=Saved"
    page = client.get("/documents/1?msg=Saved").text
    assert "New Name" in page and "$12.50" in page and "Saved" in page

    r = client.post("/documents/1/edit", data={"csrf_token": csrf, "service_date": "13/13/2026"})
    assert r.status_code == 422
    assert "YYYY-MM-DD" in r.text
    assert client.post("/documents/1/edit", data={"csrf_token": "bad"}).status_code == 403

    r = client.get("/documents/1/file")
    assert r.status_code == 200 and r.content == eob_pdf()

    r = client.post("/binders/generate", data={"csrf_token": csrf, "patient_id": "PT-7777"})
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    assert client.post("/binders/generate", data={"csrf_token": "", "patient_id": "PT-7777"}).status_code == 403

    r = client.post("/documents/1/reprocess", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/documents/1/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert "No documents match" in client.get("/documents").text


def test_documents_status_filter(client):
    browser_login(client)
    r = client.get("/documents?status=needs_review")
    assert r.status_code == 200


def test_viewer_is_read_only(client):
    browser_login(client)
    client.cookies.clear()
    headers = {
        "Authorization": "Bearer "
        + client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).json()["access_token"]
    }
    client.cookies.clear()
    make_user(client, headers, "viewer@example.test", "viewer")
    client.cookies.clear()
    upload(client, headers, "eob.pdf", eob_pdf())
    csrf = browser_login(client, "viewer@example.test", PASSWORD)
    r = client.get("/documents")
    assert "<h2>Upload PDFs</h2>" not in r.text
    assert "Users" not in client.get("/").text.split("<main>")[0]
    assert "Edit Fields" not in client.get("/documents/1").text
    files = [("files", ("a.pdf", eob_pdf(), "application/pdf"))]
    assert client.post("/documents/upload", data={"csrf_token": csrf}, files=files).status_code == 403
    assert client.post("/documents/1/edit", data={"csrf_token": csrf}).status_code == 403
    assert client.post("/documents/1/delete", data={"csrf_token": csrf}).status_code == 403
    assert client.get("/admin/users", headers={"accept": "text/html"}).status_code == 403
    assert client.get("/admin/audit").status_code == 403


def test_admin_user_forms(client):
    csrf = browser_login(client)
    r = client.post(
        "/admin/users", data={"csrf_token": csrf, "email": "new@example.test", "password": "weak", "role": "member"}
    )
    assert r.status_code == 422
    assert "at least" in r.text
    r = client.post(
        "/admin/users",
        data={"csrf_token": csrf, "email": "new@example.test", "password": PASSWORD, "role": "member", "name": "New"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "new@example.test" in client.get("/admin/users").text
    r = client.post(
        "/admin/users/2", data={"csrf_token": csrf, "role": "viewer", "is_active": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    r = client.post("/admin/users/1", data={"csrf_token": csrf, "role": "member", "is_active": "1"})
    assert r.status_code == 422
    assert "last active admin" in r.text
    assert client.post("/admin/users/99", data={"csrf_token": csrf, "role": "member"}).status_code == 404
    assert (
        client.post("/admin/users", data={"csrf_token": "x", "email": "a@b.co", "password": PASSWORD}).status_code
        == 403
    )


def test_account_password_change_and_logout(client):
    csrf = browser_login(client)
    r = client.post("/account/password", data={"csrf_token": csrf, "current_password": "bad", "new_password": PASSWORD})
    assert r.status_code == 400
    r = client.post("/account/password", data={"csrf_token": csrf, "current_password": ADMIN[1], "new_password": "x"})
    assert r.status_code == 422
    r = client.post(
        "/account/password",
        data={"csrf_token": csrf, "current_password": ADMIN[1], "new_password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    client.cookies.clear()
    csrf = browser_login(client, ADMIN[0], PASSWORD)
    assert client.post("/logout", data={"csrf_token": "bad"}).status_code == 403
    r = client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_error_param_escaped(client):
    r = client.get("/login?error=<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_favicon(client):
    assert client.get("/favicon.ico").status_code == 204
