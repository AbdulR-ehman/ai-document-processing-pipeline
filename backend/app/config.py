"""Application configuration.

Everything is read from environment variables (optionally seeded from a local
``.env`` file). No secrets are ever hard-coded. The app refuses to start outside
a development environment when the secret key is missing or left at the example
value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

try:  # python-dotenv is an optional convenience; stdlib-only still works.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - defensive
    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

#: Placeholder only. Outside development the app refuses to start while the
#: secret key still has this value (see ``validate_startup``), so this string
#: can never become a real key.
DEFAULT_SECRET = "change-me"  # nosec B105
DEV_ENVS = {"dev", "development", "local", "test"}
# Values that must never be used outside development.
INSECURE_SECRETS = {
    "",
    "change-me",
    "changeme",
    "changeme-please",
    "secret",
    "password",
    "test",
    "dev",
    "secret-key",
}
MIN_PROD_SECRET_LENGTH = 32


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    return None if value is None or value.strip() == "" else value.strip()


def _str(name: str, default: str = "") -> str:
    return _raw(name) or default


def _int(name: str, default: int) -> int:
    try:
        return int(_raw(name) or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_raw(name) or default)
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = _raw(name) or default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    """Immutable, validated application settings."""

    app_env: str
    debug: bool
    log_level: str
    secret_key: str
    access_token_expire_minutes: int
    database_url: str
    upload_dir: Path
    max_upload_bytes: int
    max_upload_mb: int
    max_pdf_pages: int
    max_text_chars: int
    extraction_timeout_seconds: float
    max_docs_per_user: int
    max_storage_bytes_per_user: int
    cors_origins: tuple[str, ...]
    rate_limit_login: str
    rate_limit_upload: str
    rate_limit_reprocess: str
    rate_limit_export: str
    rate_limit_api: str
    login_max_failed: int
    login_lockout_seconds: int
    ai_provider: str
    ai_max_input_chars: int
    ai_timeout_seconds: float
    ai_max_output_tokens: int
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    business_total_tolerance: float
    export_max_rows: int
    expose_docs: bool
    admin_email: str
    project_root: Path = field(default=PROJECT_ROOT)

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in DEV_ENVS

    @property
    def database_path(self) -> Path | None:
        """Return the SQLite file path if the URL points at a file."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        raw = self.database_url[len(prefix):]
        if raw in {"", ":memory:"}:
            return None
        return Path(raw)

    @property
    def has_admin_email(self) -> bool:
        """True when ``ADMIN_EMAIL`` names the account that gets the admin role."""
        return bool(self.admin_email)


def load_settings() -> Settings:
    """Build the settings object from the current environment."""
    max_upload_mb = _int("MAX_UPLOAD_MB", 10)
    settings = Settings(
        app_env=_str("APP_ENV", "dev"),
        debug=_bool("DEBUG", False),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        secret_key=_str("SECRET_KEY", DEFAULT_SECRET),
        access_token_expire_minutes=_int("ACCESS_TOKEN_EXPIRE_MINUTES", 30),
        database_url=_str("DATABASE_URL", "sqlite:///./app.db"),
        upload_dir=_resolve_path(_str("UPLOAD_DIR", "./uploads")),
        max_upload_bytes=max_upload_mb * 1024 * 1024,
        max_upload_mb=max_upload_mb,
        max_pdf_pages=_int("MAX_PDF_PAGES", 50),
        max_text_chars=_int("MAX_TEXT_CHARS", 100_000),
        extraction_timeout_seconds=_float("EXTRACTION_TIMEOUT_SECONDS", 20.0),
        max_docs_per_user=_int("MAX_DOCS_PER_USER", 1000),
        max_storage_bytes_per_user=_int("MAX_STORAGE_MB_PER_USER", 200) * 1024 * 1024,
        cors_origins=_list("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"),
        rate_limit_login=_str("RATE_LIMIT_LOGIN", "5/minute"),
        rate_limit_upload=_str("RATE_LIMIT_UPLOAD", "10/minute"),
        rate_limit_reprocess=_str("RATE_LIMIT_REPROCESS", "10/minute"),
        rate_limit_export=_str("RATE_LIMIT_EXPORT", "5/minute"),
        rate_limit_api=_str("RATE_LIMIT_API", "60/minute"),
        login_max_failed=_int("LOGIN_MAX_FAILED", 5),
        login_lockout_seconds=_int("LOGIN_LOCKOUT_SECONDS", 300),
        ai_provider=_str("AI_PROVIDER", "mock").lower(),
        ai_max_input_chars=_int("AI_MAX_INPUT_CHARS", 20_000),
        ai_timeout_seconds=_float("AI_TIMEOUT_SECONDS", 20.0),
        ai_max_output_tokens=_int("AI_MAX_OUTPUT_TOKENS", 2000),
        openai_base_url=_str("OPENAI_COMPATIBLE_BASE_URL", ""),
        openai_api_key=_str("OPENAI_COMPATIBLE_API_KEY", ""),
        openai_model=_str("OPENAI_COMPATIBLE_MODEL", "gpt-4o-mini"),
        business_total_tolerance=_float("BUSINESS_TOTAL_TOLERANCE", 0.01),
        export_max_rows=_int("EXPORT_MAX_ROWS", 5000),
        expose_docs=_bool("EXPOSE_DOCS", True),
        admin_email=_str("ADMIN_EMAIL", "").lower(),
    )
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    return settings


def validate_startup(settings: Settings) -> None:
    """Fail fast on dangerous configuration. Raises ``RuntimeError``."""
    if settings.ai_provider not in {"mock", "openai_compatible"}:
        raise RuntimeError(
            f"AI_PROVIDER must be 'mock' or 'openai_compatible', got {settings.ai_provider!r}"
        )
    if settings.ai_provider == "openai_compatible" and not settings.openai_base_url:
        raise RuntimeError("AI_PROVIDER=openai_compatible requires OPENAI_COMPATIBLE_BASE_URL")
    if settings.is_dev:
        return

    # Non-development environments are held to a higher standard.
    if settings.secret_key.lower() in INSECURE_SECRETS:
        raise RuntimeError(
            "SECRET_KEY is missing or still set to the example value. "
            "Refusing to start outside development."
        )
    if len(settings.secret_key) < MIN_PROD_SECRET_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY must be at least {MIN_PROD_SECRET_LENGTH} characters outside development."
        )
    if settings.debug:
        raise RuntimeError("DEBUG must be false outside development.")
    if settings.expose_docs:
        raise RuntimeError("EXPOSE_DOCS must be false outside development.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings."""
    return load_settings()
