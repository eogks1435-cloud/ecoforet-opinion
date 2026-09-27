"""Admin editing of the opinion document: edit, preview, publish as a new version, and read old versions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import document
from ..database import get_db
from ..models import STATUS_ACTIVE, OpinionDocument, OpinionSubmission
from ..security import client_ip, csrf_token, csrf_valid, is_admin
from ..templating import templates
from .admin import FLASH_KEY, flash, to_login

router = APIRouter(prefix="/admin/document")
logger = logging.getLogger("opinion")

EXPIRED_MESSAGE = "보안 확인 시간이 지났습니다. 다시 시도해 주세요."


def _submission_counts(db: Session) -> tuple[dict[str, int], dict[str, int]]:
    """(valid, all) submission counts per document version."""
    all_counts = dict(
        db.execute(
            select(OpinionSubmission.document_version, func.count()).group_by(OpinionSubmission.document_version)
        ).all()
    )
    valid_counts = dict(
        db.execute(
            select(OpinionSubmission.document_version, func.count())
            .where(OpinionSubmission.status == STATUS_ACTIVE)
            .group_by(OpinionSubmission.document_version)
        ).all()
    )
    return valid_counts, all_counts


def _current(db: Session) -> OpinionDocument:
    current = document.active_document(db)
    if current is None:
        document.seed_initial_document(db)
        current = document.active_document(db)
    return current


def _editor(
    request: Request,
    db: Session,
    *,
    form: dict[str, str],
    base_version: str,
    errors: dict[str, str] | None = None,
    message: str = "",
    preview: document.DocumentView | None = None,
    status_code: int = 200,
):
    current = _current(db)
    valid_counts, all_counts = _submission_counts(db)
    versions = db.scalars(select(OpinionDocument).order_by(OpinionDocument.version_no.desc())).all()
    next_number = (db.scalar(select(func.max(OpinionDocument.version_no))) or 0) + 1
    context = {
        "active": "document",
        "csrf_token": csrf_token(request),
        "flash": request.session.pop(FLASH_KEY, None),
        "current": current,
        "current_valid": valid_counts.get(current.version, 0),
        "form": form,
        "base_version": base_version,
        "errors": errors or {},
        "message": message,
        "preview": preview,
        "next_version": document.next_version_label(current.version, next_number),
        "history": [(doc, valid_counts.get(doc.version, 0), all_counts.get(doc.version, 0)) for doc in versions],
    }
    return templates.TemplateResponse(request, "admin_document.html", context, status_code=status_code)


def _form(title: str, body: str, notes: str, recipient: str) -> dict[str, str]:
    return {"title": title, "body": body, "notes": notes, "recipient": recipient}


@router.get("")
def editor(request: Request, db: Session = Depends(get_db), source: str = Query("", alias="from")):
    if not is_admin(request):
        return to_login()
    current = _current(db)
    start = current
    if source:
        start = db.scalar(select(OpinionDocument).where(OpinionDocument.version == source)) or current
    return _editor(
        request, db, form=_form(start.title, start.body, start.notes, start.recipient), base_version=current.version
    )


@router.post("/preview")
def preview(
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(""),
    body: str = Form(""),
    notes: str = Form(""),
    recipient: str = Form(""),
    base_version: str = Form(""),
    csrf: str = Form(""),
):
    if not is_admin(request):
        return to_login()
    form = _form(title, body, notes, recipient)
    if not csrf_valid(request, csrf):
        return _editor(request, db, form=form, base_version=base_version, message=EXPIRED_MESSAGE, status_code=400)
    data, errors = document.clean_document(title, body, notes, recipient)
    if errors:
        return _editor(request, db, form=form, base_version=base_version, errors=errors, status_code=422)
    return _editor(
        request,
        db,
        form=_form(data.title, data.body, data.notes, data.recipient),
        base_version=base_version,
        preview=document.view_of(data, version="미리보기"),
    )


@router.post("/publish")
def publish(
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(""),
    body: str = Form(""),
    notes: str = Form(""),
    recipient: str = Form(""),
    base_version: str = Form(""),
    csrf: str = Form(""),
):
    if not is_admin(request):
        return to_login()
    form = _form(title, body, notes, recipient)
    if not csrf_valid(request, csrf):
        return _editor(request, db, form=form, base_version=base_version, message=EXPIRED_MESSAGE, status_code=400)
    data, errors = document.clean_document(title, body, notes, recipient)
    if errors:
        return _editor(request, db, form=form, base_version=base_version, errors=errors, status_code=422)
    try:
        new = document.publish_document(db, data, base_version, client_ip(request))
    except document.PublishError as exc:
        db.rollback()
        # After a conflict the editor restarts from the version now online, so a second click is a choice.
        base = _current(db).version if exc.code == "conflict" else base_version
        return _editor(request, db, form=form, base_version=base, message=str(exc), status_code=409)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("document publish failed")
        return _editor(
            request, db, form=form, base_version=base_version,
            message="게시하지 못했습니다. 잠시 후 다시 시도해 주세요.", status_code=503,
        )
    logger.info("opinion document published version=%s", new.version)
    flash(request, "success", f"{new.version} 버전을 게시했습니다. 주민 페이지에 바로 반영되었습니다.")
    return RedirectResponse("/admin/document", status_code=303)


@router.get("/versions/{version}")
def version_page(version: str, request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    record = db.scalar(select(OpinionDocument).where(OpinionDocument.version == version))
    if record is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    view = document.view_of(record)
    valid_counts, all_counts = _submission_counts(db)
    context = {
        "active": "document",
        "csrf_token": csrf_token(request),
        "record": record,
        "doc": view,
        "hash_ok": view.text_hash == record.text_hash,
        "valid_count": valid_counts.get(record.version, 0),
        "all_count": all_counts.get(record.version, 0),
    }
    return templates.TemplateResponse(request, "admin_document_version.html", context)
