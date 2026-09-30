"""A4 PDFs for consent agendas (admin only).

- build_consent_pdf(row): internal copy of one record - the wording exactly as signed (stored version), every
  answer, the privacy and provision consents, 동·호수·성명·서명, receipt, time (KST), version, hash, status.
- build_consent_pdf(row, recipient_key=...): the same record for one recipient - only that recipient's
  provision part, no status internals.
- build_consent_bundle_pdf(rows, ...): many records' copies in one file for printing (a cover listing them, each
  record from a new page, optionally from a new sheet for double-sided printing).
- build_recipient_list_pdf(...): one recipient's list of named agreers (valid records that agreed to this
  recipient and to at least one request), each person's actual answer per question, then the wording.
- build_blank_form_pdf(...): a paper form with a published version's wording, empty tick boxes and blank
  동·호수·성명·서명·작성일, for residents who take part on paper (it has no overseas-transfer section).

None of them contains IP addresses or browser data.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping
from datetime import datetime, timezone

from fpdf import FPDF

from . import consent
from .models import OPINION_AGREE, STATUS_ACTIVE, Agenda, ConsentSubmission, ConsentVersion
from .pdf import FONT, FONT_FILES, _Printable
from .templating import format_kst

# NanumGothic has no circled digits; NFKC would print "① 현 관리사무소장" as "1 현 관리사무소장".
_CIRCLED = str.maketrans({chr(0x2460 + i): f"({i + 1})" for i in range(20)})
SELECTED, NOT_SELECTED, CHECKED = "●", "○", "■"
BODY = 10.2
LINE = 5.9
GREY = (90, 90, 90)
RED = (180, 35, 24)


class _PagedPDF(FPDF):
    """FPDF with a small footer on every page: what the copy is, and page n of N (for printed stacks).

    In a bundle of several records the footer also counts the pages of the record the page belongs to.
    """

    footer_text = ""
    record_first_page: int | None = None  # set in bundles only

    def footer(self) -> None:
        self.set_y(-11)
        self.set_font(FONT, "", 7.6)
        self.set_text_color(*GREY)
        if self.record_first_page is None:
            text = f"{self.footer_text}   {self.page_no()} / {{nb}}"
        elif self.footer_text:
            text = (f"{self.footer_text}   이 동의서 {self.page_no() - self.record_first_page + 1}쪽"
                    f" · 전체 {self.page_no()} / {{nb}}")
        else:  # a bundle's cover, or the empty back of a sheet
            text = f"전체 {self.page_no()} / {{nb}}"
        self.cell(0, 5, text, align="C")
        self.set_text_color(0, 0, 0)


class _Doc:
    """Thin layer over FPDF: the consent wording's headings, paragraphs (**bold**) and answer marks."""

    def __init__(self, title: str, orientation: str = "P", footer: str = "") -> None:
        self.text = _Printable()
        self.pdf = _PagedPDF(orientation=orientation, format="A4")
        self.pdf.footer_text = self.text(footer)
        self.pdf.set_margins(18, 16, 18)
        self.pdf.set_auto_page_break(True, margin=16)
        for style, path in FONT_FILES.items():
            self.pdf.add_font(FONT, style, path)
        self.pdf.set_title(title)
        self.pdf.set_creator("ecoforet-opinion")
        self.pdf.add_page()

    # ---------------------------------------------------------------- primitives
    def t(self, value: str | None) -> str:
        return self.text((value or "").translate(_CIRCLED))

    @property
    def width(self) -> float:
        return self.pdf.epw

    def room(self, height: float) -> None:
        """Start a new page unless `height` mm still fit (keeps small blocks together)."""
        if self.pdf.get_y() + height > self.pdf.h - self.pdf.b_margin:
            self.pdf.add_page()

    def gap(self, height: float = 2.0) -> None:
        self.pdf.ln(height)

    def line(self, value: str, size: float = BODY, bold: bool = False, align: str = "L",
             color: tuple[int, int, int] | None = None, height: float | None = None) -> None:
        if color:
            self.pdf.set_text_color(*color)
        self.pdf.set_font(FONT, "B" if bold else "", size)
        self.pdf.multi_cell(self.width, height or size * 0.58, self.t(value), align=align,
                            new_x="LMARGIN", new_y="NEXT")
        if color:
            self.pdf.set_text_color(0, 0, 0)

    def rich(self, text: str, size: float = BODY) -> None:
        """Paragraphs (blank line), line breaks and **bold** as on the resident page."""
        pdf = self.pdf
        for paragraph in consent.rich_blocks(text):
            for runs in paragraph:
                for run, bold in runs:
                    pdf.set_font(FONT, "B" if bold else "", size)
                    pdf.write(LINE * size / BODY, self.t(run))
                pdf.ln(LINE * size / BODY)
            pdf.ln(1.6)

    def heading(self, value: str, level: int = 1) -> None:
        if not value:
            return
        self.room(18)
        self.gap(2.5 if level == 1 else 1.2)
        self.line(value, size=12.2 if level == 1 else 11, bold=True, height=7 if level == 1 else 6.4)
        self.gap(1.2)

    def label_value(self, label: str, value: str, label_w: float = 46) -> None:
        """A two-column row (label | text); the text wraps, the row stays together when it fits a page."""
        pdf = self.pdf
        label_text = self.t(label)
        value_text = self.t("\n".join(consent.plain_lines(value)))
        pdf.set_font(FONT, "B", 9.6)
        label_lines = len(pdf.multi_cell(label_w, 5.6, label_text, dry_run=True, output="LINES"))
        pdf.set_font(FONT, "", 9.6)
        value_lines = len(pdf.multi_cell(self.width - label_w, 5.6, value_text, dry_run=True, output="LINES"))
        height = 5.6 * max(label_lines, value_lines)
        if height < 120:
            self.room(height)
        top, page = pdf.get_y(), pdf.page
        pdf.set_font(FONT, "B", 9.6)
        pdf.multi_cell(label_w, 5.6, label_text, align="L", new_x="RIGHT", new_y="TOP")
        pdf.set_xy(pdf.l_margin + label_w, top)
        pdf.set_font(FONT, "", 9.6)
        pdf.multi_cell(self.width - label_w, 5.6, value_text, align="L", new_x="LMARGIN", new_y="NEXT")
        if pdf.page == page:
            pdf.set_y(max(pdf.get_y(), top + 5.6 * label_lines))
        pdf.ln(0.8)

    def fit_cell(self, width: float, height: float, value: str, size: float = 9.4) -> None:
        """One table cell; long text gets a smaller font, then wraps inside the cell (never runs over)."""
        pdf = self.pdf
        value = self.t(value)
        x, y = pdf.get_x(), pdf.get_y()
        while size > 7 and pdf.get_string_width(value) > width - 2:
            size -= 0.4
            pdf.set_font(FONT, "", size)
        pdf.set_font(FONT, "", size)
        if pdf.get_string_width(value) <= width - 2:
            pdf.cell(width, height, value, border=1, align="C")
            return
        pdf.cell(width, height, "", border=1)
        lines = pdf.multi_cell(width - 2, 4, value, dry_run=True, output="LINES")[: int(height // 4)]
        pdf.set_xy(x + 1, y + (height - 4 * len(lines)) / 2)
        pdf.multi_cell(width - 2, 4, "\n".join(lines), align="C")
        pdf.set_xy(x + width, y)

    def choice(self, agree_label: str, disagree_label: str, answer: str | None) -> None:
        """The two options of a question with the resident's choice marked (● chosen, ○ not chosen)."""
        pdf = self.pdf
        self.room(9)
        pdf.set_x(pdf.l_margin + 4)
        for value, label in ((OPINION_AGREE, agree_label), ("DISAGREE", disagree_label)):
            chosen = answer == value
            pdf.set_font(FONT, "B" if chosen else "", 10.5)
            pdf.write(6.6, f"{SELECTED if chosen else NOT_SELECTED} {self.t(label)}")
            pdf.write(6.6, "      ")
        pdf.ln(6.6)
        if answer is None:
            self.line("(이 질문에 대한 답변 기록 없음)", size=9, color=RED)
        self.gap(1.5)

    def box_rows(self, rows: list[tuple[str, str]], label_w: float = 34, row_h: float = 9) -> None:
        pdf = self.pdf
        pdf.set_draw_color(110, 110, 110)
        for label, value in rows:
            pdf.set_font(FONT, "B", 10.5)
            pdf.cell(label_w, row_h, self.t(label), border=1, align="C")
            pdf.set_font(FONT, "", 10.5)
            pdf.cell(self.width - label_w, row_h, "  " + self.t(value), border=1, new_x="LMARGIN", new_y="NEXT")

    def signature(self, png: bytes, label_w: float = 34, height: float = 34) -> None:
        pdf = self.pdf
        top = pdf.get_y()
        pdf.set_font(FONT, "B", 10.5)
        pdf.cell(label_w, height, "서명", border=1, align="C")
        pdf.cell(self.width - label_w, height, "", border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.image(io.BytesIO(png), x=pdf.l_margin + label_w + 4, y=top + 2, w=self.width - label_w - 8,
                  h=height - 4, keep_aspect_ratio=True)

    def boxes(self, *labels: str, size: float = 10.5) -> None:
        """Empty tick boxes on one line (drawn squares: the □ glyph marks missing characters elsewhere)."""
        pdf = self.pdf
        self.room(10)
        x, top = pdf.l_margin + 4, pdf.get_y()
        pdf.set_font(FONT, "", size)
        pdf.set_draw_color(40, 40, 40)
        for label in labels:
            pdf.rect(x, top + 1.7, 4.2, 4.2)
            pdf.set_xy(x + 6, top)
            text = self.t(label)
            pdf.cell(pdf.get_string_width(text) + 2, 7.6, text)
            x = pdf.get_x() + 10
        pdf.set_xy(pdf.l_margin, top + 7.6)
        self.gap(1.5)

    def tick_line(self, text: str, size: float = 10.5, bold: bool = True) -> None:
        """One empty tick box followed by a (possibly long) sentence."""
        pdf = self.pdf
        self.room(12)
        top = pdf.get_y()
        pdf.set_draw_color(40, 40, 40)
        pdf.rect(pdf.l_margin, top + 1.6, 4.2, 4.2)
        pdf.set_xy(pdf.l_margin + 6.5, top)
        pdf.set_font(FONT, "B" if bold else "", size)
        pdf.multi_cell(self.width - 6.5, 6.6, self.t(text), align="L", new_x="LMARGIN", new_y="NEXT")

    def blank_rows(self, rows: list[tuple[str, str]], label_w: float = 30, row_h: float = 13) -> None:
        """Label | empty writing space (with an optional faint guide such as '동   호')."""
        pdf = self.pdf
        pdf.set_draw_color(90, 90, 90)
        for label, guide in rows:
            pdf.set_font(FONT, "B", 10.5)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(label_w, row_h, self.t(label), border=1, align="C")
            pdf.set_font(FONT, "", 10.5)
            pdf.set_text_color(*GREY)
            pdf.cell(self.width - label_w, row_h, self.t(guide), border=1, align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)

    def blank_signature(self, label_w: float = 30, height: float = 32) -> None:
        pdf = self.pdf
        pdf.set_draw_color(90, 90, 90)
        pdf.set_font(FONT, "B", 10.5)
        pdf.cell(label_w, height, "서명", border=1, align="C")
        pdf.cell(self.width - label_w, height, "", border=1, new_x="LMARGIN", new_y="NEXT")

    def note_box(self, title: str, lines: list[str]) -> None:
        """A framed block of short notes (kept on one page when it fits)."""
        pdf = self.pdf
        pdf.set_font(FONT, "", 9.8)
        body = [self.t(line) for line in lines if line]
        height = 8 + sum(5.6 * len(pdf.multi_cell(self.width - 8, 5.6, line, dry_run=True, output="LINES"))
                         for line in body)
        self.room(height + 4)
        top = pdf.get_y()
        pdf.set_xy(pdf.l_margin + 4, top + 3)
        pdf.set_font(FONT, "B", 10.5)
        pdf.cell(self.width - 8, 6, self.t(title), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(FONT, "", 9.8)
        for line in body:
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(self.width - 8, 5.6, line, align="L", new_x="LMARGIN", new_y="NEXT")
        bottom = pdf.get_y() + 2
        pdf.set_draw_color(120, 120, 120)
        pdf.rect(pdf.l_margin, top, self.width, bottom - top)
        pdf.set_y(bottom + 3)

    def output(self) -> bytes:
        return bytes(self.pdf.output())

    def missing_glyph_note(self) -> None:
        if self.text.replaced:
            self.line("□ 표시는 PDF 글꼴에 없는 문자입니다(한자·이모지 등). 원문은 관리자 화면과 CSV에서 확인할 수 있습니다.",
                      size=8, color=GREY, height=4.6)


# ------------------------------------------------------------------------------------------- the wording
def _document_head(doc: _Doc, content: Mapping) -> None:
    if content.get("apartment_name"):
        doc.line(content["apartment_name"], size=10.5, align="C", color=GREY, height=6)
    doc.line(content["title"], size=16, bold=True, align="C", height=8.4)
    if content.get("subtitle"):
        doc.line(content["subtitle"], size=13, bold=True, align="C", height=7.4)
    doc.gap(3)
    rows = [("수신", content.get("recipient_line", "")), ("참조", content.get("cc_line", ""))]
    for label, value in rows:
        if value:
            doc.label_value(label, value, label_w=16)
    if content.get("intro"):
        doc.gap(1)
        doc.rich(content["intro"])
    doc.gap(2)


def _document_body(doc: _Doc, content: Mapping) -> None:
    for section in content.get("sections", []):
        doc.heading(section["title"], section["level"])
        doc.rich(section["body"])


def _privacy_notice(doc: _Doc, content: Mapping) -> None:
    privacy = content["privacy"]
    doc.heading(privacy["title"])
    doc.line("(1) 개인정보 수집·관리 주체 및 연락처", bold=True, height=6)
    doc.label_value("수집·관리 주체", privacy["controller"])
    doc.label_value("개인정보 관리책임자", privacy["officer"])
    doc.label_value("문의·열람·정정·철회 요청 연락처", privacy["contact"])
    doc.line("(2) 수집·이용 목적", bold=True, height=6)
    doc.rich(privacy["purpose"])
    doc.line("(3) 수집 항목", bold=True, height=6)
    doc.label_value("입력·선택 정보", privacy["items_input"])
    doc.label_value("생성·수집 정보", consent.generated_items(content))
    doc.line("(4) 보유·이용기간", bold=True, height=6)
    doc.label_value("동의자료", privacy["retention_records"])
    if privacy.get("collect_access_info"):
        doc.label_value("접속 IP 주소 및 브라우저 정보", privacy["retention_access"])
    doc.rich(privacy["retention_notes"])
    if privacy.get("storage_location"):
        doc.label_value("개인정보 저장 위치", privacy["storage_location"])
    doc.line("(5) 동의 거부권 및 거부 시 영향", bold=True, height=6)
    doc.rich(privacy["refusal"])


def _overseas_notice(doc: _Doc, overseas: Mapping) -> None:
    doc.heading(overseas["title"])
    doc.rich(overseas["intro"])
    for label, name in (("이전되는 항목", "items"), ("이전되는 국가", "country"), ("이전 시기와 방법", "timing"),
                        ("이전받는 자", "recipient"), ("이용 목적", "purpose"), ("보유·이용기간", "retention")):
        doc.label_value(label, overseas[name])
    doc.line("거부 방법과 효과", bold=True, height=6)
    doc.rich(overseas["refusal"])
    if overseas.get("alternative"):
        doc.label_value("다른 참여 방법", overseas["alternative"])


def _overseas_of(content: Mapping) -> Mapping | None:
    """The overseas-transfer section when this wording has it (versions from before it existed do not)."""
    overseas = content.get("overseas") or {}
    return overseas if overseas.get("enabled") else None


def _recipient_notice(doc: _Doc, recipient: Mapping) -> None:
    doc.heading(recipient["heading"], 2)
    doc.label_value("(1) 제공받는 자", recipient["name"])
    doc.label_value("(2) 이용 목적", recipient["purpose"])
    doc.label_value("(3) 제공 항목", recipient["items"])
    doc.label_value("(4) 보유·이용기간", recipient["retention"])
    doc.line("(5) 동의 거부권 및 거부 시 영향", bold=True, size=9.6, height=5.6)
    doc.rich(recipient["refusal"], size=9.6)


def _wording_only(doc: _Doc, content: Mapping) -> None:
    """The whole resident page as text (no answers): the appendix of a recipient list."""
    _document_head(doc, content)
    _document_body(doc, content)
    doc.heading(content["questions_title"])
    doc.rich(content["questions_intro"])
    for number, question in enumerate(consent.active_questions(content), start=1):
        doc.heading(f"질문 {number}. {question['title']}", 2)
        doc.rich(question["text"])
        doc.line(f"{NOT_SELECTED} {question['agree_label']}      {NOT_SELECTED} {question['disagree_label']}", height=6.4)
        doc.gap(1)
    doc.heading(content["participant_title"])
    doc.label_value("참여 대상", content["participant_target"])
    doc.line("동 / 호수 / 성명 / 서명 입력", size=9.6, color=GREY)
    doc.heading(content["participation_notes_title"], 2)
    doc.rich(content["participation_notes"])
    _privacy_notice(doc, content)
    overseas = _overseas_of(content)
    if overseas:
        _overseas_notice(doc, overseas)
        doc.line(f"{NOT_SELECTED} {overseas['agree_label']}      {NOT_SELECTED} {overseas['disagree_label']}", height=6.4)
    doc.heading(content["provision_title"])
    doc.rich(content["provision_intro"])
    for recipient in content["recipients"]:
        _recipient_notice(doc, recipient)
    doc.heading(content["principles_title"], 2)
    doc.rich(content["principles"])
    doc.heading(content["final_title"])
    doc.rich(content["final_statements"])
    doc.line(f"{NOT_SELECTED} {content['final_checkbox']}", height=6.4)
    if content.get("footer_note"):
        doc.line(content["footer_note"], size=9, color=GREY)


# ----------------------------------------------------------------------------------------- one record
def _copy_of(content: Mapping, recipient_key: str | None) -> tuple[list[dict], str]:
    """The recipients a copy shows (all, or the one it is for) and the copy's name."""
    recipients = [r for r in content["recipients"] if recipient_key in (None, r["key"])]
    name = "내부 열람용" if recipient_key is None else f"{recipients[0]['short_name'] or recipients[0]['name']} 제출용"
    return recipients, name


def _record_footer(row: ConsentSubmission, recipient_key: str | None) -> str:
    return f"제출번호 {row.receipt_no} · {row.version_label} · {_copy_of(row.version.content, recipient_key)[1]}"


def build_consent_pdf(row: ConsentSubmission, recipient_key: str | None = None) -> bytes:
    content = row.version.content  # the stored wording of the version this resident signed
    doc = _Doc(f"{content['title']} {content.get('subtitle', '')} - 제출번호 {row.receipt_no}",
               footer=_record_footer(row, recipient_key))
    _write_record(doc, row, recipient_key)
    doc.missing_glyph_note()
    return doc.output()


def _write_record(doc: _Doc, row: ConsentSubmission, recipient_key: str | None) -> None:
    content = row.version.content
    answers, provisions = row.answer_map(), row.provision_map()
    recipients, _name = _copy_of(content, recipient_key)

    if recipient_key is None and row.status != STATUS_ACTIVE:
        note = f"{consent.STATUS_LABEL[row.status]} 처리된 제출입니다 ({format_kst(row.status_changed_at, '%Y-%m-%d %H:%M')})"
        if row.status_reason:
            note += f" - 사유: {row.status_reason}"
        doc.line(note, size=11, bold=True, align="C", color=RED, height=7)
        doc.gap(1)
    if recipient_key is not None:
        doc.line(f"{recipients[0]['short_name'] or recipients[0]['name']} 제출용 사본", size=9.6, bold=True,
                 align="R", color=GREY, height=5.6)
    else:
        doc.line("내부 열람용", size=9.6, bold=True, align="R", color=GREY, height=5.6)

    _document_head(doc, content)
    _document_body(doc, content)

    doc.heading(content["questions_title"])
    doc.rich(content["questions_intro"])
    doc.line(f"{SELECTED} 선택한 답변   {NOT_SELECTED} 선택하지 않은 답변", size=8.6, color=GREY, height=5)
    for number, question in enumerate(consent.active_questions(content), start=1):
        doc.heading(f"질문 {number}. {question['title']}", 2)
        doc.rich(question["text"])
        doc.choice(question["agree_label"], question["disagree_label"], answers.get(question["key"]))

    doc.heading(content["participant_title"])
    if content.get("participant_target"):
        doc.label_value("참여 대상", content["participant_target"])
    doc.room(70)
    doc.box_rows([("동·호수", f"{row.building}동 {row.unit}호"), ("성명", row.resident_name)])
    doc.signature(bytes(row.signature_data))
    doc.box_rows([("제출일시", format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S") + " (한국시간)")])
    doc.gap(3)
    doc.heading(content["participation_notes_title"], 2)
    doc.rich(content["participation_notes"], size=9.6)

    _privacy_notice(doc, content)
    privacy = content["privacy"]
    doc.line(privacy["question"], bold=True, height=6.2)
    doc.choice(privacy["agree_label"], privacy["disagree_label"], OPINION_AGREE if row.privacy_consent else "DISAGREE")

    overseas = _overseas_of(content)
    if overseas:
        _overseas_notice(doc, overseas)
        doc.line(overseas["question"], bold=True, height=6.2)
        doc.choice(overseas["agree_label"], overseas["disagree_label"],
                   None if row.overseas_consent is None else (OPINION_AGREE if row.overseas_consent else "DISAGREE"))

    doc.heading(content["provision_title"])
    doc.rich(content["provision_intro"])
    for recipient in recipients:
        _recipient_notice(doc, recipient)
        provision = provisions.get(recipient["key"])
        doc.line(recipient["question"], bold=True, height=6.2)
        doc.choice(recipient["agree_label"], recipient["disagree_label"],
                   None if provision is None else (OPINION_AGREE if provision.agreed else "DISAGREE"))
        if recipient_key is None and provision is not None and provision.withdrawn_at is not None:
            note = f"제공 동의 철회 기록: {format_kst(provision.withdrawn_at, '%Y-%m-%d %H:%M')}"
            if provision.withdrawn_reason:
                note += f" - 사유: {provision.withdrawn_reason}"
            if provision.already_delivered:
                note += " (제출처 전달 후 철회)"
            doc.line(note, size=9.4, bold=True, color=RED, height=5.6)
            doc.gap(1)
    doc.heading(content["principles_title"], 2)
    doc.rich(content["principles"], size=9.6)

    doc.heading(content["final_title"])
    doc.rich(content["final_statements"])
    doc.room(10)
    doc.line(f"{CHECKED if row.final_confirmed else NOT_SELECTED} {content['final_checkbox']}", bold=True, height=6.6)
    if content.get("footer_note"):
        doc.line(content["footer_note"], size=9, color=GREY, height=5.2)

    # Submission record: what identifies this copy.
    doc.gap(4)
    doc.room(64)  # keep the whole record block on one page
    printed = format_kst(datetime.now(timezone.utc), "%Y-%m-%d %H:%M")
    meta = [
        ("제출번호", row.receipt_no),
        ("제출일시", format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S") + " (한국시간, 서버 수신 시각)"),
        ("문서 버전", row.version_label),
        ("본문 확인값", f"SHA-256 {row.content_hash} ({row.version.hash_scheme})"),
    ]
    if recipient_key is None:
        status = consent.STATUS_LABEL[row.status]
        if row.status != STATUS_ACTIVE:
            status += f" ({format_kst(row.status_changed_at, '%Y-%m-%d %H:%M')}"
            status += f", 사유: {row.status_reason})" if row.status_reason else ")"
            if row.already_delivered:
                status += " · 제출처 전달 후 처리"
        meta.append(("상태", status))
    meta.append(("출력", f"{printed} (한국시간) · " + ("내부 열람용" if recipient_key is None else
                                                  f"{recipients[0]['name']} 제출용")))
    doc.pdf.set_draw_color(160, 160, 160)
    for label, value in meta:
        doc.label_value(label, value, label_w=26)
    doc.line("온라인 동의서 제출 기록에서 출력한 문서입니다. 동·호수·성명·서명만으로 본인인증을 거친 것은 아닙니다.",
             size=8.4, color=GREY, height=4.8)


# ------------------------------------------------------------------------------ many records in one file
def build_consent_bundle_pdf(rows: list[ConsentSubmission], recipient_key: str | None, *, title: str,
                             copy_title: str, first_number: int, total: int, note: str,
                             duplex: bool = False) -> bytes:
    """Several records' copies in one file for printing: a cover listing them, then each record from a new page.

    With `duplex` an empty back side follows a record that ends on a front side, so that no printed sheet
    carries two people's pages.
    """
    last_number = first_number + len(rows) - 1
    doc = _Doc(f"{title} - {copy_title} 개별 동의서 {first_number}~{last_number}번")
    pdf = doc.pdf
    pdf.record_first_page = 1  # the cover's footer shows only the page count
    printed = format_kst(datetime.now(timezone.utc), "%Y-%m-%d %H:%M")

    content = rows[0].version.content if rows else {}
    if content.get("apartment_name"):
        doc.line(content["apartment_name"], size=10.5, align="C", color=GREY, height=6)
    doc.line(title, size=15, bold=True, align="C", height=8)
    doc.line(f"{copy_title} 개별 동의서 묶음", size=12.5, bold=True, align="C", height=7.4)
    doc.gap(2)
    doc.label_value("이 파일", f"{first_number}~{last_number}번 ({len(rows)}명, 전체 {total}명 중)", label_w=36)
    doc.label_value("출력일시", f"{printed} (한국시간)", label_w=36)
    doc.label_value("인쇄", "양면 인쇄용: 사람마다 새 종이에서 시작합니다(빈 면이 들어 있습니다)." if duplex else
                    "사람마다 새 쪽에서 시작합니다. 양면으로 인쇄할 때는 양면용 파일을 쓰세요.", label_w=36)
    doc.line(note, size=9, color=GREY, height=5)
    doc.gap(3)
    widths = [16, 34, 44, 30, 50]
    widths = [w * doc.width / sum(widths) for w in widths]
    pdf.set_draw_color(150, 150, 150)
    pdf.set_font(FONT, "B", 9.4)
    pdf.set_fill_color(240, 242, 245)
    for width, label in zip(widths, ("번호", "동·호수", "성명", "제출번호", "제출일시")):
        pdf.cell(width, 8, label, border=1, align="C", fill=True)
    pdf.ln(8)
    for number, row in enumerate(rows, start=first_number):
        doc.room(8)
        for width, value in zip(widths, (str(number), f"{row.building}동 {row.unit}호", row.resident_name,
                                         row.receipt_no, format_kst(row.submitted_at, "%Y-%m-%d %H:%M"))):
            doc.fit_cell(width, 8, value)
        pdf.ln(8)

    for row in rows:
        if duplex and pdf.page_no() % 2 == 1:
            pdf.add_page()  # the empty back of the previous sheet
            pdf.footer_text = ""
        pdf.add_page()
        pdf.footer_text = doc.text(_record_footer(row, recipient_key))
        pdf.record_first_page = pdf.page_no()
        _write_record(doc, row, recipient_key)
    doc.missing_glyph_note()
    return doc.output()


# ---------------------------------------------------------------------------------- recipient list PDF
def build_recipient_list_pdf(agenda: Agenda, recipient_key: str, rows: list[ConsentSubmission],
                             questions: list[consent.QuestionCount], version: ConsentVersion | None) -> bytes:
    """Named agreers for one recipient: a table with each person's answers and signature, then the wording."""
    content = version.content if version else consent.seed_content()
    recipient = next(r for r in content["recipients"] if r["key"] == recipient_key)
    doc = _Doc(f"{content['title']} - {recipient['short_name'] or recipient['name']} 제출용 기명 동의자 명단",
               orientation="L", footer=f"{recipient['name']} 제출용 기명 동의자 명단 · 출력 "
                                       f"{format_kst(datetime.now(timezone.utc), '%Y-%m-%d %H:%M')}")
    pdf = doc.pdf
    printed = format_kst(datetime.now(timezone.utc), "%Y-%m-%d %H:%M")

    if content.get("apartment_name"):
        doc.line(content["apartment_name"], size=10.5, align="C", color=GREY, height=6)
    doc.line(f"{content['title']} {content.get('subtitle', '')}", size=15, bold=True, align="C", height=8)
    doc.line(f"{recipient['name']} 제출용 기명 동의자 명단", size=12.5, bold=True, align="C", height=7.4)
    doc.gap(2)
    versions = sorted({row.version_label for row in rows})
    doc.label_value("명단 인원", f"{len(rows)}명", label_w=36)
    for number, question in enumerate(questions, start=1):
        agreed = sum(1 for row in rows if row.answer_map().get(question.key) == OPINION_AGREE)
        title = re.sub(r"^질문 \d+\. ", "", question.title)
        doc.label_value(f"질문{number}", f"{title} - 기명 동의자 {agreed}명", label_w=36)
    doc.label_value("문서 버전", ", ".join(versions) if versions else "-", label_w=36)
    doc.label_value("출력일시", f"{printed} (한국시간)", label_w=36)
    doc.line(
        f"이 명단에는 유효한 제출 중 {recipient['name']}에 대한 개인정보 제공에 동의하고 1개 이상의 요청사항에 동의한 "
        "참여자만 포함합니다. 각 참여자의 질문별 답변은 제출 내용 그대로 표시하며(동의하지 않은 질문은 '비동의'), "
        "질문별 인원을 서로 더하지 않습니다. 접속 IP·브라우저 정보는 포함하지 않습니다.",
        size=9, color=GREY, height=5,
    )
    doc.gap(3)

    # Table: 번호, 동, 호, 성명, 질문1.., 서명, 제출일시, 제출번호 (+버전 when several)
    several = len(versions) > 1
    widths = [11, 15, 16, 30] + [17] * len(questions) + [48, 34, 22] + ([34] if several else [])
    scale = doc.width / sum(widths)
    widths = [w * scale for w in widths]
    header = ["번호", "동", "호수", "성명"] + [f"질문{n}" for n in range(1, len(questions) + 1)] + [
        "서명", "제출일시", "제출번호"] + (["문서 버전"] if several else [])
    row_h = 16.0

    def table_header() -> None:
        pdf.set_font(FONT, "B", 9.4)
        pdf.set_fill_color(240, 242, 245)
        for width, label in zip(widths, header):
            pdf.cell(width, 8, label, border=1, align="C", fill=True)
        pdf.ln(8)

    pdf.set_draw_color(150, 150, 150)
    table_header()
    for number, row in enumerate(rows, start=1):
        if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
            pdf.add_page()
            table_header()
        answers = row.answer_map()
        cells = [str(number), row.building, row.unit, row.resident_name] + [
            {"AGREE": "동의", "DISAGREE": "비동의"}.get(answers.get(q.key, ""), "-") for q in questions
        ]
        top, x = pdf.get_y(), pdf.l_margin
        for width, value in zip(widths, cells):
            pdf.set_xy(x, top)
            doc.fit_cell(width, row_h, value)
            x += width
        sig_w = widths[len(cells)]
        pdf.set_xy(x, top)
        pdf.cell(sig_w, row_h, "", border=1)
        pdf.image(io.BytesIO(bytes(row.signature_data)), x=x + 1.5, y=top + 1, w=sig_w - 3, h=row_h - 2,
                  keep_aspect_ratio=True)
        x += sig_w
        tail = [format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S"), row.receipt_no] + (
            [row.version_label] if several else [])
        pdf.set_font(FONT, "", 8.6)
        for width, value in zip(widths[len(cells) + 1:], tail):
            pdf.set_xy(x, top)
            pdf.cell(width, row_h, value, border=1, align="C")
            x += width
        pdf.set_xy(pdf.l_margin, top + row_h)
    if not rows:
        pdf.set_font(FONT, "", 10)
        pdf.cell(doc.width, 12, "해당하는 기록이 없습니다.", border=1, align="C", new_x="LMARGIN", new_y="NEXT")

    # Appendix: the wording of every version the listed people signed.
    shown = {v.label: v.content for v in ([version] if version else [])}
    for row in rows:
        shown.setdefault(row.version_label, row.version.content)
    for label in sorted(shown):
        pdf.add_page(orientation="P")
        doc.line(f"[부록] 동의서 원문 - {label}", size=10, bold=True, color=GREY, height=6)
        digest = next((r.content_hash for r in rows if r.version_label == label), None) or (
            version.content_hash if version and version.label == label else "")
        if digest:
            doc.line(f"본문 확인값 SHA-256 {digest}", size=8.4, color=GREY, height=4.8)
        doc.gap(2)
        _wording_only(doc, shown[label])
    doc.missing_glyph_note()
    return doc.output()


# ------------------------------------------------------------------------------------------ paper form
def build_blank_form_pdf(content: Mapping, label: str, digest: str, *, draft: bool = False) -> bytes:
    """A printable paper form: the version's wording with empty tick boxes and blank fields.

    Residents who do not agree to the overseas transfer take part on paper, so the overseas-transfer section
    is left out and a "서면 제출 안내" block says what differs on paper (plus the operator's own text on how
    paper forms are taken in). The system-time footer note of the online form is replaced by a 작성일 field.
    """
    footer = "서면 동의서 · 초안(배포용 아님)" if draft else f"서면 동의서 · {label}"
    doc = _Doc(f"{content['title']} {content.get('subtitle', '')} - 서면 동의서", footer=footer)
    if draft:
        doc.line("초안 미리보기 · 게시 전 확인용이며 주민에게 배포하지 마세요", size=10.5, bold=True, align="C",
                 color=RED, height=6.4)
    doc.line("서면 동의서", size=9.6, bold=True, align="R", color=GREY, height=5.6)
    _document_head(doc, content)
    _document_body(doc, content)

    doc.heading(content["questions_title"])
    doc.rich(content["questions_intro"])
    doc.line("각 질문마다 한 칸에만 표시해 주세요.", size=9, color=GREY, height=5)
    for number, question in enumerate(consent.active_questions(content), start=1):
        doc.heading(f"질문 {number}. {question['title']}", 2)
        doc.rich(question["text"])
        doc.boxes(question["agree_label"], question["disagree_label"])

    doc.heading(content["participant_title"])
    if content.get("participant_target"):
        doc.label_value("참여 대상", content["participant_target"])
    doc.room(75)
    doc.blank_rows([("동·호수", "동                    호     "), ("성명", "")])
    doc.blank_signature()
    doc.blank_rows([("작성일", "년            월            일     ")])
    doc.gap(3)
    doc.heading(content["participation_notes_title"], 2)
    doc.rich(content["participation_notes"], size=9.6)

    _privacy_notice(doc, content)
    privacy = content["privacy"]
    doc.line(privacy["question"], bold=True, height=6.2)
    doc.boxes(privacy["agree_label"], privacy["disagree_label"])

    overseas = content.get("overseas") or {}
    notes = [
        f"이 서면 동의서는 온라인 동의서 {label}의 문안을 그대로 옮긴 것입니다. 같은 동·호수는 온라인과 서면을 "
        "합쳐 한 번만 유효하게 참여할 수 있습니다.",
        "서면으로 제출하시는 경우 접속 IP 주소·브라우저 정보는 수집하지 않으며, 제출번호와 제출일시 대신 "
        "작성일을 직접 적습니다.",
    ]
    if overseas.get("alternative"):
        notes.append(f"제출 방법: {' '.join(consent.plain_lines(overseas['alternative']))}")
    doc.note_box("서면 제출 안내", notes)

    doc.heading(content["provision_title"])
    doc.rich(content["provision_intro"])
    for recipient in content["recipients"]:
        _recipient_notice(doc, recipient)
        doc.line(recipient["question"], bold=True, height=6.2)
        doc.boxes(recipient["agree_label"], recipient["disagree_label"])
    doc.heading(content["principles_title"], 2)
    doc.rich(content["principles"], size=9.6)

    doc.room(72)  # the final statements and their tick box stay on one page
    doc.heading(content["final_title"])
    doc.rich(content["final_statements"])
    doc.tick_line(content["final_checkbox"])
    doc.gap(4)
    doc.line(f"문서 버전 {label} · 본문 확인값 SHA-256 {digest}", size=8, color=GREY, height=4.6)
    doc.missing_glyph_note()
    return doc.output()
