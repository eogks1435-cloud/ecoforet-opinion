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

from .. import document
from ..config import settings
from ..database import get_db
from ..models import STATUS_ACTIVE, OpinionSubmission
from ..schemas import COMMENT_MAX, NAME_MAX, FormErrors, validate_submission
from ..security import client_ip, same_origin
from ..templating import format_kst, templates

router = APIRouter()
logger = logging.getLogger("opinion")

DUPLICATE_MESSAGE = "이미 해당 동·호수로 제출된 의견서가 있습니다."
TEMPORARY_FAILURE_MESSAGE = "일시적으로 제출하지 못했습니다. 잠시 후 다시 시도해 주세요."
BAD_REQUEST_MESSAGE = "잘못된 요청입니다. 페이지를 새로고침한 후 다시 시도해 주세요."
DOCUMENT_CHANGED_MESSAGE = (
    "작성하시는 동안 의견서 내용이 변경되었습니다. 페이지를 새로고침하여 변경된 내용을 확인한 뒤 다시 작성해 주세요."
)
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


@router.api_route("/opinion", methods=["GET", "HEAD"])
def opinion_form(request: Request, db: Session = Depends(get_db)):
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


@router.get("/opinion/complete")
def opinion_complete(request: Request, r: str = ""):
    try:
        receipt = _receipts.loads(r, max_age=RECEIPT_MAX_AGE)
        receipt_no = str(receipt["n"])
        submitted_at = datetime.fromisoformat(str(receipt["t"]))
    except (BadData, KeyError, TypeError, ValueError):
        return RedirectResponse("/opinion", status_code=303)
    context = {"receipt_no": receipt_no, "submitted_at": format_kst(submitted_at)}
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
