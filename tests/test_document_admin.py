"""Editing the opinion document from the admin page: preview, versioned publishing and old versions."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import document
from app.document import SEED_BODY, SEED_NOTES, SEED_RECIPIENT, SEED_TITLE, SEED_VERSION
from app.models import OpinionDocument, OpinionSubmission

from .conftest import ORIGIN, csrf_from

PDF_NOTE = (
    "본 의견서는 주민 의견 전달을 위한 자료이며, 필요할 경우 관할 행정기관에 관련 민원·확인 요청 등의 "
    "참고자료로 제출될 수 있음에 동의합니다."
)


def _fields(**overrides: str) -> dict[str, str]:
    fields = {"title": SEED_TITLE, "body": SEED_BODY, "notes": SEED_NOTES, "recipient": SEED_RECIPIENT}
    fields.update(overrides)
    return fields


def _post(admin, action: str, base_version: str = SEED_VERSION, csrf: str | None = None, **overrides: str):
    token = csrf if csrf is not None else csrf_from(admin.get("/admin/document").text)
    data = dict(_fields(**overrides), base_version=base_version, csrf=token)
    return admin.post(f"/admin/document/{action}", data=data, headers={"Origin": ORIGIN}, follow_redirects=False)


def _versions(db) -> list[tuple[str, bool]]:
    db.expire_all()
    return [(d.version, d.is_active) for d in db.scalars(select(OpinionDocument).order_by(OpinionDocument.version_no))]


def test_document_pages_require_login(client):
    for path in ("/admin/document", f"/admin/document/versions/{SEED_VERSION}"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    for action in ("preview", "publish"):
        response = client.post(f"/admin/document/{action}", data=_fields(), follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"


def test_editor_shows_the_current_text_with_markup(admin):
    html = admin.get("/admin/document").text
    assert SEED_VERSION in html
    assert f'value="{SEED_TITLE}"' in html
    assert "**지하주차장 에폭시 전면공사**와 관련하여" in html  # bold markup is editable
    assert "버전 기록" in html


def test_preview_shows_the_result_without_publishing(admin, db):
    response = _post(admin, "preview", notes=SEED_NOTES.split("\n")[0] + "\n" + PDF_NOTE)
    assert response.status_code == 200
    assert "미리보기 · 주민에게 이렇게 보입니다" in response.text
    assert PDF_NOTE in response.text
    assert "EPOXY_OPINION_V2(으)로 게시하기" in response.text
    assert _versions(db) == [(SEED_VERSION, True)]


def test_publishing_creates_a_version_and_old_submissions_keep_theirs(admin, submit, client, db):
    assert submit(building="101", unit="1203").status_code == 201  # signed under V1

    new_notes = SEED_NOTES.split("\n")[0] + "\n" + PDF_NOTE
    response = _post(admin, "publish", notes=new_notes)
    assert response.status_code == 303 and response.headers["location"] == "/admin/document"
    assert _versions(db) == [(SEED_VERSION, False), ("EPOXY_OPINION_V2", True)]
    assert "EPOXY_OPINION_V2 버전을 게시했습니다" in admin.get("/admin/document").text

    page = client.get("/opinion").text
    assert PDF_NOTE in page
    assert 'name="document_version" value="EPOXY_OPINION_V2"' in page

    stale = submit(building="102", unit="1203")  # a form still showing V1
    assert stale.status_code == 409 and stale.json()["code"] == "document_changed"
    assert submit(building="102", unit="1203", document_version="EPOXY_OPINION_V2").status_code == 201

    rows = {r.building: r for r in db.scalars(select(OpinionSubmission))}
    v2 = db.scalar(select(OpinionDocument).where(OpinionDocument.version == "EPOXY_OPINION_V2"))
    assert rows["101"].document_version == SEED_VERSION
    assert rows["101"].document_text_hash == document.text_hash(SEED_TITLE, SEED_BODY, SEED_NOTES, SEED_RECIPIENT)
    assert rows["102"].document_version == "EPOXY_OPINION_V2"
    assert rows["102"].document_text_hash == v2.text_hash != rows["101"].document_text_hash

    old = admin.get(f"/admin/document/versions/{SEED_VERSION}").text
    assert "저장된 문구와 일치합니다." in old
    assert SEED_NOTES.split("\n")[1] in old and PDF_NOTE not in old
    assert "유효 1건 / 전체 1건" in old


def test_publish_refuses_stale_edits_and_unchanged_text(admin, db):
    conflict = _post(admin, "publish", base_version="EPOXY_OPINION_V0", title="다른 제목")
    assert conflict.status_code == 409
    assert "먼저 게시되었습니다" in conflict.text
    unchanged = _post(admin, "publish")
    assert unchanged.status_code == 409
    assert "달라진 점이 없습니다" in unchanged.text
    assert _versions(db) == [(SEED_VERSION, True)]


def test_publish_validates_the_fields(admin, db):
    cases = [
        ({"title": "   "}, "제목을 입력해 주세요."),
        ({"body": "\n\n"}, "본문을 입력해 주세요."),
        ({"body": "굵게 **시작만 있는 문단"}, "짝이 맞지 않는"),
        ({"recipient": "가" * 301}, "제출처는 300자 이내"),
    ]
    for overrides, message in cases:
        response = _post(admin, "publish", **overrides)
        assert response.status_code == 422, overrides
        assert message in response.text, overrides
    assert _versions(db) == [(SEED_VERSION, True)]


def test_publish_requires_the_csrf_token(admin, db):
    response = _post(admin, "publish", csrf="forged", title="바뀐 제목")
    assert response.status_code == 400
    assert _versions(db) == [(SEED_VERSION, True)]


def test_document_text_is_escaped(admin, client):
    body = "<script>alert(1)</script> 문단\n\n**<b>굵게</b>** 끝"
    assert _post(admin, "publish", body=body).status_code == 303
    page = client.get("/opinion").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; 문단" in page
    assert "<strong>&lt;b&gt;굵게&lt;/b&gt;</strong> 끝" in page


def test_an_old_wording_can_be_restored_as_a_new_version(admin, db):
    assert _post(admin, "publish", title="임시 제목").status_code == 303
    editor = admin.get(f"/admin/document?from={SEED_VERSION}").text
    assert f'value="{SEED_TITLE}"' in editor
    assert re.search(r'name="base_version" value="EPOXY_OPINION_V2"', editor)
    assert _post(admin, "publish", base_version="EPOXY_OPINION_V2").status_code == 303
    versions = {d.version: d for d in db.scalars(select(OpinionDocument))}
    assert versions["EPOXY_OPINION_V3"].is_active
    assert versions["EPOXY_OPINION_V3"].text_hash == versions[SEED_VERSION].text_hash


def test_the_database_allows_one_active_version(db):
    db.add(
        OpinionDocument(
            version_no=99, version="X99", title="t", body="b", notes="", recipient="", text_hash="0" * 64,
            is_active=True,
        )
    )
    try:
        db.commit()
        raise AssertionError("a second active document version was accepted")
    except IntegrityError:
        db.rollback()


def test_markup_rules():
    assert document.split_paragraphs("첫 줄\n이어지는 줄\n\n\n두 번째 문단 ") == ["첫 줄 이어지는 줄", "두 번째 문단"]
    assert document.paragraph_runs("앞 **굵게** 뒤") == [("앞 ", False), ("굵게", True), (" 뒤", False)]
    assert document.note_lines("※ 첫째\n\n  둘째  ") == ["첫째", "둘째"]
    assert document.next_version_label("EPOXY_OPINION_V9", 10) == "EPOXY_OPINION_V10"
    cleaned, errors = document.clean_document(" 제목 ", "본문", "", "제출처 : 관리사무소장 귀하")
    assert not errors and cleaned.title == "제목" and cleaned.recipient == "관리사무소장 귀하"
