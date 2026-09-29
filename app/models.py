"""Tables.

Legacy agenda (the first single-choice opinion form, kept exactly as it was):
    opinion_documents, opinion_submissions
Agendas and multi-question consent forms (added later; the legacy tables are not altered):
    agendas, consent_versions, consent_drafts, consent_submissions, consent_answers, consent_provisions
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

STATUS_ACTIVE = "ACTIVE"
STATUS_INVALIDATED = "INVALIDATED"
STATUS_WITHDRAWN = "WITHDRAWN"  # consent submissions only: the resident withdrew the whole submission
OPINION_AGREE = "AGREE"
OPINION_DISAGREE = "DISAGREE"

AGENDA_LEGACY = "legacy_opinion"  # the opinion_documents / opinion_submissions tables
AGENDA_CONSENT = "consent"  # consent_versions / consent_submissions


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


class Agenda(Base):
    """What residents are asked about. Each agenda keeps its own wording versions and submissions.

    Exactly one agenda is public (shown at /opinion). The legacy agenda stands for the original
    opinion_documents / opinion_submissions tables, which are kept unchanged.
    """

    __tablename__ = "agendas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # While public: whether submissions are accepted (off = paused/closed, the page stays readable).
    accepting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    public_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("kind IN ('legacy_opinion', 'consent')", name="ck_agenda_kind"),
        Index(
            "uq_agenda_public", "is_public", unique=True,
            postgresql_where=text("is_public"), sqlite_where=text("is_public = 1"),
        ),
        Index(
            "uq_agenda_legacy", "kind", unique=True,
            postgresql_where=text("kind = 'legacy_opinion'"), sqlite_where=text("kind = 'legacy_opinion'"),
        ),
    )


class ConsentVersion(Base):
    """One published version of a consent agenda: the full structured wording, never edited or deleted."""

    __tablename__ = "consent_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agenda_id: Mapped[int] = mapped_column(ForeignKey("agendas.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_scheme: Mapped[str] = mapped_column(String(30), nullable=False, default="consent-json-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("agenda_id", "version_no", name="uq_consent_version_no"),)


class ConsentDraft(Base):
    """The admin's working copy of a consent agenda's wording (one per agenda, freely editable)."""

    __tablename__ = "consent_drafts"

    agenda_id: Mapped[int] = mapped_column(ForeignKey("agendas.id"), primary_key=True)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_ip: Mapped[str | None] = mapped_column(String(64))


class ConsentSubmission(Base):
    """One household's signed consent form for one agenda."""

    __tablename__ = "consent_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, default=uuid.uuid4)
    agenda_id: Mapped[int] = mapped_column(ForeignKey("agendas.id"), nullable=False)
    version_id: Mapped[int] = mapped_column(ForeignKey("consent_versions.id"), nullable=False)
    version_label: Mapped[str] = mapped_column(String(60), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    building: Mapped[str] = mapped_column(String(8), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)
    resident_name: Mapped[str] = mapped_column(String(50), nullable=False)
    signature_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    privacy_consent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Random per page load: a retried request with the same token returns the first result instead of a copy.
    client_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    access_info_collected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_ACTIVE, server_default=STATUS_ACTIVE)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_reason: Mapped[str | None] = mapped_column(String(200))
    # Set with a status change: whether a copy had already been handed to a recipient (who keeps it and has
    # to be told separately; changing the status here does not recall that copy).
    already_delivered: Mapped[bool | None] = mapped_column(Boolean)

    answers: Mapped[list[ConsentAnswer]] = relationship(
        back_populates="submission", lazy="selectin", order_by="ConsentAnswer.question_key"
    )
    provisions: Mapped[list[ConsentProvision]] = relationship(
        back_populates="submission", lazy="selectin", order_by="ConsentProvision.recipient_key"
    )
    version: Mapped[ConsentVersion] = relationship(lazy="joined", innerjoin=True)  # version_id is NOT NULL

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'INVALIDATED', 'WITHDRAWN')", name="ck_consent_status"),
        CheckConstraint("privacy_consent AND final_confirmed", name="ck_consent_required"),
        # One valid submission per agenda + 동 + 호수; invalidated/withdrawn rows stay on record.
        Index(
            "uq_consent_active_unit",
            "agenda_id",
            "building",
            "unit",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_consent_agenda_submitted", "agenda_id", "submitted_at"),
    )

    @property
    def receipt_no(self) -> str:
        return self.public_id.hex[:8].upper()

    def answer_map(self) -> dict[str, str]:
        return {a.question_key: a.answer for a in self.answers}

    def provision_map(self) -> dict[str, ConsentProvision]:
        return {p.recipient_key: p for p in self.provisions}


class ConsentAnswer(Base):
    """The answer to one question (fixed question key) of one submission."""

    __tablename__ = "consent_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("consent_submissions.id"), nullable=False)
    question_key: Mapped[str] = mapped_column(String(20), nullable=False)
    answer: Mapped[str] = mapped_column(String(10), nullable=False)

    submission: Mapped[ConsentSubmission] = relationship(back_populates="answers")

    __table_args__ = (
        CheckConstraint("answer IN ('AGREE', 'DISAGREE')", name="ck_consent_answer"),
        UniqueConstraint("submission_id", "question_key", name="uq_consent_answer"),
    )


class ConsentProvision(Base):
    """Whether one submission may be provided to one recipient (company, district office)."""

    __tablename__ = "consent_provisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("consent_submissions.id"), nullable=False)
    recipient_key: Mapped[str] = mapped_column(String(20), nullable=False)
    agreed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_reason: Mapped[str | None] = mapped_column(String(200))
    # Withdrawn after the record had already been handed to this recipient (the recipient's copy stays with them).
    already_delivered: Mapped[bool | None] = mapped_column(Boolean)

    submission: Mapped[ConsentSubmission] = relationship(back_populates="provisions")

    __table_args__ = (UniqueConstraint("submission_id", "recipient_key", name="uq_consent_provision"),)

    @property
    def effective(self) -> bool:
        """Agreed and not withdrawn: the record may go to this recipient."""
        return self.agreed and self.withdrawn_at is None
