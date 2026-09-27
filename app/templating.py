"""Jinja2 templates shared by the public and admin pages, plus Korea-time formatting."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, settings

KST = ZoneInfo("Asia/Seoul")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))  # autoescapes .html


def to_kst(value: datetime) -> datetime:
    if value.tzinfo is None:  # SQLite hands back naive UTC values
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST)


def format_kst(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return to_kst(value).strftime(fmt) if value else ""


def static_url(path: str) -> str:
    return f"/static/{path}?v={settings.asset_version}"


templates.env.filters["kst"] = format_kst
templates.env.globals["static_url"] = static_url
