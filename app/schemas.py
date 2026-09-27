"""Server-side validation of the public form (the browser checks the same rules first)."""

from __future__ import annotations

import base64
import binascii
import io
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from .document import OPINION_CHOICES

NUMBER_MAX_DIGITS = 4
NAME_MIN_LETTERS = 2
NAME_MAX = 30
COMMENT_MAX = 1000
REASON_MAX = 200

SIGNATURE_FIELD_MAX = 400_000  # characters of the data URL, about 300 KB of PNG
SIGNATURE_MAX_SIDE = 1200
SIGNATURE_OUT_WIDTH = 600
SIGNATURE_MIN_INK_PIXELS = 80  # dark pixels needed to count as a signature (a dot or a blank pad is not)

_DATA_URL_PREFIX = "data:image/png;base64,"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps \t, \n and \r


@dataclass(frozen=True)
class SubmissionInput:
    opinion_choice: str
    building: str
    unit: str
    resident_name: str
    signature_png: bytes
    additional_comment: str | None


class FormErrors(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("invalid form")
        self.errors = errors


class SignatureError(ValueError):
    pass


def clean_text(raw: str | None) -> str:
    return _CONTROL_CHARS.sub("", unicodedata.normalize("NFC", raw or ""))


def parse_number(raw: str | None, suffix: str) -> tuple[str | None, str | None]:
    """Normalise 동/호수 input ('101', '0101', '101동', full-width digits) to '101'.

    Returns (value, None) or (None, 'missing' | 'invalid').
    """
    value = unicodedata.normalize("NFKC", raw or "").replace(" ", "").strip()
    if value.endswith(suffix):
        value = value[: -len(suffix)]
    if not value:
        return None, "missing"
    if not (value.isascii() and value.isdigit()):
        return None, "invalid"
    number = int(value)
    if number == 0 or len(str(number)) > NUMBER_MAX_DIGITS:
        return None, "invalid"
    return str(number), None


def _checked(value: str | None) -> bool:
    return (value or "").strip().lower() in {"on", "true", "1", "yes"}


def process_signature(data_url: str) -> bytes:
    """Decode the canvas PNG, check that something was drawn and re-encode it as a small grayscale PNG.

    Re-encoding also strips anything a crafted upload might carry besides pixels.
    """
    unreadable = SignatureError("서명 이미지를 읽지 못했습니다. 서명란에 다시 서명해 주세요.")
    if len(data_url) > SIGNATURE_FIELD_MAX or not data_url.startswith(_DATA_URL_PREFIX):
        raise unreadable
    try:
        raw = base64.b64decode(data_url[len(_DATA_URL_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        raise unreadable from None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size  # header only; checked before any pixel is decoded
            if img.format != "PNG" or not (50 <= width <= SIGNATURE_MAX_SIDE and 25 <= height <= SIGNATURE_MAX_SIDE):
                raise unreadable
            rgba = img.convert("RGBA")
    # ValueError: oversized text chunks; SyntaxError: Pillow's "broken PNG file"
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise unreadable from None

    flattened = Image.new("RGBA", rgba.size, (255, 255, 255, 255))  # transparent areas become white paper
    flattened.alpha_composite(rgba)
    gray = flattened.convert("L")
    if sum(gray.histogram()[:160]) < SIGNATURE_MIN_INK_PIXELS:
        raise SignatureError("서명이 확인되지 않습니다. 서명란에 다시 서명해 주세요.")
    if gray.width > SIGNATURE_OUT_WIDTH:
        new_height = max(1, round(gray.height * SIGNATURE_OUT_WIDTH / gray.width))
        gray = gray.resize((SIGNATURE_OUT_WIDTH, new_height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    gray.save(out, format="PNG", optimize=True)
    return out.getvalue()


def validate_submission(form: Mapping[str, str]) -> SubmissionInput:
    errors: dict[str, str] = {}

    choice = (form.get("opinion_choice") or "").strip()
    if choice not in OPINION_CHOICES:
        errors["opinion_choice"] = "본인의 의견을 선택해 주세요."

    building, building_problem = parse_number(form.get("building"), "동")
    unit, unit_problem = parse_number(form.get("unit"), "호")
    if building_problem == "missing" and unit_problem == "missing":
        errors["residence"] = "동과 호수를 입력해 주세요."
    elif building_problem:
        errors["residence"] = "동을 입력해 주세요." if building_problem == "missing" else "동을 숫자로 정확히 입력해 주세요."
    elif unit_problem:
        errors["residence"] = "호수를 입력해 주세요." if unit_problem == "missing" else "호수를 숫자로 정확히 입력해 주세요."

    name = " ".join(clean_text(form.get("resident_name")).split())
    if not name:
        errors["resident_name"] = "성명을 입력해 주세요."
    elif len(name.replace(" ", "")) < NAME_MIN_LETTERS:
        errors["resident_name"] = "성명을 2자 이상 입력해 주세요."
    elif len(name) > NAME_MAX:
        errors["resident_name"] = f"성명은 {NAME_MAX}자 이내로 입력해 주세요."

    signature_png = b""
    raw_signature = form.get("signature") or ""
    if not raw_signature:
        errors["signature"] = "서명해 주세요."
    else:
        try:
            signature_png = process_signature(raw_signature)
        except SignatureError as exc:
            errors["signature"] = str(exc)

    comment = clean_text(form.get("additional_comment")).replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(comment) > COMMENT_MAX:
        errors["additional_comment"] = "기타 전하고 싶은 말은 1,000자 이내로 작성해 주세요."

    if not _checked(form.get("statement_confirmed")):
        errors["statement_confirmed"] = "본인 의사 확인에 체크해 주세요."
    if not _checked(form.get("usage_consent")):
        errors["usage_consent"] = "제출·활용 동의에 체크해 주세요."

    if errors:
        raise FormErrors(errors)
    return SubmissionInput(
        opinion_choice=choice,
        building=building or "",
        unit=unit or "",
        resident_name=name,
        signature_png=signature_png,
        additional_comment=comment or None,
    )
