"""Application settings, loaded from environment variables (and an optional .env).

Every setting can be overridden with an environment variable of the same name.
Production mode (``APP_ENV=production``) refuses to start with insecure defaults.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET = "change-me-in-production"  # noqa: S105 (sentinel, rejected in production)
DEFAULT_ADMIN_PASSWORD = "ChangeMe123!"  # noqa: S105 (documented demo value, rejected in production)
LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "ollama", "host.docker.internal"})


class ConfigError(ValueError):
    """Raised when the configuration is unsafe or inconsistent."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    # --- runtime ---
    APP_NAME: str = "Vitality BSS"
    APP_ENV: Literal["development", "production", "test"] = "development"
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8001, ge=1, le=65535)
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "json"

    # --- persistence ---
    DATABASE_URL: str = "sqlite:///./data/app.db"
    DATA_DIR: Path = Path("./data")
    AUTO_MIGRATE: bool = True

    # --- auth ---
    SECRET_KEY: str = DEFAULT_SECRET
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=480, ge=5, le=60 * 24 * 7)
    COOKIE_SECURE: bool | None = None  # default: True in production
    LOGIN_MAX_ATTEMPTS: int = Field(default=5, ge=1)
    LOGIN_LOCKOUT_MINUTES: int = Field(default=15, ge=1)
    MIN_PASSWORD_LENGTH: int = Field(default=12, ge=8)
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=16)
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""

    # --- encryption at rest ---
    # Comma-separated Fernet keys. The first key encrypts; all keys decrypt (rotation).
    ENCRYPTION_KEYS: str = ""
    REQUIRE_ENCRYPTION: bool = False

    # --- upload limits ---
    MAX_UPLOAD_MB: int = Field(default=25, ge=1, le=1024)
    MAX_FILES_PER_REQUEST: int = Field(default=20, ge=1, le=500)
    MAX_PAGES_PER_DOCUMENT: int = Field(default=500, ge=1)

    # --- OCR ---
    OCR_ENABLED: bool = True
    OCR_LANG: str = "eng"
    OCR_DPI: int = Field(default=300, ge=72, le=600)
    OCR_MIN_TEXT_CHARS: int = Field(default=25, ge=0)
    OCR_TIMEOUT_SECONDS: int = Field(default=120, ge=5)

    # --- AI ---
    # off: deterministic rules only. llm: OpenAI-compatible endpoint. mock: offline
    # deterministic stand-in for the LLM (tests, demos).
    AI_MODE: Literal["off", "llm", "mock"] = "off"
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "llama-3.1-8b-instant"
    LLM_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0, le=10)
    LLM_MAX_INPUT_CHARS: int = Field(default=12000, ge=500)
    # Sending document text to a non-local LLM transmits PHI to a third party.
    # It is refused unless explicitly acknowledged (and a BAA should be in place).
    LLM_ALLOW_REMOTE_PHI: bool = False

    # --- dedupe ---
    NEAR_DUPLICATE_THRESHOLD: float = Field(default=0.97, ge=0.0, le=1.0)

    # --- demo ---
    SEED_DEMO_DATA: bool = False

    @field_validator("ADMIN_EMAIL")
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.strip().lower()

    # ------------------------------------------------------------ derived ---
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.is_production if self.COOKIE_SECURE is None else self.COOKIE_SECURE

    @property
    def upload_dir(self) -> Path:
        return self.DATA_DIR / "uploads"

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def encryption_keys(self) -> list[str]:
        return [k.strip() for k in self.ENCRYPTION_KEYS.split(",") if k.strip()]

    @property
    def llm_is_local(self) -> bool:
        host = urlparse(self.LLM_BASE_URL).hostname or ""
        return host in LOCAL_LLM_HOSTS

    def validate_for_runtime(self) -> list[str]:
        """Raise ConfigError for unsafe config; return non-fatal warnings otherwise."""
        errors: list[str] = []
        warnings: list[str] = []
        if self.is_production:
            if self.SECRET_KEY == DEFAULT_SECRET or len(self.SECRET_KEY) < 32:
                errors.append("SECRET_KEY must be set to a random value of at least 32 characters")
            if self.ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
                errors.append("ADMIN_PASSWORD must not be the documented demo password")
            if self.SEED_DEMO_DATA:
                errors.append("SEED_DEMO_DATA must be false in production")
            if not self.encryption_keys:
                warnings.append("ENCRYPTION_KEYS is empty: stored documents are not encrypted at rest")
        elif self.SECRET_KEY == DEFAULT_SECRET:
            warnings.append("SECRET_KEY is the default value; set it before exposing the app")
        if self.REQUIRE_ENCRYPTION and not self.encryption_keys:
            errors.append("REQUIRE_ENCRYPTION is true but ENCRYPTION_KEYS is empty")
        if self.ADMIN_PASSWORD and len(self.ADMIN_PASSWORD) < self.MIN_PASSWORD_LENGTH:
            errors.append(f"ADMIN_PASSWORD must be at least {self.MIN_PASSWORD_LENGTH} characters")
        if self.AI_MODE == "llm":
            if not self.LLM_BASE_URL:
                errors.append("AI_MODE=llm requires LLM_BASE_URL")
            elif not self.llm_is_local and not self.LLM_ALLOW_REMOTE_PHI:
                errors.append(
                    "LLM_BASE_URL is a remote host; set LLM_ALLOW_REMOTE_PHI=true only if you "
                    "are permitted to send PHI to that provider (e.g. a signed BAA)"
                )
        if errors:
            raise ConfigError("; ".join(errors))
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
