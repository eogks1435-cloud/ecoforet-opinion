"""Public pages: the opinion form, the submission endpoint and the completion page."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import exists, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .. import consent, document
from ..config import settings
from ..database import get_db
from ..models import (
    AGENDA_CONSENT,
    AGENDA_LEGACY,
    STATUS_ACTIVE,
    ConsentAnswer,
    ConsentProvision,
    ConsentSubmission,
    OpinionSubmission,
)
from ..schemas import COMMENT_MAX, NAME_MAX, FormErrors, validate_submission
from ..security import client_ip, same_origin
from ..templating import format_kst, templates

router = APIRouter()
logger = logging.getLogger("opinion")

DUPLICATE_MESSAGE = "이미 해당 동·호수로 제출된 의견서가 있습니다."
CONSENT_DUPLICATE_MESSAGE = "이미 해당 동·호수로 제출된 온라인 동의서가 있습니다."
TEMPORARY_FAILURE_MESSAGE = "일시적으로 제출하지 못했습니다. 잠시 후 다시 시도해 주세요."
BAD_REQUEST_MESSAGE = "잘못된 요청입니다. 페이지를 새로고침한 후 다시 시도해 주세요."
DOCUMENT_CHANGED_MESSAGE = (
    "작성하시는 동안 의견서 내용이 변경되었습니다. 페이지를 새로고침하여 변경된 내용을 확인한 뒤 다시 작성해 주세요."
)
CONSENT_CHANGED_MESSAGE = (
    "작성하시는 동안 동의서 문구가 변경되었습니다. 새로고침하여 변경된 내용을 확인하고, 각 질문의 답변과 서명을 "
    "다시 입력해 주세요. 입력하신 동·호수와 성명은 유지됩니다."
)
AGENDA_CHANGED_MESSAGE = "접수 중인 안건이 변경되었습니다. 페이지를 새로고침하여 현재 안건을 확인해 주세요."
PAUSED_MESSAGE = "현재 접수가 일시 중지되었습니다. 안내에 따라 나중에 다시 제출해 주세요."
RECEIPT_MAX_AGE = 30 * 24 * 3600

# The completion page only shows what this signed receipt says, so its URL cannot be forged.
_receipts = URLSafeTimedSerializer(settings.secret_key, salt="opinion-receipt")


def _json(status_code: int, **payload: object) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _duplicate() -> JSONResponse:
    return _json(409, ok=False, code="duplicate", message=DUPLICATE_MESSAGE, errors={"residence": DUPLICATE_MESSAGE})


def _temporary_failure() -> JSONResponse:
    return _json(503, ok=False, code="temporary", message=TEMPORARY_FAILURE_MESSAGE)


def _active_submission_exists(db: Session, building: str, unit: str) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    OpinionSubmission.building == building,
                    OpinionSubmission.unit == unit,
                    OpinionSubmission.status == STATUS_ACTIVE,
                )
            )
        )
    )


@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/opinion", status_code=302)


def consent_page_context(content: dict, *, agenda_code: str, version_label: str, accepting: bool,
                         preview: str = "") -> dict:
    """Template context for the consent form (live page, admin draft preview or a stored version)."""
    return {
        "c": content,
        "questions": consent.active_questions(content),
        "generated_items": consent.generated_items(content),
        "agenda_code": agenda_code,
        "version_label": version_label,
        "accepting": accepting,
        "preview": preview,
        "name_max": NAME_MAX,
    }


@router.api_route("/opinion", methods=["GET", "HEAD"])
def opinion_form(request: Request, db: Session = Depends(get_db)):
    agenda = consent.public_agenda(db)
    if agenda is not None and agenda.kind == AGENDA_CONSENT:
        version = consent.current_version(db, agenda)
        if version is not None:
            context = consent_page_context(
                version.content, agenda_code=agenda.code, version_label=version.label, accepting=agenda.accepting
            )
            return templates.TemplateResponse(request, "consent.html", context, headers={"Cache-Control": "no-cache"})
    current = document.active_document(db)
    if current is None:  # only before the first start has seeded the table
        return PlainTextResponse("의견서를 준비하고 있습니다. 잠시 후 다시 접속해 주세요.", status_code=503)
    context = {
        "doc": document.view_of(current),
        "doc_title": current.title,
        "choices": [
            (value, label, document.OPINION_SUMMARY[value]) for value, label in document.OPINION_CHOICES.items()
        ],
        "statement_text": document.STATEMENT_TEXT,
        "usage_consent_text": document.USAGE_CONSENT_TEXT,
        "comment_max": COMMENT_MAX,
        "name_max": NAME_MAX,
        "paused": agenda is not None and not agenda.accepting,
    }
    return templates.TemplateResponse(request, "opinion.html", context, headers={"Cache-Control": "no-cache"})


@router.post("/opinion")
def submit_opinion(
    request: Request,
    db: Session = Depends(get_db),
    opinion_choice: str = Form(""),
    building: str = Form(""),
    unit: str = Form(""),
    resident_name: str = Form(""),
    signature: str = Form(""),
    additional_comment: str = Form(""),
    statement_confirmed: str = Form(""),
    usage_consent: str = Form(""),
    document_version: str = Form(""),
) -> JSONResponse:
    received_at = datetime.now(timezone.utc)
    # The page posts with fetch and this header; a cross-site form cannot add it without a CORS preflight.
    if request.headers.get("x-requested-with") != "fetch" or not same_origin(request):
        return _json(403, ok=False, code="forbidden", message=BAD_REQUEST_MESSAGE)
    try:
        # The single-choice form is accepted only while its (legacy) agenda is the public, open one.
        agenda = consent.public_agenda(db)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("agenda lookup failed")
        return _temporary_failure()
    if agenda is not None and agenda.kind != AGENDA_LEGACY:
        return _json(409, ok=False, code="document_changed", message=AGENDA_CHANGED_MESSAGE)
    if agenda is not None and not agenda.accepting:
        return _json(423, ok=False, code="paused", message=PAUSED_MESSAGE)

    try:
        data = validate_submission(
            {
                "opinion_choice": opinion_choice,
                "building": building,
                "unit": unit,
                "resident_name": resident_name,
                "signature": signature,
                "additional_comment": additional_comment,
                "statement_confirmed": statement_confirmed,
                "usage_consent": usage_consent,
            }
        )
    except FormErrors as exc:
        return _json(422, ok=False, code="invalid", message="입력 내용을 확인해 주세요.", errors=exc.errors)

    try:
        current = document.active_document(db)
        # The resident must have read the version that is recorded with the signature.
        if current is None or document_version != current.version:
            return _json(409, ok=False, code="document_changed", message=DOCUMENT_CHANGED_MESSAGE)
        if _active_submission_exists(db, data.building, data.unit):
            return _duplicate()
        submission = OpinionSubmission(
            public_id=uuid.uuid4(),
            building=data.building,
            unit=data.unit,
            resident_name=data.resident_name,
            opinion_choice=data.opinion_choice,
            signature_data=data.signature_png,
            additional_comment=data.additional_comment,
            statement_confirmed=True,
            usage_consent=True,
            document_title=current.title,
            document_version=current.version,
            document_text_hash=current.text_hash,
            ip_address=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:512] or None,
            submitted_at=received_at,
            status=STATUS_ACTIVE,
        )
        db.add(submission)
        db.commit()  # success is reported only after this commit has returned
    except IntegrityError:
        # Two submissions for the same 동·호수 at the same moment: the partial unique index keeps only one.
        db.rollback()
        try:
            duplicate = _active_submission_exists(db, data.building, data.unit)
        except SQLAlchemyError:
            db.rollback()
            duplicate = False
        if duplicate:
            return _duplicate()
        logger.exception("opinion insert rejected by a database constraint")
        return _temporary_failure()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("opinion insert failed")
        return _temporary_failure()

    logger.info("opinion submission stored receipt=%s version=%s", submission.receipt_no, submission.document_version)
    token = _receipts.dumps({"n": submission.receipt_no, "t": received_at.isoformat()})
    return _json(201, ok=True, redirect=f"/opinion/complete?r={token}")


async def _form_fields(request: Request) -> dict[str, str]:
    """All text fields of a form post (the consent form's field names depend on its questions)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _consent_receipt(submission: ConsentSubmission) -> JSONResponse:
    token = _receipts.dumps({"n": submission.receipt_no, "t": submission.submitted_at.isoformat(), "k": "consent"})
    return _json(201, ok=True, redirect=f"/opinion/complete?r={token}")


def _replay(db: Session, data: consent.ConsentInput, version_label: str) -> JSONResponse | None:
    """A retry of a request that was already stored returns the first result instead of a second record."""
    if not data.client_token:
        return None
    earlier = db.scalar(select(ConsentSubmission).where(ConsentSubmission.client_token == data.client_token))
    if earlier is None:
        return None
    if earlier.request_hash == data.request_hash(version_label):
        return _consent_receipt(earlier)
    return _json(
        409, ok=False, code="already_submitted",
        message=f"이 화면에서 이미 제출된 동의서가 있습니다(제출번호 {earlier.receipt_no}). "
                "정정이 필요하면 안내된 연락처로 요청해 주세요.",
    )


@router.post("/opinion/consent")
def submit_consent(
    request: Request,
    db: Session = Depends(get_db),
    fields: dict[str, str] = Depends(_form_fields),
) -> JSONResponse:
    received_at = datetime.now(timezone.utc)
    if request.headers.get("x-requested-with") != "fetch" or not same_origin(request):
        return _json(403, ok=False, code="forbidden", message=BAD_REQUEST_MESSAGE)
    try:
        agenda = consent.agenda_by_code(db, fields.get("agenda_code", ""))
        if agenda is None or agenda.kind != AGENDA_CONSENT or not agenda.is_public:
            return _json(409, ok=False, code="document_changed", message=AGENDA_CHANGED_MESSAGE)
        if not agenda.accepting:
            return _json(423, ok=False, code="paused", message=PAUSED_MESSAGE)
        version = consent.current_version(db, agenda)
        # A signature only counts for the wording the resident actually read.
        if version is None or fields.get("version_label") != version.label:
            return _json(409, ok=False, code="document_changed", message=CONSENT_CHANGED_MESSAGE)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("consent agenda lookup failed")
        return _temporary_failure()

    try:
        data = consent.validate_consent(fields, version.content)
    except FormErrors as exc:
        return _json(422, ok=False, code="invalid", message="입력 내용을 확인해 주세요.", errors=exc.errors)

    collect_access = bool(version.content["privacy"].get("collect_access_info"))
    try:
        replay = _replay(db, data, version.label)
        if replay is not None:
            return replay
        if consent.active_submission_exists(db, agenda.id, data.building, data.unit):
            return _json(409, ok=False, code="duplicate", message=CONSENT_DUPLICATE_MESSAGE,
                         errors={"residence": CONSENT_DUPLICATE_MESSAGE})
        submission = ConsentSubmission(
            public_id=uuid.uuid4(),
            agenda_id=agenda.id,
            version_id=version.id,
            version_label=version.label,
            content_hash=version.content_hash,
            building=data.building,
            unit=data.unit,
            resident_name=data.resident_name,
            signature_data=data.signature_png,
            privacy_consent=True,
            final_confirmed=True,
            client_token=data.client_token,
            request_hash=data.request_hash(version.label),
            access_info_collected=collect_access,
            ip_address=client_ip(request) if collect_access else None,
            user_agent=((request.headers.get("user-agent") or "")[:512] or None) if collect_access else None,
            submitted_at=received_at,
            status=STATUS_ACTIVE,
        )
        submission.answers = [ConsentAnswer(question_key=k, answer=v) for k, v in data.answers.items()]
        submission.provisions = [ConsentProvision(recipient_key=k, agreed=v) for k, v in data.provisions.items()]
        db.add(submission)
        db.commit()  # success is reported only after the submission and its answers are committed together
    except IntegrityError:
        # Same unit or same retried request at the same moment: the unique indexes keep only one record.
        db.rollback()
        try:
            replay = _replay(db, data, version.label)
            if replay is not None:
                return replay
            duplicate = consent.active_submission_exists(db, agenda.id, data.building, data.unit)
        except SQLAlchemyError:
            db.rollback()
            duplicate = False
        if duplicate:
            return _json(409, ok=False, code="duplicate", message=CONSENT_DUPLICATE_MESSAGE,
                         errors={"residence": CONSENT_DUPLICATE_MESSAGE})
        logger.exception("consent insert rejected by a database constraint")
        return _temporary_failure()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("consent insert failed")
        return _temporary_failure()

    logger.info("consent submission stored receipt=%s version=%s", submission.receipt_no, submission.version_label)
    return _consent_receipt(submission)


@router.get("/opinion/complete")
def opinion_complete(request: Request, r: str = ""):
    try:
        receipt = _receipts.loads(r, max_age=RECEIPT_MAX_AGE)
        receipt_no = str(receipt["n"])
        submitted_at = datetime.fromisoformat(str(receipt["t"]))
    except (BadData, KeyError, TypeError, ValueError):
        return RedirectResponse("/opinion", status_code=303)
    context = {
        "receipt_no": receipt_no,
        "submitted_at": format_kst(submitted_at),
        "kind": "consent" if receipt.get("k") == "consent" else "opinion",
    }
    return templates.TemplateResponse(request, "success.html", context, headers={"Cache-Control": "no-store"})


@router.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "database unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


@router.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
