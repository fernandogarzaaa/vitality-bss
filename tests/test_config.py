from pathlib import Path

import pytest

from vitality_bss.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_SECRET, ConfigError, Settings

GOOD_SECRET = "s" * 40


def make(**kw):  # type: ignore[no-untyped-def]
    base = {"SECRET_KEY": GOOD_SECRET, "ADMIN_PASSWORD": "", "ENCRYPTION_KEYS": "", "AI_MODE": "off",
            "APP_ENV": "development", "SEED_DEMO_DATA": False, "REQUIRE_ENCRYPTION": False}
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_defaults():
    s = Settings(_env_file=None)
    assert s.PORT == 8001
    assert s.MAX_UPLOAD_MB == 25
    assert s.NEAR_DUPLICATE_THRESHOLD == 0.97


def test_derived_properties():
    s = make(DATA_DIR=Path("/x"), MAX_UPLOAD_MB=2, ENCRYPTION_KEYS="a, b ,,")
    assert s.upload_dir == Path("/x/uploads")
    assert s.max_upload_bytes == 2 * 1024 * 1024
    assert s.encryption_keys == ["a", "b"]
    assert s.is_production is False
    assert make(APP_ENV="production").is_production is True


def test_cookie_secure_defaults():
    assert make().cookie_secure is False
    assert make(APP_ENV="production").cookie_secure is True
    assert make(APP_ENV="production", COOKIE_SECURE=False).cookie_secure is False
    assert make(COOKIE_SECURE=True).cookie_secure is True


def test_admin_email_lowercased():
    assert make(ADMIN_EMAIL=" Admin@Example.COM ").ADMIN_EMAIL == "admin@example.com"


@pytest.mark.parametrize(
    ("url", "local"),
    [("http://localhost:11434/v1", True), ("http://127.0.0.1:8000/v1", True), ("http://ollama:11434/v1", True),
     ("https://api.groq.com/openai/v1", False), ("https://openrouter.ai/api/v1", False), ("", False)],
)
def test_llm_is_local(url, local):
    assert make(LLM_BASE_URL=url).llm_is_local is local


def test_dev_default_secret_warns():
    warnings = make(SECRET_KEY=DEFAULT_SECRET).validate_for_runtime()
    assert any("SECRET_KEY" in w for w in warnings)
    assert make().validate_for_runtime() == []


def test_production_requires_strong_secret():
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        make(APP_ENV="production", SECRET_KEY=DEFAULT_SECRET).validate_for_runtime()
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        make(APP_ENV="production", SECRET_KEY="x" * 31).validate_for_runtime()
    make(APP_ENV="production", SECRET_KEY="x" * 32, ENCRYPTION_KEYS="k").validate_for_runtime()


def test_production_rejects_demo_password_and_demo_data():
    with pytest.raises(ConfigError, match="ADMIN_PASSWORD"):
        make(APP_ENV="production", ADMIN_PASSWORD=DEFAULT_ADMIN_PASSWORD).validate_for_runtime()
    with pytest.raises(ConfigError, match="SEED_DEMO_DATA"):
        make(APP_ENV="production", SEED_DEMO_DATA=True).validate_for_runtime()


def test_production_warns_without_encryption():
    warnings = make(APP_ENV="production").validate_for_runtime()
    assert any("ENCRYPTION_KEYS" in w for w in warnings)
    assert make(APP_ENV="production", ENCRYPTION_KEYS="k").validate_for_runtime() == []


def test_require_encryption():
    with pytest.raises(ConfigError, match="REQUIRE_ENCRYPTION"):
        make(REQUIRE_ENCRYPTION=True).validate_for_runtime()
    make(REQUIRE_ENCRYPTION=True, ENCRYPTION_KEYS="k").validate_for_runtime()


def test_short_admin_password():
    with pytest.raises(ConfigError, match="at least 12"):
        make(ADMIN_PASSWORD="Short1!").validate_for_runtime()
    make(ADMIN_PASSWORD="Long-Enough-123").validate_for_runtime()


def test_llm_mode_validation():
    with pytest.raises(ConfigError, match="requires LLM_BASE_URL"):
        make(AI_MODE="llm").validate_for_runtime()
    with pytest.raises(ConfigError, match="LLM_ALLOW_REMOTE_PHI"):
        make(AI_MODE="llm", LLM_BASE_URL="https://api.groq.com/openai/v1").validate_for_runtime()
    make(AI_MODE="llm", LLM_BASE_URL="https://api.groq.com/openai/v1", LLM_ALLOW_REMOTE_PHI=True).validate_for_runtime()
    make(AI_MODE="llm", LLM_BASE_URL="http://localhost:11434/v1").validate_for_runtime()


def test_multiple_errors_joined():
    with pytest.raises(ConfigError) as exc:
        make(APP_ENV="production", SECRET_KEY="x", SEED_DEMO_DATA=True).validate_for_runtime()
    assert "SECRET_KEY" in str(exc.value)
    assert "SEED_DEMO_DATA" in str(exc.value)
