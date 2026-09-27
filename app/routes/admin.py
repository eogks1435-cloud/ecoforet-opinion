"""Password-protected admin: counts, searchable list, signatures, invalidation and CSV export."""

from __future__ import annotations

import csv
import io
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import Select, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import document
from ..config import settings
from ..database import get_db
from ..models import (
    OPINION_AGREE,
    OPINION_DISAGREE,
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    OpinionDocument,
    OpinionSubmission,
)
from ..pdf import build_submission_pdf
from ..schemas import REASON_MAX, clean_text, parse_number
from ..security import (
    client_ip,
    csrf_token,
    csrf_valid,
    is_admin,
    login_throttle,
    password_matches,
    start_admin_session,
)
from ..templating import format_kst, templates, to_kst

router = APIRouter(prefix="/admin")
logger = logging.getLogger("opinion")

STATUS_LABEL = {STATUS_ACTIVE: "유효", STATUS_INVALIDATED: "무효"}
EXPORT_HEADER = ["번호", "동", "호수", "성명", "의견", "기타 의견", "제출일시", "의견서 버전", "상태", "제출번호"]
FLASH_KEY = "flash"
FAILED_LOGIN_DELAY = 0.5  # seconds; slows down password guessing


@dataclass(frozen=True)
class Filters:
    building: str = ""
    unit: str = ""
    name: str = ""
    opinion: str = ""
    status: str = ""

    @classmethod
    def from_query(cls, building: str, unit: str, name: str, opinion: str, status: str) -> Filters:
        return cls(
            building=building.strip()[:10],
            unit=unit.strip()[:10],
            name=" ".join(clean_text(name).split())[:30],
            opinion=opinion if opinion in (OPINION_AGREE, OPINION_DISAGREE) else "",
            status=status if status in (STATUS_ACTIVE, STATUS_INVALIDATED) else "",
        )

    def apply(self, stmt: Select) -> Select:
        if self.building:
            value, _problem = parse_number(self.building, "동")
            stmt = stmt.where(OpinionSubmission.building == (value or self.building))
        if self.unit:
            value, _problem = parse_number(self.unit, "호")
            stmt = stmt.where(OpinionSubmission.unit == (value or self.unit))
        if self.name:
            stmt = stmt.where(OpinionSubmission.resident_name.contains(self.name, autoescape=True))
        if self.opinion:
            stmt = stmt.where(OpinionSubmission.opinion_choice == self.opinion)
        if self.status:
            stmt = stmt.where(OpinionSubmission.status == self.status)
        return stmt

    def query_string(self) -> str:
        params = {key: value for key, value in asdict(self).items() if value}
        return "?" + urlencode(params) if params else ""


def flash(request: Request, kind: str, text: str) -> None:
    request.session[FLASH_KEY] = {"kind": kind, "text": text}


def to_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def _counts(db: Session) -> dict[str, int]:
    """Only ACTIVE submissions are counted; invalidated ones are reported separately."""
    by_choice = dict(
        db.execute(
            select(OpinionSubmission.opinion_choice, func.count())
            .where(OpinionSubmission.status == STATUS_ACTIVE)
            .group_by(OpinionSubmission.opinion_choice)
        ).all()
    )
    invalidated = db.scalar(
        select(func.count()).select_from(OpinionSubmission).where(OpinionSubmission.status == STATUS_INVALIDATED)
    )
    agree = int(by_choice.get(OPINION_AGREE, 0))
    disagree = int(by_choice.get(OPINION_DISAGREE, 0))
    return {"total": agree + disagree, "agree": agree, "disagree": disagree, "invalidated": int(invalidated or 0)}


def _detail(row: OpinionSubmission) -> dict[str, str]:
    status = STATUS_LABEL[row.status]
    if row.status == STATUS_INVALIDATED and row.invalidated_at:
        status += f" (처리 {format_kst(row.invalidated_at, '%Y-%m-%d %H:%M')})"
    return {
        "id": str(row.public_id),
        "title": f"{row.building}동 {row.unit}호 · {row.resident_name}",
        "opinion": f"{document.OPINION_EXPORT_LABEL[row.opinion_choice]} ({row.opinion_choice})",
        "submitted": format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S"),
        "number": row.receipt_no,
        "status": status,
        "reason": row.invalidated_reason or "",
        "version": row.document_version,
        "comment": row.additional_comment or "",
        "ip": row.ip_address or "",
        "ua": row.user_agent or "",
    }


