"""The opinion document residents sign, and its versions.

Versions live in the opinion_documents table and are edited from /admin/document. Every published edit
becomes a new version with its own SHA-256; submissions keep the version (and hash) they were signed with.
The first version is seeded from the SEED_* constants below, which follow 주민의견서에코포레.pdf word for word.

Body markup: paragraphs are separated by a blank line and **text** is shown in bold (the bold passages of
the PDF). Notes: one per line, shown with ※. The hash covers the plain text (markup removed):
    title / each paragraph / each note / "제출처: " + recipient, joined with "\\n", NFC-normalised.

    python -m app.document    # prints the seed document's canonical text and SHA-256
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import OpinionDocument

SEED_VERSION = "EPOXY_OPINION_V1"
SEED_TITLE = "e편한세상강동에코포레 에폭시 공사 관련 주민 의견서"
SEED_BODY = "\n\n".join(
    [
        "현재 예정되어 있는 **지하주차장 에폭시 전면공사**와 관련하여 공사의 필요성, 범위 및 진행 여부에 대해 "
        "주민들의 다양한 의견이 제기되고 있습니다.",
        "이에 본인은 현재 예정된 공사를 그대로 진행하기보다, **우선 착공을 보류하고 주민들에게 공사 관련 내용을 "
        "충분히 설명한 후 주민 의견을 수렴하여 공사 필요성과 범위, 진행 여부를 전면 재검토하여 주실 것을 요청합니다.**",
        "주민 의견 수렴 이후에도 공사 진행에 대한 충분한 공감대가 형성되지 않는 경우에는 **주민투표 또는 서면동의 등 "
        "주민들의 의사를 객관적으로 확인할 수 있는 절차를 거쳐 공사 진행 여부 자체를 다시 판단하여 주시기를 요청합니다.**",
        "따라서 주민 의견이 충분히 수렴되고 공사의 필요성 및 진행 여부에 대한 재검토가 이루어질 때까지 현재 예정된 "
        "**지하주차장 에폭시 전면공사의 착공을 보류하여 주시기 바랍니다.**",
        "본 요청은 공사를 무조건 반대하기 위한 것이 아니라, 다수 주민의 생활환경에 직접 영향을 미치는 공사인 만큼 "
        "**충분한 설명과 주민 의견 확인을 거쳐 공사의 필요성부터 다시 검토해 달라는 취지입니다.**",
    ]
)
# 하단 안내 as worded in the development brief. The PDF's second note ends with
# "…제출될 수 있음에 동의합니다." — online, that consent is a separate required checkbox (USAGE_CONSENT_TEXT).
SEED_NOTES = "\n".join(
    [
        "본인은 위 내용을 확인하고 본인의 의사에 따라 서명합니다.",
        "본 의견서는 주민 의견 전달을 위한 자료이며, 필요할 경우 관할 행정기관에 관련 민원·확인 요청 등의 "
        "참고자료로 제출될 수 있습니다.",
    ]
)
SEED_RECIPIENT = "e편한세상강동에코포레 입주자대표회의 회장 / 관리주체 / 관리사무소장 귀하"

# Form wording (not part of the editable document).
OPINION_CHOICES = {
    "AGREE": "위 주민의견서 내용에 동의합니다.",
    "DISAGREE": "위 주민의견서 내용에 동의하지 않습니다.",
}
OPINION_SUMMARY = {  # confirmation dialog: "의견: …"
    "AGREE": "의견서 내용에 동의합니다.",
    "DISAGREE": "의견서 내용에 동의하지 않습니다.",
}
OPINION_ADMIN_LABEL = {"AGREE": "동의", "DISAGREE": "동의하지 않음"}
OPINION_EXPORT_LABEL = {"AGREE": "의견서 내용에 동의", "DISAGREE": "의견서 내용에 동의하지 않음"}
STATEMENT_TEXT = "본인은 위 내용을 직접 확인하였으며, 본인의 의사에 따라 작성하였습니다."
USAGE_CONSENT_TEXT = (
    "본 의견서를 입주자대표회의, 관리주체, 관리사무소 및 필요한 경우 관할 행정기관에 "
    "주민 의견 자료로 제출·활용하는 것에 동의합니다."
)

TITLE_MAX = 200
BODY_MAX = 10_000
NOTES_MAX = 2_000
RECIPIENT_MAX = 300

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _normalise(value: str | None) -> str:
    text = unicodedata.normalize("NFC", value or "").replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS.sub("", text)


def _one_line(value: str | None) -> str:
    return " ".join(_normalise(value).split())


def split_paragraphs(body: str) -> list[str]:
    """Paragraphs (still carrying ** markers). A blank line separates paragraphs; other line breaks become spaces."""
    blocks = re.split(r"\n[ \t]*\n", _normalise(body).strip())
    return [" ".join(block.split()) for block in blocks if block.strip()]


def paragraph_runs(paragraph: str) -> list[tuple[str, bool]]:
    """[(text, bold), ...] for one paragraph."""
    return [(part, index % 2 == 1) for index, part in enumerate(paragraph.split("**")) if part]


def note_lines(notes: str) -> list[str]:
    lines = []
    for line in _normalise(notes).split("\n"):
        line = " ".join(line.split())
        if line.startswith("※"):
            line = line[1:].strip()
        if line:
            lines.append(line)
    return lines


def canonical_text(title: str, body: str, notes: str, recipient: str) -> str:
    lines = [title, *(p.replace("**", "") for p in split_paragraphs(body)), *note_lines(notes)]
    if recipient:
        lines.append("제출처: " + recipient)
    return unicodedata.normalize("NFC", "\n".join(lines))


def text_hash(title: str, body: str, notes: str, recipient: str) -> str:
    return hashlib.sha256(canonical_text(title, body, notes, recipient).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DocumentInput:
    title: str
    body: str
    notes: str
    recipient: str


def clean_document(title: str, body: str, notes: str, recipient: str) -> tuple[DocumentInput, dict[str, str]]:
    """Normalise an edited document and report problems per field."""
    errors: dict[str, str] = {}
    title = _one_line(title)
    paragraphs = split_paragraphs(body)
    body = "\n\n".join(paragraphs)
    notes = "\n".join(note_lines(notes))
    recipient = re.sub(r"^제출처\s*:?\s*", "", _one_line(recipient))

    if not title:
        errors["title"] = "제목을 입력해 주세요."
    elif len(title) > TITLE_MAX:
        errors["title"] = f"제목은 {TITLE_MAX}자 이내로 입력해 주세요."
    if not paragraphs:
        errors["body"] = "본문을 입력해 주세요."
    elif len(body) > BODY_MAX:
        errors["body"] = f"본문은 {BODY_MAX:,}자 이내로 입력해 주세요."
    elif any(paragraph.count("**") % 2 for paragraph in paragraphs):
        errors["body"] = "굵게 표시(**)의 앞뒤 짝이 맞지 않는 문단이 있습니다."
    if len(notes) > NOTES_MAX:
        errors["notes"] = f"하단 안내는 {NOTES_MAX:,}자 이내로 입력해 주세요."
    if len(recipient) > RECIPIENT_MAX:
        errors["recipient"] = f"제출처는 {RECIPIENT_MAX}자 이내로 입력해 주세요."
    return DocumentInput(title=title, body=body, notes=notes, recipient=recipient), errors


@dataclass(frozen=True)
class DocumentView:
    """What the templates need to show a document."""

    version: str
    title: str
    paragraphs: list[list[tuple[str, bool]]]
    notes: list[str]
    recipient: str
    text_hash: str
    created_at: datetime | None = None


def view_of(doc: OpinionDocument | DocumentInput, version: str = "") -> DocumentView:
    return DocumentView(
        version=getattr(doc, "version", version),
        title=doc.title,
        paragraphs=[paragraph_runs(p) for p in split_paragraphs(doc.body)],
        notes=note_lines(doc.notes),
        recipient=doc.recipient,
        text_hash=text_hash(doc.title, doc.body, doc.notes, doc.recipient),
        created_at=getattr(doc, "created_at", None),
    )


def active_document(db: Session) -> OpinionDocument | None:
    return db.scalar(select(OpinionDocument).where(OpinionDocument.is_active.is_(True)))


def seed_initial_document(db: Session) -> None:
    """Insert the first version when the table is empty (never touches existing versions)."""
    if db.scalar(select(func.count()).select_from(OpinionDocument)):
        return
    db.add(
        OpinionDocument(
            version_no=1,
            version=SEED_VERSION,
            title=SEED_TITLE,
            body=SEED_BODY,
            notes=SEED_NOTES,
            recipient=SEED_RECIPIENT,
            text_hash=text_hash(SEED_TITLE, SEED_BODY, SEED_NOTES, SEED_RECIPIENT),
            is_active=True,
        )
    )
    db.commit()


def next_version_label(current: str, number: int) -> str:
    """EPOXY_OPINION_V1 -> EPOXY_OPINION_V2 (the prefix of the current label is kept)."""
    return f"{re.sub(r'[0-9]+$', '', current) or 'V'}{number}"


class PublishError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # "missing" | "conflict" | "unchanged"


def publish_document(db: Session, data: DocumentInput, base_version: str, ip_address: str | None) -> OpinionDocument:
    """Make `data` the new active version. `base_version` is the version the editor started from."""
    current = db.scalar(select(OpinionDocument).where(OpinionDocument.is_active.is_(True)).with_for_update())
    if current is None:
        # Another admin published while this one waited for the row lock: a fresh read sees the new version.
        live = active_document(db)
        if live is not None:
            raise PublishError(
                "conflict",
                f"수정하는 동안 {live.version} 버전이 먼저 게시되었습니다. "
                "위의 현재 게시본을 확인한 뒤 다시 미리보기하고 게시해 주세요.",
            )
        raise PublishError("missing", "게시 중인 의견서를 찾지 못했습니다.")
    if current.version != base_version:
        raise PublishError(
            "conflict",
            f"수정하는 동안 {current.version} 버전이 먼저 게시되었습니다. "
            "위의 현재 게시본을 확인한 뒤 다시 미리보기하고 게시해 주세요.",
        )
    if (current.title, current.body, current.notes, current.recipient) == (
        data.title,
        data.body,
        data.notes,
        data.recipient,
    ):
        raise PublishError("unchanged", "현재 게시 중인 내용과 달라진 점이 없습니다.")
    number = (db.scalar(select(func.max(OpinionDocument.version_no))) or 0) + 1
    current.is_active = False
    db.flush()  # only one row may be active at a time (partial unique index)
    new = OpinionDocument(
        version_no=number,
        version=next_version_label(current.version, number),
        title=data.title,
        body=data.body,
        notes=data.notes,
        recipient=data.recipient,
        text_hash=text_hash(data.title, data.body, data.notes, data.recipient),
        is_active=True,
        created_ip=ip_address,
    )
    db.add(new)
    db.commit()
    return new


if __name__ == "__main__":
    print(canonical_text(SEED_TITLE, SEED_BODY, SEED_NOTES, SEED_RECIPIENT))
    print()
    print(f"{SEED_VERSION} sha256={text_hash(SEED_TITLE, SEED_BODY, SEED_NOTES, SEED_RECIPIENT)}")
