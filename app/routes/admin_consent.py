"""Admin for agendas and multi-question consent forms.

/admin/agendas                       all agendas: which one /opinion shows, accepting on/off, new agenda
/admin/consent/{code}                counts per question and per recipient, filtered list, exports
/admin/consent/{code}/edit           draft editor -> full preview -> checks -> publish as a new version
/admin/consent/{code}/versions/{v}   a published version exactly as residents saw it
/admin/consent-submissions/{id}      one record: answers, consents, signature, status actions, PDFs
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import Select, exists, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .. import consent, consent_pdf
from ..database import get_db
from ..models import (
    AGENDA_CONSENT,
    AGENDA_LEGACY,
    OPINION_AGREE,
    OPINION_DISAGREE,
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    STATUS_WITHDRAWN,
    Agenda,
    ConsentAnswer,
    ConsentProvision,
    ConsentSubmission,
    ConsentVersion,
    OpinionSubmission,
)
from ..schemas import REASON_MAX, clean_text, parse_number
from ..security import client_ip, csrf_token, csrf_valid, is_admin
from ..templating import format_kst, templates, to_kst
from .admin import FLASH_KEY, _csv_cell, _pdf_slots, flash, to_login
from .public import consent_page_context

router = APIRouter(prefix="/admin")
logger = logging.getLogger("opinion")

EXPIRED_MESSAGE = "보안 확인 시간이 지났습니다. 다시 시도해 주세요."
FAILED_MESSAGE = "처리하지 못했습니다. 잠시 후 다시 시도해 주세요."
PROVISION_FILTERS = {
    "agreed": "제공 동의(철회 제외)",
    "declined": "제공 동의하지 않음",
    "withdrawn": "제공 동의 후 철회",
    "eligible": "제출 파일 포함 대상",
}


# ------------------------------------------------------------------------------------------------ helpers
def _consent_agenda(db: Session, code: str) -> Agenda | None:
    agenda = consent.agenda_by_code(db, code)
    return agenda if agenda is not None and agenda.kind == AGENDA_CONSENT else None


def _primary_consent(db: Session) -> Agenda | None:
    """The consent agenda the menu opens: the public one, else the newest."""
    public = consent.public_agenda(db)
    if public is not None and public.kind == AGENDA_CONSENT:
        return public
    return db.scalar(select(Agenda).where(Agenda.kind == AGENDA_CONSENT).order_by(Agenda.id.desc()))


def _not_found(request: Request):
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)


def _reason(value: str) -> str:
    return " ".join(clean_text(value).split())[:REASON_MAX]


def _local_back(value: str, default: str) -> str:
    """Only admin pages of this site are valid return addresses."""
    return value if value.startswith("/admin/") and "//" not in value and "\\" not in value else default


def _pdf_response(content: bytes, filename: str) -> Response:
    return Response(
        content, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"},
    )


def _busy() -> Response:
    return Response("PDF 요청이 많습니다. 잠시 후 다시 열어 주세요.", status_code=503, media_type="text/plain; charset=utf-8")


def _csv_response(rows: list[list[object]], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(rows)
    return Response(
        ("﻿" + buffer.getvalue()).encode("utf-8"),  # UTF-8 BOM so Excel shows Korean correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def _stamp() -> str:
    return to_kst(datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M")


def _answer_label(value: str | None) -> str:
    return consent.ANSWER_LABEL.get(value or "", "해당 없음")


def _overseas_label(row: ConsentSubmission) -> str:
    """동의 / 해당 없음 (the version did not ask) / 기록 없음 (asked, but taken by older code after a rollback)."""
    if row.overseas_consent is not None:
        return "동의" if row.overseas_consent else "동의하지 않음"
    asked = bool((row.version.content.get("overseas") or {}).get("enabled"))
    return "기록 없음" if asked else "해당 없음"


def _provision_label(provision: ConsentProvision | None) -> str:
    if provision is None:
        return "해당 없음"
    if not provision.agreed:
        return "동의하지 않음"
    if provision.withdrawn_at is not None:
        return f"동의 후 철회({format_kst(provision.withdrawn_at, '%Y-%m-%d')})"
    return "동의"


# ---------------------------------------------------------------------------------------------- agendas
def _agenda_rows(db: Session) -> list[dict]:
    rows = []
    for agenda in db.scalars(select(Agenda).order_by(Agenda.id)).all():
        row: dict = {"agenda": agenda}
        if agenda.kind == AGENDA_LEGACY:
            counts = dict(
                db.execute(select(OpinionSubmission.status, func.count()).group_by(OpinionSubmission.status)).all()
            )
            row.update(
                valid=int(counts.get(STATUS_ACTIVE, 0)),
                total=int(sum(counts.values())),
                version_label="",
                blockers=[],
                has_version=True,
            )
        else:
            counts = dict(
                db.execute(
                    select(ConsentSubmission.status, func.count())
                    .where(ConsentSubmission.agenda_id == agenda.id)
                    .group_by(ConsentSubmission.status)
                ).all()
            )
            version = consent.current_version(db, agenda)
            draft = consent.draft_of(db, agenda)
            row.update(
                valid=int(counts.get(STATUS_ACTIVE, 0)),
                total=int(sum(counts.values())),
                version_label=version.label if version else "",
                has_version=version is not None,
                blockers=consent.publish_blockers(version.content) if version else [],
                draft_blockers=consent.publish_blockers(draft.content) if draft else ["초안 없음"],
            )
        rows.append(row)
    return rows


@router.get("/agendas")
def agendas_page(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    rows = _agenda_rows(db)
    context = {
        "active": "agendas",
        "csrf_token": csrf_token(request),
        "flash": request.session.pop(FLASH_KEY, None),
        "rows": rows,
        "public": next((row for row in rows if row["agenda"].is_public), None),
        "consent_agendas": [row["agenda"] for row in rows if row["agenda"].kind == AGENDA_CONSENT],
    }
    return templates.TemplateResponse(request, "admin_agendas.html", context)


@router.post("/agendas/{code}/public")
def agenda_make_public(code: str, request: Request, db: Session = Depends(get_db), csrf: str = Form("")):
    if not is_admin(request):
        return to_login()
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse("/admin/agendas", status_code=303)
    agenda = consent.agenda_by_code(db, code)
    if agenda is None:
        flash(request, "error", "해당 안건을 찾을 수 없습니다.")
        return RedirectResponse("/admin/agendas", status_code=303)
    try:
        consent.make_public(db, agenda)
    except consent.PublishError as exc:
        db.rollback()
        flash(request, "error", str(exc))
        return RedirectResponse("/admin/agendas", status_code=303)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("agenda switch failed")
        flash(request, "error", FAILED_MESSAGE)
        return RedirectResponse("/admin/agendas", status_code=303)
    logger.info("public agenda switched to %s", agenda.code)
    state = "접수 중" if agenda.accepting else "접수 중지 상태"
    flash(request, "success", f"주민 페이지(/opinion)가 '{agenda.name}' 안건을 보여 줍니다({state}). "
                              "다른 안건의 기록은 그대로 보관되어 있습니다.")
    return RedirectResponse("/admin/agendas", status_code=303)


@router.post("/agendas/{code}/accepting")
def agenda_accepting(
    code: str, request: Request, db: Session = Depends(get_db), csrf: str = Form(""), value: str = Form("")
):
    if not is_admin(request):
        return to_login()
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse("/admin/agendas", status_code=303)
    agenda = consent.agenda_by_code(db, code)
    if agenda is None:
        flash(request, "error", "해당 안건을 찾을 수 없습니다.")
        return RedirectResponse("/admin/agendas", status_code=303)
    try:
        consent.set_accepting(db, agenda, value == "1")
    except SQLAlchemyError:
        db.rollback()
        logger.exception("accepting switch failed")
        flash(request, "error", FAILED_MESSAGE)
        return RedirectResponse("/admin/agendas", status_code=303)
    logger.info("agenda %s accepting=%s", agenda.code, agenda.accepting)
    flash(request, "success", f"'{agenda.name}' 접수를 {'다시 열었습니다' if agenda.accepting else '중지했습니다'}. "
                              "이미 접수된 기록은 그대로입니다.")
    return RedirectResponse("/admin/agendas", status_code=303)


@router.post("/agendas/new")
def agenda_new(
    request: Request, db: Session = Depends(get_db), csrf: str = Form(""), name: str = Form(""),
    source: str = Form(""),
):
    if not is_admin(request):
        return to_login()
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse("/admin/agendas", status_code=303)
    content = consent.seed_content()
    base = _consent_agenda(db, source) if source else None
    if base is not None:  # start from that agenda's draft wording (its records are not copied)
        draft = consent.draft_of(db, base)
        version = consent.current_version(db, base)
        content = draft.content if draft else (version.content if version else content)
    try:
        agenda = consent.create_agenda(db, name, content)
    except consent.PublishError as exc:
        db.rollback()
        flash(request, "error", str(exc))
        return RedirectResponse("/admin/agendas", status_code=303)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("agenda creation failed")
        flash(request, "error", FAILED_MESSAGE)
        return RedirectResponse("/admin/agendas", status_code=303)
    logger.info("agenda created code=%s", agenda.code)
    flash(request, "success", f"새 안건 '{agenda.name}'({agenda.code})을 만들었습니다. 아직 게시·공개되지 않았습니다.")
    return RedirectResponse(f"/admin/consent/{agenda.code}/edit", status_code=303)


@router.get("/consent")
def consent_home(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    agenda = _primary_consent(db)
    return RedirectResponse(f"/admin/consent/{agenda.code}" if agenda else "/admin/agendas", status_code=303)


@router.get("/consent/edit")
def consent_edit_home(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    agenda = _primary_consent(db)
    return RedirectResponse(f"/admin/consent/{agenda.code}/edit" if agenda else "/admin/agendas", status_code=303)


# ------------------------------------------------------------------------------------ dashboard and list
@dataclass(frozen=True)
class ConsentFilters:
    building: str = ""
    unit: str = ""
    name: str = ""
    status: str = ""
    version: str = ""
    question: str = ""
    answer: str = ""
    recipient: str = ""
    provision: str = ""

    @classmethod
    def from_query(cls, params: dict[str, str], question_keys: set[str], recipient_keys: set[str],
                   version_labels: set[str]) -> ConsentFilters:
        get = lambda key: (params.get(key) or "").strip()  # noqa: E731
        question = get("question") if get("question") in question_keys else ""
        recipient = get("recipient") if get("recipient") in recipient_keys else ""
        return cls(
            building=get("building")[:10],
            unit=get("unit")[:10],
            name=" ".join(clean_text(get("name")).split())[:30],
            status=get("status") if get("status") in consent.STATUS_LABEL else "",
            version=get("version") if get("version") in version_labels else "",
            question=question,
            answer=get("answer") if question and get("answer") in (OPINION_AGREE, OPINION_DISAGREE) else "",
            recipient=recipient,
            provision=get("provision") if recipient and get("provision") in PROVISION_FILTERS else "",
        )

    def apply(self, stmt: Select) -> Select:
        if self.building:
            value, _problem = parse_number(self.building, "동")
            stmt = stmt.where(ConsentSubmission.building == (value or self.building))
        if self.unit:
            value, _problem = parse_number(self.unit, "호")
            stmt = stmt.where(ConsentSubmission.unit == (value or self.unit))
        if self.name:
            stmt = stmt.where(ConsentSubmission.resident_name.contains(self.name, autoescape=True))
        if self.status:
            stmt = stmt.where(ConsentSubmission.status == self.status)
        if self.version:
            stmt = stmt.where(ConsentSubmission.version_label == self.version)
        if self.question and self.answer:
            stmt = stmt.where(
                exists().where(
                    ConsentAnswer.submission_id == ConsentSubmission.id,
                    ConsentAnswer.question_key == self.question,
                    ConsentAnswer.answer == self.answer,
                )
            )
        if self.recipient and self.provision:
            provision = [ConsentProvision.submission_id == ConsentSubmission.id,
                         ConsentProvision.recipient_key == self.recipient]
            if self.provision == "declined":
                provision.append(ConsentProvision.agreed.is_(False))
            elif self.provision == "withdrawn":
                provision += [ConsentProvision.agreed.is_(True), ConsentProvision.withdrawn_at.is_not(None)]
            else:
                provision += [ConsentProvision.agreed.is_(True), ConsentProvision.withdrawn_at.is_(None)]
            stmt = stmt.where(exists().where(*provision))
            if self.provision == "eligible":
                stmt = stmt.where(
                    ConsentSubmission.status == STATUS_ACTIVE,
                    exists().where(
                        ConsentAnswer.submission_id == ConsentSubmission.id, ConsentAnswer.answer == OPINION_AGREE
                    ),
                )
        return stmt

    def query_string(self) -> str:
        params = {key: value for key, value in asdict(self).items() if value}
        return "?" + urlencode(params) if params else ""


def _filters(request: Request, db: Session, agenda: Agenda) -> tuple[ConsentFilters, list, list, list[str]]:
    questions = consent.question_catalog(db, agenda)
    recipients = consent.recipient_catalog(db, agenda)
    labels = db.scalars(
        select(ConsentVersion.label).where(ConsentVersion.agenda_id == agenda.id).order_by(ConsentVersion.version_no)
    ).all()
    filters = ConsentFilters.from_query(
        dict(request.query_params), {q.key for q in questions}, {r.key for r in recipients}, set(labels)
    )
    return filters, questions, recipients, list(labels)


def _short_names(questions: list) -> dict[str, str]:
    """Q1, Q2... for the current questions (display order); older questions keep their key."""
    active = [q.key for q in questions if q.active]
    return {q.key: f"Q{active.index(q.key) + 1}" if q.active else f"이전 {q.key}" for q in questions}


def _submissions(db: Session, agenda: Agenda, filters: ConsentFilters, newest_first: bool = True):
    order = (ConsentSubmission.submitted_at.desc(), ConsentSubmission.id.desc()) if newest_first else (
        ConsentSubmission.submitted_at.asc(), ConsentSubmission.id.asc())
    stmt = filters.apply(select(ConsentSubmission).where(ConsentSubmission.agenda_id == agenda.id))
    return db.scalars(stmt.order_by(*order)).all()


@router.get("/consent/{code}")
def consent_dashboard(code: str, request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return _not_found(request)
    filters, questions, recipients, labels = _filters(request, db, agenda)
    rows = _submissions(db, agenda, filters)
    query = filters.query_string()
    stats_version = consent.version_by_label(db, agenda, filters.version) if filters.version else None
    access_rows, access_oldest = db.execute(
        select(func.count(), func.min(ConsentSubmission.submitted_at)).where(
            ConsentSubmission.agenda_id == agenda.id,
            (ConsentSubmission.ip_address.is_not(None)) | (ConsentSubmission.user_agent.is_not(None)),
        )
    ).one()
    context = {
        "active": "consent",
        "csrf_token": csrf_token(request),
        "flash": request.session.pop(FLASH_KEY, None),
        "agenda": agenda,
        "version": consent.current_version(db, agenda),
        "stats": consent.consent_stats(db, agenda, stats_version),
        "stats_version": stats_version,
        "access_rows": int(access_rows or 0),
        "access_oldest": access_oldest,
        "filters": filters,
        "question_options": questions,
        "question_short": _short_names(questions),
        "recipient_options": recipients,
        "version_labels": labels,
        "provision_filters": PROVISION_FILTERS,
        "status_labels": consent.STATUS_LABEL,
        "answer_labels": consent.ANSWER_LABEL,
        "rows": rows,
        "current_url": f"/admin/consent/{agenda.code}{query}",
        "export_url": f"/admin/consent/{agenda.code}/export.csv{query}",
    }
    return templates.TemplateResponse(request, "admin_consent.html", context)


@router.get("/consent/{code}/export.csv")
def consent_export(code: str, request: Request, db: Session = Depends(get_db)):
    """Internal list (admin only): every record with its status. No IP or browser data."""
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return Response(status_code=404)
    filters, questions, recipients, _labels = _filters(request, db, agenda)
    rows = _submissions(db, agenda, filters, newest_first=False)
    header = ["번호", "동", "호수", "성명"] + [q.title for q in questions] + ["개인정보 수집·이용", "개인정보 국외 이전"]
    header += [f"{r.short_name} 제공" for r in recipients]
    header += ["최종 확인", "제출일시(한국시간)", "문서 버전", "본문 확인값(SHA-256)", "상태", "상태 사유", "상태 처리일시",
               "제출처 전달 후 처리", "제출번호"]
    out: list[list[object]] = [header]
    for number, row in enumerate(rows, start=1):
        answers, provisions = row.answer_map(), row.provision_map()
        out.append(
            [number, row.building, row.unit, _csv_cell(row.resident_name)]
            + [_answer_label(answers.get(q.key)) for q in questions]
            + ["동의" if row.privacy_consent else "동의하지 않음", _overseas_label(row)]
            + [_provision_label(provisions.get(r.key)) for r in recipients]
            + [
                "확인함" if row.final_confirmed else "",
                format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S"),
                row.version_label,
                row.content_hash,
                consent.STATUS_LABEL[row.status],
                _csv_cell(row.status_reason or ""),
                format_kst(row.status_changed_at, "%Y-%m-%d %H:%M:%S"),
                "예" if row.already_delivered else "",
                f'="{row.receipt_no}"',  # Excel would turn e.g. 00123456 or 1234E567 into a number
            ]
        )
    return _csv_response(out, f"consent_{agenda.code}_internal_{_stamp()}.csv")


def _recipient(db: Session, agenda: Agenda, key: str) -> consent.RecipientCount | None:
    return next((r for r in consent.recipient_catalog(db, agenda) if r.key == key), None)


@router.get("/consent/{code}/recipients/{key}.csv")
def recipient_export(code: str, key: str, request: Request, db: Session = Depends(get_db)):
    """For one recipient: valid records that agreed to this recipient and to at least one request.

    Each person's actual answer to every question; nothing about other recipients, no IP or browser data.
    """
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    recipient = _recipient(db, agenda, key) if agenda else None
    if recipient is None:
        return Response(status_code=404)
    questions = consent.question_catalog(db, agenda)
    rows = consent.eligible_submissions(db, agenda, key)
    out: list[list[object]] = [
        ["번호", "동", "호수", "성명"] + [q.title for q in questions]
        + ["제출일시(한국시간)", "문서 버전", "제출번호"]
    ]
    for number, row in enumerate(rows, start=1):
        answers = row.answer_map()
        out.append(
            [number, row.building, row.unit, _csv_cell(row.resident_name)]
            + [_answer_label(answers.get(q.key)) for q in questions]
            + [format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S"), row.version_label, f'="{row.receipt_no}"']
        )
    logger.info("recipient csv agenda=%s recipient=%s rows=%d", agenda.code, key, len(rows))
    return _csv_response(out, f"consent_{agenda.code}_{key}_{_stamp()}.csv")


@router.get("/consent/{code}/recipients/{key}.pdf")
def recipient_pdf(code: str, key: str, request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    if not _pdf_slots.acquire(timeout=20):
        return _busy()
    try:
        agenda = _consent_agenda(db, code)
        recipient = _recipient(db, agenda, key) if agenda else None
        if recipient is None:
            return Response(status_code=404)
        rows = consent.eligible_submissions(db, agenda, key)
        content = consent_pdf.build_recipient_list_pdf(
            agenda, key, rows, consent.question_catalog(db, agenda), consent.current_version(db, agenda)
        )
    finally:
        _pdf_slots.release()
    logger.info("recipient pdf agenda=%s recipient=%s rows=%d", agenda.code, key, len(rows))
    return _pdf_response(content, f"consent_{agenda.code}_{key}_{_stamp()}.pdf")


@router.post("/consent/{code}/access-info/purge")
def access_info_purge(code: str, request: Request, db: Session = Depends(get_db), csrf: str = Form(""),
                      days: str = Form("")):
    """Carry out the retention period of IP/browser data: erase it from records older than N days."""
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return _not_found(request)
    target = f"/admin/consent/{agenda.code}"
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse(target, status_code=303)
    if not days.strip().isdigit() or not 1 <= int(days) <= 3650:
        flash(request, "error", "경과 일수를 1 이상의 숫자로 입력해 주세요.")
        return RedirectResponse(target, status_code=303)
    try:
        count = consent.purge_access_info(db, agenda, int(days))
    except SQLAlchemyError:
        db.rollback()
        logger.exception("access info purge failed")
        flash(request, "error", FAILED_MESSAGE)
        return RedirectResponse(target, status_code=303)
    logger.info("access info purged agenda=%s days=%s rows=%d", agenda.code, days, count)
    flash(request, "success", f"제출 후 {int(days)}일이 지난 {count}건의 접속 IP·브라우저 정보를 지웠습니다. "
                              "이전에 만든 백업에는 백업 보관기간이 끝날 때까지 남아 있습니다.")
    return RedirectResponse(target, status_code=303)


@router.get("/consent/{code}/blank.pdf")
def blank_form_pdf(code: str, request: Request, db: Session = Depends(get_db), version: str = "", draft: str = ""):
    """Printable paper form of a published version (default: the current one), or of the draft (preview only)."""
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return Response(status_code=404)
    if draft == "1":
        stored = consent.draft_of(db, agenda)
        if stored is None:
            return Response(status_code=404)
        content = consent.normalize_content(stored.content)
        label, digest = "초안", consent.content_hash(content)
    else:
        record = consent.version_by_label(db, agenda, version) if version else consent.current_version(db, agenda)
        if record is None:
            return Response("게시된 버전이 없습니다. 먼저 동의서 문구를 게시하거나, 편집 화면의 초안 서면 미리보기를 이용해 주세요.",
                            status_code=404, media_type="text/plain; charset=utf-8")
        content, label, digest = record.content, record.label, record.content_hash
    if not _pdf_slots.acquire(timeout=20):
        return _busy()
    try:
        pdf = consent_pdf.build_blank_form_pdf(content, label, digest, draft=draft == "1")
    finally:
        _pdf_slots.release()
    suffix = "draft" if draft == "1" else label
    return _pdf_response(pdf, f"consent_{agenda.code}_paper_{suffix}.pdf")


# ------------------------------------------------------------------------------------------ one record
def _record(db: Session, public_id: uuid.UUID) -> ConsentSubmission | None:
    return db.scalar(select(ConsentSubmission).where(ConsentSubmission.public_id == public_id))


@router.get("/consent-submissions/{public_id}")
def record_page(public_id: uuid.UUID, request: Request, db: Session = Depends(get_db), back: str = ""):
    if not is_admin(request):
        return to_login()
    row = _record(db, public_id)
    if row is None:
        return _not_found(request)
    agenda = db.get(Agenda, row.agenda_id)
    content = row.version.content
    answers, provisions = row.answer_map(), row.provision_map()
    asked = consent.active_questions(content)
    context = {
        "active": "consent",
        "csrf_token": csrf_token(request),
        "flash": request.session.pop(FLASH_KEY, None),
        "row": row,
        "agenda": agenda,
        "back": _local_back(back, f"/admin/consent/{agenda.code}"),
        "self_url": request.url.path + ("?" + urlencode({"back": back}) if back else ""),
        "answers": [
            (f"질문 {i}. {q['title']}", q["text"], _answer_label(answers.get(q["key"])), answers.get(q["key"]))
            for i, q in enumerate(asked, start=1)
        ],
        "recipients": [
            (r, provisions.get(r["key"]), consent.eligible_for(row, r["key"])) for r in content["recipients"]
        ],
        "status_labels": consent.STATUS_LABEL,
        "overseas_label": _overseas_label(row),
    }
    return templates.TemplateResponse(request, "admin_consent_record.html", context)


@router.get("/consent-submissions/{public_id}/signature.png")
def record_signature(public_id: uuid.UUID, request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        return Response(status_code=401)
    data = db.scalar(select(ConsentSubmission.signature_data).where(ConsentSubmission.public_id == public_id))
    if data is None:
        return Response(status_code=404)
    return Response(content=bytes(data), media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/consent-submissions/{public_id}/pdf")
def record_pdf(public_id: uuid.UUID, request: Request, db: Session = Depends(get_db)) -> Response:
    """Internal copy: the wording as signed, every answer and consent, status and reason."""
    if not is_admin(request):
        return to_login()
    if not _pdf_slots.acquire(timeout=20):
        return _busy()
    try:
        row = _record(db, public_id)
        if row is None:
            return Response(status_code=404)
        content = consent_pdf.build_consent_pdf(row)
    finally:
        _pdf_slots.release()
    return _pdf_response(content, f"consent_{row.building}-{row.unit}_{row.receipt_no}.pdf")


@router.get("/consent-submissions/{public_id}/pdf/{key}")
def record_recipient_pdf(public_id: uuid.UUID, key: str, request: Request, db: Session = Depends(get_db)):
    """Copy for one recipient; only for a record that may go to that recipient."""
    if not is_admin(request):
        return to_login()
    if not _pdf_slots.acquire(timeout=20):
        return _busy()
    try:
        row = _record(db, public_id)
        if row is None or key not in {r["key"] for r in row.version.content["recipients"]}:
            return Response(status_code=404)
        if not consent.eligible_for(row, key):
            return Response(
                "이 기록은 해당 제출처용 자료에 포함할 수 없습니다 (무효·철회, 제공 비동의·철회 또는 동의한 요청 없음).",
                status_code=409, media_type="text/plain; charset=utf-8",
            )
        content = consent_pdf.build_consent_pdf(row, recipient_key=key)
    finally:
        _pdf_slots.release()
    return _pdf_response(content, f"consent_{key}_{row.building}-{row.unit}_{row.receipt_no}.pdf")


@router.post("/consent-submissions/{public_id}/status")
def record_status(
    public_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    csrf: str = Form(""),
    action: str = Form(""),
    reason: str = Form(""),
    delivered: str = Form(""),
    back: str = Form(""),
):
    if not is_admin(request):
        return to_login()
    target = _local_back(back, "/admin/consent")
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse(target, status_code=303)
    status = {"invalidate": STATUS_INVALIDATED, "withdraw": STATUS_WITHDRAWN}.get(action)
    reason = _reason(reason)
    row = db.scalar(
        select(ConsentSubmission).where(ConsentSubmission.public_id == public_id).with_for_update(of=ConsentSubmission)
    )
    if row is None or status is None:
        flash(request, "error", "해당 제출 건을 찾을 수 없습니다.")
    elif row.status != STATUS_ACTIVE:
        flash(request, "info", f"이미 {consent.STATUS_LABEL[row.status]} 처리된 제출입니다.")
    elif not reason:
        flash(request, "error", "처리 사유를 입력해 주세요.")
    else:
        try:
            consent.change_status(db, row, status, reason, delivered == "1")
        except SQLAlchemyError:
            db.rollback()
            logger.exception("consent status change failed")
            flash(request, "error", FAILED_MESSAGE)
            return RedirectResponse(target, status_code=303)
        logger.info("consent submission %s receipt=%s", status.lower(), row.receipt_no)
        flash(request, "success", f"{row.building}동 {row.unit}호 제출을 {consent.STATUS_LABEL[status]} 처리했습니다. "
                                  "집계와 제출처 자료에서 빠지고, 이 안건에 같은 동·호수로 다시 제출할 수 있습니다.")
    return RedirectResponse(target, status_code=303)


@router.post("/consent-submissions/{public_id}/provisions/{key}/withdraw")
def record_provision_withdraw(
    public_id: uuid.UUID,
    key: str,
    request: Request,
    db: Session = Depends(get_db),
    csrf: str = Form(""),
    reason: str = Form(""),
    delivered: str = Form(""),
    back: str = Form(""),
):
    if not is_admin(request):
        return to_login()
    target = _local_back(back, "/admin/consent")
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse(target, status_code=303)
    reason = _reason(reason)
    provision = db.scalar(
        select(ConsentProvision)
        .join(ConsentSubmission, ConsentSubmission.id == ConsentProvision.submission_id)
        .where(ConsentSubmission.public_id == public_id, ConsentProvision.recipient_key == key)
        .with_for_update(of=ConsentProvision)
    )
    if provision is None:
        flash(request, "error", "해당 제공 동의를 찾을 수 없습니다.")
    elif not provision.effective:
        flash(request, "info", "이미 제공 대상이 아닌 기록입니다(동의하지 않음 또는 철회됨).")
    elif not reason:
        flash(request, "error", "철회 사유를 입력해 주세요.")
    else:
        try:
            consent.withdraw_provision(db, provision, reason, delivered == "1")
        except SQLAlchemyError:
            db.rollback()
            logger.exception("provision withdrawal failed")
            flash(request, "error", FAILED_MESSAGE)
            return RedirectResponse(target, status_code=303)
        logger.info("consent provision withdrawn recipient=%s", key)
        flash(request, "success", "제공 동의 철회를 기록했습니다. 이 제출처의 기명 동의자 수와 제출 파일에서 빠집니다.")
    return RedirectResponse(target, status_code=303)


# ---------------------------------------------------------------------------------- wording and versions
def _editor_context(request: Request, db: Session, agenda: Agenda, content: dict, *, message: str = "",
                    stamp: str = "") -> dict:
    version = consent.current_version(db, agenda)
    versions = db.scalars(
        select(ConsentVersion).where(ConsentVersion.agenda_id == agenda.id).order_by(ConsentVersion.version_no.desc())
    ).all()
    counts = dict(
        db.execute(
            select(ConsentSubmission.version_label, func.count())
            .where(ConsentSubmission.agenda_id == agenda.id, ConsentSubmission.status == STATUS_ACTIVE)
            .group_by(ConsentSubmission.version_label)
        ).all()
    )
    digest = consent.content_hash(content)
    reworded = consent.reworded_questions(version.content if version else None, content)
    missing = {"/".join(path) for path, _label in consent.required_settings(content)
               if not consent._get(content, path).strip()}
    if content["privacy"]["collect_access_info"] and not content["privacy"]["retention_access"].strip():
        missing.add("privacy/retention_access")
    return {
        "active": "consent_edit",
        "csrf_token": csrf_token(request),
        "flash": request.session.pop(FLASH_KEY, None),
        "message": message,
        "agenda": agenda,
        "c": content,
        "p": content["privacy"],
        "blockers": consent.publish_blockers(content),
        "reworded": reworded,
        "missing": missing,
        "draft_hash": digest,
        "draft_stamp": stamp,
        "version": version,
        "same_as_version": version is not None and version.content_hash == digest,
        "next_label": f"{agenda.code}_V{(version.version_no if version else 0) + 1}",
        "history": [(v, int(counts.get(v.label, 0))) for v in versions],
        "is_public": agenda.is_public,
        "section_slots": list(enumerate(content["sections"])) + [(len(content["sections"]), None)],
        "question_slots": list(enumerate(content["questions"])) + (
            [(len(content["questions"]), None)] if len(content["questions"]) < consent.QUESTIONS_MAX else []),
        "recipient_slots": list(enumerate(content["recipients"])) + (
            [(len(content["recipients"]), None)] if len(content["recipients"]) < consent.RECIPIENTS_MAX else []),
        "provided_items": consent.SEED_CONTENT["recipients"][0]["items"],
    }


def _draft_stamp(draft) -> str:
    return draft.updated_at.isoformat() if draft is not None and draft.updated_at else ""


@router.get("/consent/{code}/edit")
def consent_editor(code: str, request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return _not_found(request)
    draft = consent.draft_of(db, agenda)
    version = consent.current_version(db, agenda)
    content = consent.normalize_content(draft.content if draft else (version.content if version else consent.SEED_CONTENT))
    context = _editor_context(request, db, agenda, content, stamp=_draft_stamp(draft))
    return templates.TemplateResponse(request, "admin_consent_edit.html", context)


async def _form_fields(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


@router.post("/consent/{code}/draft")
def consent_save_draft(code: str, request: Request, db: Session = Depends(get_db),
                       fields: dict[str, str] = Depends(_form_fields)):
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return _not_found(request)
    draft = consent.draft_of(db, agenda)
    base = draft.content if draft else consent.SEED_CONTENT
    published = consent.current_version(db, agenda)
    content = consent.content_from_form(fields, base, consent.used_question_keys(db, agenda),
                                        published.content if published else None,
                                        consent.used_recipient_keys(db, agenda))
    if not csrf_valid(request, fields.get("csrf")):
        context = _editor_context(request, db, agenda, content, message=EXPIRED_MESSAGE, stamp=fields.get("draft_stamp", ""))
        return templates.TemplateResponse(request, "admin_consent_edit.html", context, status_code=400)
    if fields.get("draft_stamp", "") != _draft_stamp(draft):
        # Someone saved (or published from) the draft since this page was opened: do not overwrite that silently.
        context = _editor_context(
            request, db, agenda, content, stamp=fields.get("draft_stamp", ""),
            message="이 화면을 연 뒤 다른 곳에서 초안이 먼저 저장되었습니다. 입력하신 내용은 아래에 그대로 남아 있습니다. "
                    "최신 초안을 확인하려면 새 창에서 편집 화면을 열어 비교한 뒤 저장해 주세요.",
        )
        context["draft_stamp"] = _draft_stamp(draft)  # a second save is a deliberate overwrite
        return templates.TemplateResponse(request, "admin_consent_edit.html", context, status_code=409)
    try:
        consent.save_draft(db, agenda, content, client_ip(request))
    except SQLAlchemyError:
        db.rollback()
        logger.exception("consent draft save failed")
        context = _editor_context(request, db, agenda, content, message=FAILED_MESSAGE, stamp=fields.get("draft_stamp", ""))
        return templates.TemplateResponse(request, "admin_consent_edit.html", context, status_code=503)
    problems = consent.publish_blockers(content)
    flash(request, "success" if not problems else "info",
          "초안을 저장했습니다. " + ("게시 전 확인 항목이 모두 채워졌습니다. 전체 미리보기를 확인한 뒤 게시해 주세요."
                               if not problems else f"게시 전에 채워야 할 항목이 {len(problems)}개 남아 있습니다."))
    if fields.get("then") == "preview":
        return RedirectResponse(f"/admin/consent/{agenda.code}/preview", status_code=303)
    anchor = fields.get("anchor", "")
    anchor = anchor if anchor.replace("-", "").isalnum() else ""
    return RedirectResponse(f"/admin/consent/{agenda.code}/edit" + (f"#{anchor}" if anchor else ""), status_code=303)


@router.post("/consent/{code}/draft/load")
def consent_load_version(code: str, request: Request, db: Session = Depends(get_db), csrf: str = Form(""),
                         label: str = Form("")):
    """Replace the draft with a published version's wording (to undo draft edits or restart from an old version)."""
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return _not_found(request)
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse(f"/admin/consent/{agenda.code}/edit", status_code=303)
    version = consent.version_by_label(db, agenda, label)
    if version is None:
        flash(request, "error", "해당 버전을 찾을 수 없습니다.")
    else:
        try:
            consent.save_draft(db, agenda, version.content, client_ip(request))
        except SQLAlchemyError:
            db.rollback()
            logger.exception("consent draft load failed")
            flash(request, "error", FAILED_MESSAGE)
            return RedirectResponse(f"/admin/consent/{agenda.code}/edit", status_code=303)
        flash(request, "success", f"{version.label} 문구를 초안으로 불러왔습니다. 게시된 버전은 바뀌지 않았습니다.")
    return RedirectResponse(f"/admin/consent/{agenda.code}/edit", status_code=303)


