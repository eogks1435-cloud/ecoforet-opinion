"""Admin session, CSRF tokens, same-origin checks, client IP and the admin login throttle."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import Request

from .config import settings

ADMIN_SESSION_KEY = "admin"
CSRF_SESSION_KEY = "csrf"
ADMIN_SESSION_SECONDS = 8 * 3600


def admin_fingerprint() -> str:
    """Changes whenever ADMIN_PASSWORD or SECRET_KEY changes, which signs every admin session out."""
    return hmac.new(settings.secret_key.encode(), settings.admin_password.encode(), hashlib.sha256).hexdigest()[:32]


def password_matches(candidate: str) -> bool:
    return settings.admin_enabled and hmac.compare_digest(candidate.encode(), settings.admin_password.encode())


def start_admin_session(request: Request) -> None:
    request.session.clear()
    request.session[ADMIN_SESSION_KEY] = {"fp": admin_fingerprint(), "at": int(time.time())}
    request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)


def is_admin(request: Request) -> bool:
    data = request.session.get(ADMIN_SESSION_KEY)
    if not settings.admin_enabled or not isinstance(data, dict):
        return False
    if not hmac.compare_digest(str(data.get("fp", "")), admin_fingerprint()):
        return False
    return time.time() - float(data.get("at", 0)) < ADMIN_SESSION_SECONDS


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def same_origin(request: Request) -> bool:
    """False for cross-site POSTs. Browsers send Origin on POST; Referer is the fallback."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        return True
    if source == "null":
        return False
    return urlsplit(source).netloc.lower() == request.headers.get("host", "").lower()


def csrf_valid(request: Request, submitted: str | None) -> bool:
    expected = request.session.get(CSRF_SESSION_KEY)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(expected), submitted) and same_origin(request)


def client_ip(request: Request) -> str | None:
    """Client address for the record (not an identity check).

    Render puts the real client IP first in X-Forwarded-For; without a proxy header the socket peer is used.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    candidate = forwarded.split(",")[0].strip() if forwarded else ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    return request.client.host[:64] if request.client else None


class LoginThrottle:
    """Blocks an address after too many wrong admin passwords (in memory, per process)."""

    def __init__(self, limit: int = 10, window_seconds: int = 15 * 60):
        self.limit = limit
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        failures = self._failures.setdefault(key, deque())
        while failures and now - failures[0] > self.window:
            failures.popleft()
        return failures

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.limit

    def record_failure(self, key: str) -> None:
        with self._lock:
            if len(self._failures) > 10_000:
                self._failures.clear()
            now = time.monotonic()
            self._recent(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


login_throttle = LoginThrottle()
