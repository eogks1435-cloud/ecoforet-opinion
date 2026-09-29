"""SQLAlchemy engine, request-scoped sessions and the idempotent table setup."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
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


# Columns added to a table after it was first created. create_all() never alters an existing table, so these are
# added here: nullable, no default, no rewrite of existing rows. Older code simply does not read them.
ADDED_COLUMNS = {"consent_submissions": {"overseas_consent": "BOOLEAN"}}


def _add_missing_columns(bind: Engine) -> None:
    for table, columns in ADDED_COLUMNS.items():
        if not inspect(bind).has_table(table):
            continue
        for name, sql_type in columns.items():
            if name in {column["name"] for column in inspect(bind).get_columns(table)}:
                continue
            try:
                with bind.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                logging.getLogger("opinion").info("database: added column %s.%s", table, name)
            except SQLAlchemyError:
                # Another instance starting at the same moment may have added it; anything else is re-raised.
                if name not in {column["name"] for column in inspect(bind).get_columns(table)}:
                    raise


def init_db(bind: Engine | None = None) -> None:
    """Create missing tables and seed the first document version and the agendas.

    Additive only: existing tables, columns and rows are never dropped, altered or rewritten. On a database
    that predates agendas this adds the agenda/consent tables, registers the existing opinion form as the
    public legacy agenda and creates the new consent agenda as an unpublished draft.
    """
    from . import models  # noqa: F401  (registers the tables on Base.metadata)
    from .consent import seed_agendas
    from .document import seed_initial_document

    bind = bind or engine
    Base.metadata.create_all(bind=bind, checkfirst=True)
    _add_missing_columns(bind)
    with sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)() as db:
        seed_initial_document(db)
        seed_agendas(db)
