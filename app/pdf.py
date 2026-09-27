"""One A4 PDF per submission for the admin: the signed document text, 동·호수, name, opinion, signature, time."""

from __future__ import annotations

import io
import logging
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fontTools.ttLib import TTFont
from fpdf import FPDF

from . import document
from .models import STATUS_INVALIDATED, OpinionDocument, OpinionSubmission
from .templating import format_kst

FONT_DIR = Path(__file__).resolve().parent / "fonts"  # NanumGothic, SIL OFL (fonts/OFL.txt)
FONT = "Nanum"
FONT_FILES = {"": FONT_DIR / "NanumGothic-Regular.ttf", "B": FONT_DIR / "NanumGothic-Bold.ttf"}
MISSING_MARK = "□"

# fontTools logs every glyph of each PDF at INFO level, which would put resident names in the server logs.
logging.getLogger("fontTools").setLevel(logging.WARNING)

# Code points both font files can draw.
_DRAWABLE = frozenset.intersection(*(frozenset(TTFont(path, lazy=True).getBestCmap()) for path in FONT_FILES.values()))


_DOT_LOOKALIKES = str.maketrans(dict.fromkeys("∙・･‧⋅", "·"))


class _Printable:
    """Maps text onto the embedded font: fpdf2 would silently drop characters the font lacks (Hanja, ①, emoji)."""

    def __init__(self) -> None:
        self.replaced = False

    def __call__(self, value: str | None) -> str:
        out = []
        for char in (value or "").replace("\t", " ").translate(_DOT_LOOKALIKES):
            if char == "\n" or ord(char) in _DRAWABLE:
                out.append(char)
                continue
            if unicodedata.category(char) in ("Cf", "Mn", "Me") or 0x1F3FB <= ord(char) <= 0x1F3FF:
                continue  # zero-width characters, joiners, variation selectors, skin tones: nothing visible

            folded = unicodedata.normalize("NFKC", char)  # ㎡ -> m2, ① -> 1, ㈜ -> (주)
            if not all(ord(c) in _DRAWABLE for c in folded):
                folded = "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c))  # é -> e
            if folded and all(ord(c) in _DRAWABLE for c in folded):
                out.append(folded)
            else:
                out.append(MISSING_MARK)
                self.replaced = True
        return "".join(out)


def build_submission_pdf(row: OpinionSubmission, doc: OpinionDocument | None) -> bytes:
    text = _Printable()
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(True, margin=18)
    for style, path in FONT_FILES.items():
        pdf.add_font(FONT, style, path)
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
        pdf.multi_cell(width, 7, text(note), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    pdf.set_font(FONT, "B", 17)
    pdf.multi_cell(width, 9, text(row.document_title), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # The text of the version this resident signed.
    if doc is not None:
        for paragraph in document.split_paragraphs(doc.body):
            for run, bold in document.paragraph_runs(paragraph):
                pdf.set_font(FONT, "B" if bold else "", 10.5)
                pdf.write(6.4, text(run))
            pdf.ln(6.4 + 2.5)
        pdf.set_font(FONT, "", 9.5)
        for note in document.note_lines(doc.notes):
            pdf.multi_cell(width, 5.6, "※ " + text(note), new_x="LMARGIN", new_y="NEXT")
        if doc.recipient:
            pdf.ln(2)
            pdf.set_font(FONT, "B", 10.5)
            pdf.multi_cell(width, 6.4, "제출처 : " + text(doc.recipient), align="C", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_text_color(180, 35, 24)
        pdf.set_font(FONT, "B", 10.5)
        pdf.multi_cell(
            width, 6.4, f"의견서 본문(버전 {text(row.document_version)})을 찾을 수 없어 본문을 표시하지 않았습니다.",
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.set_text_color(0, 0, 0)
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
    field("성명", text(row.resident_name))
    field("의견", document.OPINION_CHOICES.get(row.opinion_choice, row.opinion_choice))
    sig_h = 34
    top = pdf.get_y()
    pdf.set_font(FONT, "B", 11)
    pdf.cell(label_w, sig_h, "서명", border=1, align="C")
    pdf.cell(width - label_w, sig_h, "", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.image(  # fitted inside the cell whatever the stored image's proportions
        io.BytesIO(bytes(row.signature_data)),
        x=pdf.l_margin + label_w + 4, y=top + 2, w=width - label_w - 8, h=sig_h - 4, keep_aspect_ratio=True,
    )
    field("제출일시", format_kst(row.submitted_at, "%Y-%m-%d %H:%M:%S") + " (한국시간)")

    if row.additional_comment:
        pdf.ln(4)
        pdf.set_font(FONT, "B", 11)
        pdf.cell(width, 8, "기타 전하고 싶은 말", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(FONT, "", 10.5)
        pdf.multi_cell(width, 6.4, text(row.additional_comment), border=1, padding=2, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font(FONT, "", 8.5)
    pdf.set_text_color(90, 90, 90)
    pdf.set_auto_page_break(True, margin=10)  # the small footer may use part of the bottom margin
    if pdf.get_y() + 4.8 * (4 if text.replaced else 3) > pdf.h - pdf.b_margin:
        pdf.add_page()  # keep the receipt/hash block in one piece
    if text.replaced:
        pdf.multi_cell(
            width, 4.8, "□ 표시는 PDF 글꼴에 없는 문자입니다(한자·이모지 등). 원문은 관리자 화면과 CSV에서 확인할 수 있습니다.",
            new_x="LMARGIN", new_y="NEXT",
        )
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
