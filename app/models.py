"""Tables: opinion_documents (versions of the document) and opinion_submissions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

STATUS_ACTIVE = "ACTIVE"
STATUS_INVALIDATED = "INVALIDATED"
OPINION_AGREE = "AGREE"
OPINION_DISAGREE = "DISAGREE"


class OpinionDocument(Base):
    """One published version of the opinion document. Versions are never edited or deleted."""

    __tablename__ = "opinion_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # paragraphs split by a blank line, **bold**
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # one per line
    recipient: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        # At most one version is shown to residents at a time.
        Index(
            "uq_opinion_document_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )


class OpinionSubmission(Base):
    __tablename__ = "opinion_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, default=uuid.uuid4)
    building: Mapped[str] = mapped_column(String(8), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)
    resident_name: Mapped[str] = mapped_column(String(50), nullable=False)
    opinion_choice: Mapped[str] = mapped_column(String(16), nullable=False)
    # Grayscale PNG (BYTEA on PostgreSQL). Deferred so list queries never load the images.
    signature_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    additional_comment: Mapped[str | None] = mapped_column(Text)
    statement_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    usage_consent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    document_title: Mapped[str] = mapped_column(String(200), nullable=False)
    document_version: Mapped[str] = mapped_column(String(50), nullable=False)
    document_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    # Server time when the submission reached the app (제출 시각 = 서버 수신 시각), stored in UTC.
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set by the database when the row is inserted.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_ACTIVE, server_default=STATUS_ACTIVE)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_reason: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint("opinion_choice IN ('AGREE', 'DISAGREE')", name="ck_opinion_choice"),
        CheckConstraint("status IN ('ACTIVE', 'INVALIDATED')", name="ck_opinion_status"),
        CheckConstraint("statement_confirmed AND usage_consent", name="ck_opinion_consents"),
        # UNIQUE(building, unit) among ACTIVE rows only: blocks a second submission for the same 동·호수,
        # while an invalidated row stays on record and frees the unit for a new submission.
        Index(
            "uq_opinion_active_unit",
            "building",
            "unit",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_opinion_submitted_at", "submitted_at"),
    )

    @property
    def receipt_no(self) -> str:
        """Short submission number shown to the resident (first 8 hex digits of public_id)."""
        return self.public_id.hex[:8].upper()