def _csv_cell(value: str) -> str:
    """Keep spreadsheet apps from running resident text as a formula (CSV injection)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _login_page(request: Request, message: str = "", status_code: int = 200):
    context = {"csrf_token": csrf_token(request), "message": message, "admin_enabled": settings.admin_enabled}
    return templates.TemplateResponse(request, "admin_login.html", context, status_code=status_code)


@router.get("/login")
def login_form(request: Request):
    if is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return _login_page(request)


@router.post("/login")
def login(request: Request, password: str = Form(""), csrf: str = Form("")):
    if not settings.admin_enabled:
        return _login_page(request, "관리자 비밀번호(ADMIN_PASSWORD)가 설정되지 않아 로그인할 수 없습니다.", 503)
    if not csrf_valid(request, csrf):
        return _login_page(request, "보안 확인 시간이 지났습니다. 다시 로그인해 주세요.", 400)
    ip = client_ip(request) or "unknown"
    if login_throttle.blocked(ip):
        return _login_page(request, "로그인 시도가 너무 많습니다. 15분 후 다시 시도해 주세요.", 429)
    if not password_matches(password):
        login_throttle.record_failure(ip)
        time.sleep(FAILED_LOGIN_DELAY)
        logger.warning("admin login failed ip=%s", ip)
        return _login_page(request, "비밀번호가 올바르지 않습니다.", 401)
    login_throttle.reset(ip)
    start_admin_session(request)
    logger.info("admin login ip=%s", ip)
    return RedirectResponse("/admin", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if csrf_valid(request, csrf):
        request.session.clear()
    return to_login()


@router.get("")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    building: str = "",
    unit: str = "",
    name: str = "",
    opinion: str = "",
    status: str = "",
):
    if not is_admin(request):
        return to_login()
    filters = Filters.from_query(building, unit, name, opinion, status)
    rows = db.scalars(
        filters.apply(select(OpinionSubmission)).order_by(
            OpinionSubmission.submitted_at.desc(), OpinionSubmission.id.desc()
        )
    ).all()
    context = {
        "active": "submissions",
        "items": [(row, _detail(row)) for row in rows],
        "counts": _counts(db),
        "filters": filters,
        "flash": request.session.pop(FLASH_KEY, None),
        "csrf_token": csrf_token(request),
        "opinion_labels": document.OPINION_ADMIN_LABEL,
        "status_labels": STATUS_LABEL,
        "current_url": "/admin" + filters.query_string(),
        "export_url": "/admin/export.csv" + filters.query_string(),
        "current_document": document.active_document(db),
    }
    return templates.TemplateResponse(request, "admin_dashboard.html", context)


@router.get("/signatures/{public_id}.png")
def signature_image(public_id: uuid.UUID, request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        return Response(status_code=401)
    data = db.scalar(select(OpinionSubmission.signature_data).where(OpinionSubmission.public_id == public_id))
    if data is None:
        return Response(status_code=404)
    return Response(content=bytes(data), media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/submissions/{public_id}/pdf")
def submission_pdf(public_id: uuid.UUID, request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        return to_login()
    row = db.scalar(select(OpinionSubmission).where(OpinionSubmission.public_id == public_id))
    if row is None:
        return Response(status_code=404)
    doc = db.scalar(select(OpinionDocument).where(OpinionDocument.version == row.document_version))
    filename = f"opinion_{row.building}-{row.unit}_{row.receipt_no}.pdf"
    return Response(
        build_submission_pdf(row, doc),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/submissions/{public_id}/invalidate")
def invalidate(
    public_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    csrf: str = Form(""),
    reason: str = Form(""),
    back: str = Form(""),
):
    if not is_admin(request):
        return to_login()
    target = back if back == "/admin" or back.startswith("/admin?") else "/admin"
    if not csrf_valid(request, csrf):
        flash(request, "error", "보안 확인 시간이 지났습니다. 다시 시도해 주세요.")
        return RedirectResponse(target, status_code=303)

    row = db.scalar(select(OpinionSubmission).where(OpinionSubmission.public_id == public_id).with_for_update())
    if row is None:
        flash(request, "error", "해당 제출 건을 찾을 수 없습니다.")
    elif row.status == STATUS_INVALIDATED:
        flash(request, "info", "이미 무효 처리된 제출입니다.")
    else:
        row.status = STATUS_INVALIDATED  # the row is kept; only its status changes
        row.invalidated_at = datetime.now(timezone.utc)
        row.invalidated_reason = " ".join(clean_text(reason).split())[:REASON_MAX] or None
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception("invalidation failed")
            flash(request, "error", "처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")
            return RedirectResponse(target, status_code=303)
        logger.info("opinion submission invalidated receipt=%s", row.receipt_no)
        flash(
            request,
            "success",
            f"{row.building}동 {row.unit}호 제출을 무효 처리했습니다. 이 동·호수는 다시 제출할 수 있습니다.",
        )
    return RedirectResponse(target, status_code=303)


@router.get("/export.csv")
def export_csv(
    request: Request,
    db: Session = Depends(get_db),
    building: str = "",
    unit: str = "",
    name: str = "",
    opinion: str = "",
    status: str = "",
):
    if not is_admin(request):
        return to_login()
    filters = Filters.from_query(building, unit, name, opinion, status)
    rows = db.scalars(
        filters.apply(select(OpinionSubmission)).order_by(
            OpinionSubmission.submitted_at.asc(), OpinionSubmission.id.asc()
        )
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPORT_HEADER)
    for number, row in enumerate(rows, start=1):
        writer.writerow(
            [
                number,
                row.building,
                row.unit,
                _csv_cell(row.resident_name),
                document.OPINION_EXPORT_LABEL[row.opinion_choice],
                _csv_cell(row.additional_comment or ""),
                format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S"),
                row.document_version,
                STATUS_LABEL[row.status],
                row.receipt_no,
            ]
        )
    stamp = to_kst(datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M")
    body = "﻿" + buffer.getvalue()  # UTF-8 BOM so Excel shows Korean correctly
    return Response(
        body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="opinion_submissions_{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )
