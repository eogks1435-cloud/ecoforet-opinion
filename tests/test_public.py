"""Public form: page content, submission, duplicate prevention, validation and failure handling."""

from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timedelta, timezone

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import document
from app.document import SEED_BODY, SEED_NOTES, SEED_RECIPIENT, SEED_TITLE, SEED_VERSION
from app.models import OpinionSubmission
from app.routes import public
from app.templating import format_kst

from .conftest import FETCH_HEADERS, ORIGIN, form_data, signature_data_url

VOTE_WORDS = ("투표", "찬성", "반대")
SEED_PARAGRAPHS = [p.replace("**", "") for p in document.split_paragraphs(SEED_BODY)]
SEED_HASH = document.text_hash(SEED_TITLE, SEED_BODY, SEED_NOTES, SEED_RECIPIENT)


def _count(db: Session) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(OpinionSubmission))


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def _without_document(html: str) -> str:
    """The page minus the opinion document itself (whose text legitimately says 주민투표 / 반대)."""
    return re.sub(r'<article class="document".*?</article>', "", html, flags=re.S)


def test_root_redirects_to_the_form(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/opinion"
    assert client.head("/", follow_redirects=False).status_code == 302  # link-preview bots and monitors
    assert client.head("/opinion").status_code == 200
    assert client.get("/favicon.ico").status_code == 204


def test_form_shows_the_document_word_for_word(client):
    html = client.get("/opinion").text
    article = re.search(r'<article class="document".*?</article>', html, flags=re.S).group(0)
    text = _strip_tags(article)
    assert len(SEED_PARAGRAPHS) == 5
    for paragraph in SEED_PARAGRAPHS:
        assert paragraph in text
    for note in SEED_NOTES.split("\n"):
        assert note in text
    assert SEED_RECIPIENT in text
    assert article.count("<strong>") == 5  # the PDF's bold passages
    assert "<strong>지하주차장 에폭시 전면공사</strong>와 관련하여" in article
    assert f"<h1 class=\"doc-title\">{SEED_TITLE}</h1>" in html
    assert f'name="document_version" value="{SEED_VERSION}"' in html
    assert "아래 주민의견서 내용을 충분히 확인하신 후 본인의 의사에 따라 작성해 주세요." in html
    assert "별도의 회원가입이나 로그인은 필요하지 않습니다." in html
    for label in document.OPINION_CHOICES.values():
        assert label in html
    assert document.STATEMENT_TEXT in html and document.USAGE_CONSENT_TEXT in html
    assert "의견서 제출하기" in html


def test_seed_hash_is_the_plain_text_hash():
    lines = [SEED_TITLE, *SEED_PARAGRAPHS, *SEED_NOTES.split("\n"), "제출처: " + SEED_RECIPIENT]
    assert SEED_HASH == hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    assert SEED_HASH.startswith("e499c9696b68")  # the hash V1 rows were stored with before versions moved to the DB


def test_the_ui_never_calls_it_a_vote(client, submit):
    pages = [_without_document(client.get("/opinion").text)]
    redirect = submit().json()["redirect"]
    pages.append(client.get(redirect).text)
    for html in pages:
        for word in VOTE_WORDS:
            assert word not in html, word


def test_agree_submission_is_stored_with_every_field(client, submit, db):
    before = datetime.now(timezone.utc)
    headers = dict(FETCH_HEADERS, **{"X-Forwarded-For": "203.0.113.7, 10.1.2.3"})
    response = submit(headers=headers, additional_comment="  주차장 공사 일정을\r\n공유해 주세요.  ")
    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True and body["redirect"].startswith("/opinion/complete?r=")

    row = db.scalars(select(OpinionSubmission)).one()
    assert (row.building, row.unit, row.resident_name) == ("101", "1203", "홍길동")
    assert row.opinion_choice == "AGREE"
    assert row.additional_comment == "주차장 공사 일정을\n공유해 주세요."
    assert row.statement_confirmed is True and row.usage_consent is True
    assert row.document_title == SEED_TITLE
    assert row.document_version == "EPOXY_OPINION_V1"
    assert row.document_text_hash == SEED_HASH
    assert row.ip_address == "203.0.113.7"
    assert row.user_agent == FETCH_HEADERS["User-Agent"]
    assert row.status == "ACTIVE" and row.invalidated_at is None
    submitted = row.submitted_at if row.submitted_at.tzinfo else row.submitted_at.replace(tzinfo=timezone.utc)
    assert before - timedelta(seconds=1) <= submitted <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert row.created_at is not None

    image = Image.open(io.BytesIO(row.signature_data))
    assert image.format == "PNG" and image.mode == "L" and image.size == (600, 300)

    done = client.get(body["redirect"])
    assert done.status_code == 200
    assert "주민의견서가 정상적으로 제출되었습니다." in done.text
    assert "참여해 주셔서 감사합니다." in done.text
    shown_no = re.search(r"제출번호: <strong[^>]*>([^<]+)<", done.text).group(1)
    assert shown_no == row.receipt_no == row.public_id.hex[:8].upper()
    assert f"제출일시: <strong>{format_kst(row.submitted_at)}</strong>" in done.text


def test_same_unit_is_blocked_including_spelling_variants(submit, db):
    assert submit().status_code == 201
    for building, unit in (("101", "1203"), ("0101", "1203"), ("101동", "1203호"), ("１０１", "１２０３")):
        response = submit(building=building, unit=unit, resident_name="김철수", opinion_choice="DISAGREE")
        assert response.status_code == 409
        assert response.json()["message"] == "이미 해당 동·호수로 제출된 의견서가 있습니다."
    assert _count(db) == 1


def test_another_unit_is_stored(submit, db):
    assert submit().status_code == 201
    assert submit(building="102", resident_name="김영희").status_code == 201
    assert _count(db) == 2


def test_disagree_is_stored_as_disagree(submit, db):
    assert submit(opinion_choice="DISAGREE", building="103", unit="501").status_code == 201
    row = db.scalars(select(OpinionSubmission)).one()
    assert row.opinion_choice == "DISAGREE"


def test_missing_fields_are_reported_per_field(client, db):
    response = client.post("/opinion", data={}, headers=FETCH_HEADERS)
    assert response.status_code == 422
    errors = response.json()["errors"]
    assert set(errors) == {
        "opinion_choice", "residence", "resident_name", "signature", "statement_confirmed", "usage_consent",
    }
    assert errors["residence"] == "동과 호수를 입력해 주세요."
    assert _count(db) == 0


def test_field_rules(submit, db):
    cases = [
        ({"opinion_choice": "YES"}, "opinion_choice"),
        ({"building": "abc"}, "residence"),
        ({"building": "0"}, "residence"),
        ({"unit": ""}, "residence"),
        ({"unit": "12345"}, "residence"),
        ({"resident_name": " 홍 "}, "resident_name"),
        ({"resident_name": "가" * 31}, "resident_name"),
        ({"signature": signature_data_url(blank=True)}, "signature"),
        ({"signature": "data:image/png;base64,bm90LWEtcG5n"}, "signature"),
        ({"signature": "data:image/jpeg;base64,AAAA"}, "signature"),
        ({"signature": signature_data_url(size=(4000, 2000))}, "signature"),
        ({"additional_comment": "가" * 1001}, "additional_comment"),
        ({"statement_confirmed": ""}, "statement_confirmed"),
        ({"usage_consent": ""}, "usage_consent"),
    ]
    for overrides, key in cases:
        response = submit(**overrides)
        assert response.status_code == 422, overrides
        assert key in response.json()["errors"], overrides
    assert _count(db) == 0


def test_a_1000_character_comment_is_accepted(submit):
    assert submit(additional_comment="가" * 1000).status_code == 201


def test_a_form_for_another_document_version_is_refused(submit, db):
    for version in ("EPOXY_OPINION_V0", ""):
        response = submit(document_version=version)
        assert response.status_code == 409
        assert response.json()["code"] == "document_changed"
    assert _count(db) == 0


def test_post_must_come_from_the_page(client, db):
    cases = [
        {"Origin": ORIGIN},  # no X-Requested-With: a plain cross-site form cannot add it
        {"X-Requested-With": "fetch", "Origin": "https://evil.example"},
        {"X-Requested-With": "fetch", "Origin": "null"},
    ]
    for headers in cases:
        response = client.post("/opinion", data=form_data(), headers=headers)
        assert response.status_code == 403, headers
    assert _count(db) == 0


def test_completion_page_needs_a_genuine_receipt(client, submit):
    for query in ("", "?r=garbage", "?r=" + "a" * 40):
        response = client.get("/opinion/complete" + query, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/opinion"
    token = submit().json()["redirect"].split("r=", 1)[1]
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    response = client.get("/opinion/complete?r=" + tampered, follow_redirects=False)
    assert response.status_code == 303


def test_database_failure_is_reported_and_nothing_is_stored(submit, db, monkeypatch):
    def broken_commit(self):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "commit", broken_commit)
    response = submit()
    monkeypatch.undo()
    assert response.status_code == 503
    assert response.json()["message"] == "일시적으로 제출하지 못했습니다. 잠시 후 다시 시도해 주세요."
    assert _count(db) == 0


def test_simultaneous_duplicate_is_stopped_by_the_database(submit, db, monkeypatch):
    assert submit().status_code == 201
    calls = {"n": 0}
    real_check = public._active_submission_exists

    def racing_check(session, building, unit):
        calls["n"] += 1
        return False if calls["n"] == 1 else real_check(session, building, unit)  # pre-check misses the race

    monkeypatch.setattr(public, "_active_submission_exists", racing_check)
    response = submit(resident_name="동시제출")
    assert response.status_code == 409
    assert response.json()["message"] == "이미 해당 동·호수로 제출된 의견서가 있습니다."
    assert _count(db) == 1


def test_unique_index_allows_one_active_row_per_unit(db):
    def row(status: str) -> OpinionSubmission:
        return OpinionSubmission(
            building="201", unit="101", resident_name="테스트", opinion_choice="AGREE", signature_data=b"png",
            statement_confirmed=True, usage_consent=True, document_title="t", document_version="v",
            document_text_hash="0" * 64, submitted_at=datetime.now(timezone.utc), status=status,
        )

    db.add_all([row("INVALIDATED"), row("INVALIDATED"), row("ACTIVE")])
    db.commit()
    db.add(row("ACTIVE"))
    try:
        db.commit()
        raise AssertionError("a second ACTIVE row for the same unit was accepted")
    except IntegrityError:
        db.rollback()


def test_health_robots_and_security_headers(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert "Disallow: /" in client.get("/robots.txt").text
    headers = client.get("/opinion").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert "geolocation=()" in headers["permissions-policy"]
    assert client.get("/docs").status_code == 404
    missing = client.get("/no-such-page")
    assert missing.status_code == 404 and "/opinion" in missing.text
