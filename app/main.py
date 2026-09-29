"""FastAPI app: the public page (/opinion: the public agenda's form) and the password-protected admin (/admin)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import consent
from .config import BASE_DIR, settings
from .database import SessionLocal, init_db
from .routes import admin, admin_consent, admin_document, public
from .security import ADMIN_SESSION_SECONDS
from .templating import templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("opinion")

MAX_BODY_BYTES = 2 * 1024 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
# The free instance sleeps when idle and starts again on the next visit, so the purge also runs at every start.
ACCESS_INFO_PURGE_INTERVAL = 3600


def purge_expired_access_info() -> int:
    """Erase IP address and browser data older than the stated retention (ACCESS_INFO_RETENTION_DAYS)."""
    days = settings.access_info_retention_days
    if days <= 0:
        return 0
    try:
        with SessionLocal() as db:
            erased = consent.purge_expired_access_info(db, days)
    except SQLAlchemyError:
        logger.exception("automatic access info purge failed")
        return 0
    if erased:
        logger.info("access info erased automatically: %d records older than %d days", erased, days)
    return erased


async def _purge_regularly() -> None:
    while True:
        await asyncio.to_thread(purge_expired_access_info)
        await asyncio.sleep(ACCESS_INFO_PURGE_INTERVAL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # creates missing tables only; never drops or recreates anything
    purging = asyncio.create_task(_purge_regularly())
    yield
    purging.cancel()


app = FastAPI(title="주민의견서", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY_BYTES:
        return PlainTextResponse("요청이 너무 큽니다.", status_code=413)
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
    if not headers.get("content-type", "").startswith("application/pdf"):
        # Not on PDFs: the browser's built-in PDF viewer can be blocked by these.
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    if settings.is_production:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/admin"):
        headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(
    SessionMiddleware,  # signed cookie, used only by the admin pages (login state, CSRF token)
    secret_key=settings.secret_key,
    session_cookie="opinion_admin",
    max_age=ADMIN_SESSION_SECONDS,
    same_site="lax",
    https_only=settings.is_production,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(public.router)
app.include_router(admin_document.router)  # before admin.router: /admin/document is more specific
app.include_router(admin_consent.router)
app.include_router(admin.router)


@app.exception_handler(StarletteHTTPException)
async def not_found_page(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and request.method == "GET":
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return await http_exception_handler(request, exc)