@router.get("/consent/{code}/preview")
def consent_preview(code: str, request: Request, db: Session = Depends(get_db)):
    """The saved draft rendered as the full resident page (nothing can be submitted from it)."""
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    draft = consent.draft_of(db, agenda) if agenda else None
    if draft is None:
        return _not_found(request)
    content = consent.normalize_content(draft.content)
    context = consent_page_context(content, agenda_code=agenda.code, version_label="", accepting=False, preview="draft")
    context["blockers"] = consent.publish_blockers(content)
    return templates.TemplateResponse(request, "consent.html", context, headers={"Cache-Control": "no-store"})


@router.post("/consent/{code}/publish")
def consent_publish(code: str, request: Request, db: Session = Depends(get_db), csrf: str = Form(""),
                    draft_hash: str = Form(""), merge_reworded: str = Form("")):
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    if agenda is None:
        return _not_found(request)
    editor = f"/admin/consent/{agenda.code}/edit"
    if not csrf_valid(request, csrf):
        flash(request, "error", EXPIRED_MESSAGE)
        return RedirectResponse(editor, status_code=303)
    draft = consent.draft_of(db, agenda)
    if draft is None or consent.content_hash(consent.normalize_content(draft.content)) != draft_hash:
        flash(request, "error", "게시하려던 초안이 그사이 바뀌었습니다. 화면을 다시 확인한 뒤 게시해 주세요.")
        return RedirectResponse(editor, status_code=303)
    try:
        version = consent.publish(db, agenda, client_ip(request), merge_reworded=merge_reworded == "1")
    except consent.PublishError as exc:
        db.rollback()
        flash(request, "error", str(exc))
        return RedirectResponse(editor, status_code=303)
    except (IntegrityError, SQLAlchemyError):
        db.rollback()
        logger.exception("consent publish failed")
        flash(request, "error", "게시하지 못했습니다. 잠시 후 다시 시도해 주세요.")
        return RedirectResponse(editor, status_code=303)
    logger.info("consent version published %s", version.label)
    where = ("주민 페이지에 바로 반영되었습니다. 작성 중이던 주민은 새로고침 후 다시 확인하게 됩니다."
             if agenda.is_public else "이 안건은 아직 공개되지 않았습니다(안건 목록에서 공개 전환).")
    flash(request, "success", f"{version.label} 버전을 게시했습니다. {where}")
    return RedirectResponse(editor, status_code=303)


@router.get("/consent/{code}/versions/{label}")
def consent_version_page(code: str, label: str, request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return to_login()
    agenda = _consent_agenda(db, code)
    version = consent.version_by_label(db, agenda, label) if agenda else None
    if version is None:
        return _not_found(request)
    context = consent_page_context(
        version.content, agenda_code=agenda.code, version_label=version.label, accepting=False, preview="version"
    )
    context["version_meta"] = {
        "label": version.label,
        "hash": version.content_hash,
        "hash_ok": consent.version_hash_ok(version),
        "scheme": version.hash_scheme,
        "created": format_kst(version.created_at, "%Y-%m-%d %H:%M:%S"),
    }
    return templates.TemplateResponse(request, "consent.html", context, headers={"Cache-Control": "no-store"})
