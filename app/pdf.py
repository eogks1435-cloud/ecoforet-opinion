"""One A4 PDF per submission for the admin: the signed document text, 동·호수, name, opinion, signature, time."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

from . import document
from .models import STATUS_INVALIDATED, OpinionDocument, OpinionSubmission
from .templating import format_kst

FONT_DIR = Path(__file__).resolve().parent / "fonts"  # NanumGothic, SIL OFL (fonts/OFL.txt)
FONT = "Nanum"


def build_submission_pdf(row: OpinionSubmission, doc: OpinionDocument | None) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font(FONT, "", FONT_DIR / "NanumGothic-Regular.ttf")
    pdf.add_font(FONT, "B", FONT_DIR / "NanumGothic-Bold.ttf")
    pdf.set_title(f"{row.document_title} - {row.building}동 {row.unit}호")
    pdf.add_page()
    width = pdf.epw

    if row.status == STATUS_INVALIDATED:
        note = "무효 처리된 제출입니다"
        if row.invalidated_at:
            note += f" ({format_kst(row.invalidated_at, '%Y-%m-%d %H:%M')})"
        if row.invalidated_reason:
            note += f" - 사유: {row.invalidated_reason}"
        pdf.set_text_color(180, 35, 24)
        pdf.set_font(FONT, "B", 11)
        pdf.multi_cell(width, 7, note, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    pdf.set_font(FONT, "B", 17)
    pdf.multi_cell(width, 9, row.document_title, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # The text of the version this resident signed.
    if doc is not None:
        for paragraph in document.split_paragraphs(doc.body):
            for text, bold in document.paragraph_runs(paragraph):
                pdf.set_font(FONT, "B" if bold else "", 10.5)
                pdf.write(6.4, text)
            pdf.ln(6.4 + 2.5)
        pdf.set_font(FONT, "", 9.5)
        for note in document.note_lines(doc.notes):
            pdf.multi_cell(width, 5.6, "※ " + note, new_x="LMARGIN", new_y="NEXT")
        if doc.recipient:
            pdf.ln(2)
            pdf.set_font(FONT, "B", 10.5)
            pdf.multi_cell(width, 6.4, "제출처 : " + doc.recipient, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    if pdf.get_y() > pdf.h - pdf.b_margin - 95:  # keep the answer block together
        pdf.add_page()

    label_w, row_h = 32, 10

    def field(label: str, value: str) -> None:
        pdf.set_font(FONT, "B", 11)
        pdf.cell(label_w, row_h, label, border=1, align="C")
        pdf.set_font(FONT, "", 11)
        pdf.cell(width - label_w, row_h, "  " + value, border=1, new_x="LMARGIN", new_y="NEXT")

    pdf.set_draw_color(110, 110, 110)
    field("동·호수", f"{row.building}동 {row.unit}호")
    field("성명", row.resident_name)
    field("의견", document.OPINION_CHOICES.get(row.opinion_choice, row.opinion_choice))
    sig_h = 34
    top = pdf.get_y()
    pdf.set_font(FONT, "B", 11)
    pdf.cell(label_w, sig_h, "서명", border=1, align="C")
    pdf.cell(width - label_w, sig_h, "", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.image(io.BytesIO(bytes(row.signature_data)), x=pdf.l_margin + label_w + 4, y=top + 2, h=sig_h - 4)
    field("제출일시", format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S") + " (한국시간)")

    if row.additional_comment:
        pdf.ln(4)
        pdf.set_font(FONT, "B", 11)
        pdf.cell(width, 8, "기타 전하고 싶은 말", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(FONT, "", 10.5)
        pdf.multi_cell(width, 6.4, row.additional_comment, border=1, padding=2, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font(FONT, "", 8.5)
    pdf.set_text_color(90, 90, 90)
    printed = format_kst(datetime.now(timezone.utc), "%Y-%m-%d %H:%M")
    pdf.multi_cell(
        width,
        4.8,
        f"제출번호 {row.receipt_no} · 의견서 버전 {row.document_version} · 상태 "
        f"{'무효' if row.status == STATUS_INVALIDATED else '유효'} · 출력 {printed}\n"
        f"본문 SHA-256 {row.document_text_hash}\n"
        "온라인 주민의견서 제출 기록에서 출력한 문서입니다.",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    return bytes(pdf.output())
