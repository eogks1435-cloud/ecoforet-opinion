"""Runtime settings read from environment variables (a local .env file is loaded for development)."""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is only a local-development convenience; Render passes real env vars
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

if load_dotenv is not None:
    load_dotenv(PROJECT_DIR / ".env", override=False)

logger = logging.getLogger("opinion")

ADMIN_PASSWORD_MIN_LENGTH = 8
SECRET_KEY_MIN_LENGTH = 32


def normalise_database_url(url: str) -> str:
    """Render hands out postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg 3 driver name."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    secret_key: str
    admin_password: str
    db_pool_size: int
    db_max_overflow: int
    asset_version: str
    access_info_retention_days: int  # IP/browser data older than this is erased automatically; 0 = only by hand

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def admin_enabled(self) -> bool:
        return len(self.admin_password) >= ADMIN_PASSWORD_MIN_LENGTH


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "").strip().lower() or "production"

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")

    secret_key = os.environ.get("SECRET_KEY", "").strip()
    if app_env == "production" and len(secret_key) < SECRET_KEY_MIN_LENGTH:
        raise RuntimeError(f"SECRET_KEY 환경변수를 {SECRET_KEY_MIN_LENGTH}자 이상으로 설정해야 합니다.")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        logger.warning("SECRET_KEY is not set: using a random key for this process (development only).")

    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if len(admin_password) < ADMIN_PASSWORD_MIN_LENGTH:
        logger.warning("ADMIN_PASSWORD is missing or shorter than %d characters: admin login is disabled.",
                       ADMIN_PASSWORD_MIN_LENGTH)

    return Settings(
        app_env=app_env,
        database_url=normalise_database_url(database_url),
        secret_key=secret_key,
        admin_password=admin_password,
        db_pool_size=_int_env("DB_POOL_SIZE", 5),
        db_max_overflow=_int_env("DB_MAX_OVERFLOW", 10),
        # Render sets RENDER_GIT_COMMIT; used to bust browser caches of CSS/JS after each deploy.
        asset_version=os.environ.get("RENDER_GIT_COMMIT", "")[:8] or str(int(time.time())),
        # The published consent form promises 30 days; change both together.
        access_info_retention_days=_int_env("ACCESS_INFO_RETENTION_DAYS", 30),
    )


settings = load_settings()
