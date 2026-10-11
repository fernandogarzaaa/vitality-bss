"""Shared fixtures. All documents used in tests are synthetic."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

# Configure the process before any application module reads settings.
_BOOT = Path(tempfile.mkdtemp(prefix="vbss-test-"))
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{_BOOT / 'boot.db'}",
        "DATA_DIR": str(_BOOT / "data"),
        "BCRYPT_ROUNDS": "4",
        "LOG_FORMAT": "text",
        "LOG_LEVEL": "WARNING",
        "ADMIN_EMAIL": "admin@example.test",
        "ADMIN_PASSWORD": "Adm1n-Passw0rd!",
        "AI_MODE": "off",
        "ENCRYPTION_KEYS": "",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-0123456789",
    }
)

from typing import TYPE_CHECKING, Any  # noqa: E402

import pytest  # noqa: E402

from vitality_bss.config import reset_settings_cache  # noqa: E402

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

# Heavy imports (FastAPI, SQLAlchemy, reportlab) are deferred so pure unit-test
# modules start fast; this matters for mutation testing, which reruns them often.


def _db() -> Any:
    from vitality_bss import db as dbmod

    return dbmod


def make_pdf(title: str, lines: list[str], *, banner: bool = True) -> bytes:
    from vitality_bss.demo import make_pdf as _make

    return _make(title, lines, banner=banner)


ADMIN = ("admin@example.test", "Adm1n-Passw0rd!")
PASSWORD = "Str0ng-Passw0rd!"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Fresh database + data dir for one test. Yields the tmp dir."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    _db().reset_engine()
    yield tmp_path
    _db().reset_engine()
    reset_settings_cache()


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """set_env(KEY=value, ...) then refresh settings."""

    def _set(**values: str) -> None:
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        reset_settings_cache()

    return _set


@pytest.fixture
def migrated(env: Path) -> Path:
    _db().run_migrations()
    return env


@pytest.fixture
def session(migrated: Path):  # type: ignore[no-untyped-def]
    s = _db().new_session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(env: Path) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    from vitality_bss.web.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client: TestClient, email: str = ADMIN[0], password: str = ADMIN[1]) -> dict[str, str]:
    """Log in via the API and return bearer headers (cookie cleared)."""
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    return login(client)


def make_user(client: TestClient, headers: dict[str, str], email: str, role: str) -> dict[str, str]:
    r = client.post("/api/users", headers=headers, json={"email": email, "password": PASSWORD, "role": role})
    assert r.status_code == 201, r.text
    return login(client, email, PASSWORD)


def eob_pdf(
    patient_id: str = "PT-7777", name: str = "Test Person", amount: str = "500.00", extra: list[str] | None = None
) -> bytes:
    return make_pdf(
        "EXPLANATION OF BENEFITS",
        [
            "Insurer: Humana",
            f"Patient Name: {name}",
            f"Patient ID: {patient_id}",
            "Claim Number: CLM-2026-0777",
            "Service Date: 09/05/2026",
            f"Billed Amount: ${amount}",
            "Patient Responsibility: $100.00",
            *(extra or []),
        ],
    )


def invoice_pdf(patient_id: str = "PT-8888", name: str = "Api Test", amount: str = "321.00") -> bytes:
    return make_pdf(
        "PATIENT BILLING STATEMENT - INVOICE",
        [
            f"Patient Name: {name}",
            f"Patient ID: {patient_id}",
            "Service Date: 09/06/2026",
            f"Billed Amount: ${amount}",
            f"Amount Due: ${amount}",
            "Please remit payment.",
        ],
    )


def upload(client: TestClient, headers: dict[str, str], name: str, data: bytes):  # type: ignore[no-untyped-def]
    return client.post("/api/documents", headers=headers, files=[("files", (name, data, "application/pdf"))])
