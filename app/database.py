"""SQLAlchemy engine, request-scoped sessions and the idempotent table setup."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):  # tests without PostgreSQL only
        return create_engine(url, connect_args={"check_same_thread": False}, hide_parameters=True)
    return create_engine(
        url,
        hide_parameters=True,  # DB errors must not copy resident names/comments into the logs
        # One shared pool per process: 5 kept open, up to 10 more under bursts, never one per request.
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=30,
        pool_pre_ping=True,  # replace connections the server closed (idle timeout, DB restart)
        pool_recycle=1800,
        connect_args={
            "connect_timeout": 10,
            "application_name": "ecoforet-opinion",
            "options": "-c statement_timeout=15000",
        },
    )


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create missing tables and seed the first document version.

    Existing tables and data are never dropped or altered.
    """
    from . import models  # noqa: F401  (registers the tables on Base.metadata)
    from .document import seed_initial_document

    Base.metadata.create_all(bind=engine, checkfirst=True)
    with SessionLocal() as db:
        seed_initial_document(db)
