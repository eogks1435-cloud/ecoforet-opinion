"""Test setup: point the app at a disposable database *before* the app is imported."""

from __future__ import annotations

import base64
import io
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
ADMIN_PASSWORD = "test-admin-password"
ORIGIN = "http://testserver"


def _test_database_url() -> str:
    """TEST_DATABASE_URL (env or .env) when set, else a throwaway SQLite file.

    Tables are dropped and recreated, so a PostgreSQL target must be a database whose name ends in _test.
    """
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        try:
            from dotenv import dotenv_values

            url = (dotenv_values(PROJECT_DIR / ".env").get("TEST_DATABASE_URL") or "").strip()
        except ImportError:
            url = ""
    if not url:
        return "sqlite:///" + (Path(tempfile.mkdtemp()) / "opinion_test.db").as_posix()
    name = urlsplit(url).path.lstrip("/")
    if not name.endswith("_test"):
        raise RuntimeError(f"TEST_DATABASE_URL must name a database ending in _test, got {name!r}")
    return url


os.environ["DATABASE_URL"] = _test_database_url()
os.environ["APP_ENV"] = "test"
os.environ["SECRET_KEY"] = "pytest-only-secret-key-0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = ADMIN_PASSWORD
os.environ["ACCESS_INFO_RETENTION_DAYS"] = "0"  # no background purge racing the tests; one test turns it on

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app.consent import seed_agendas  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.document import SEED_VERSION, seed_initial_document  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Agenda,
    ConsentAnswer,
    ConsentDraft,
    ConsentProvision,
    ConsentSubmission,
    ConsentVersion,
    OpinionDocument,
    OpinionSubmission,
)
from app.security import login_throttle  # noqa: E402

FETCH_HEADERS = {
    "X-Requested-With": "fetch",
    "Origin": ORIGIN,
    "User-Agent": "Mozilla/5.0 (pytest) OpinionTest/1.0",
}


def signature_data_url(blank: bool = False, size: tuple[int, int] = (600, 300)) -> str:
    image = Image.new("RGBA", size, (255, 255, 255, 255))
    if not blank:
        ImageDraw.Draw(image).line(
            [(60, 200), (160, 90), (240, 210), (330, 100), (420, 190), (520, 120)], fill=(17, 17, 17, 255), width=5
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def form_data(**overrides: str) -> dict[str, str]:
    data = {
        "opinion_choice": "AGREE",
        "building": "101",
        "unit": "1203",
        "resident_name": "홍길동",
        "signature": signature_data_url(),
        "additional_comment": "",
        "statement_confirmed": "on",
        "usage_consent": "on",
        "document_version": SEED_VERSION,
    }
    data.update(overrides)
    return data


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "csrf token missing from the page"
    return match.group(1)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)  # the dedicated test database only (guarded above)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean():
    """Every test starts with no submissions, the seeded first document version and the seeded agendas
    (legacy agenda public, the consent agenda an unpublished draft)."""
    with SessionLocal() as session:
        for table in (ConsentAnswer, ConsentProvision, ConsentSubmission, ConsentVersion, ConsentDraft, Agenda,
                      OpinionSubmission, OpinionDocument):
            session.execute(delete(table))
        session.commit()
        seed_initial_document(session)
        seed_agendas(session)
    login_throttle._failures.clear()
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:  # runs the lifespan (init_db) like a real start
        yield test_client


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def submit(client):
    def _submit(headers: dict[str, str] | None = None, **overrides: str):
        return client.post("/opinion", data=form_data(**overrides), headers=headers or FETCH_HEADERS)

    return _submit


@pytest.fixture
def admin(client):
    page = client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={"password": ADMIN_PASSWORD, "csrf": csrf_from(page.text)},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/admin"
    return client
