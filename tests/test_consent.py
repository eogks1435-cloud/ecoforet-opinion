"""Consent agenda (2026-09-29 brief): separate agenda, per-question answers, provisions, counts, exports, admin.

Only virtual test data is used (no real residents' names, signatures or units).
"""

from __future__ import annotations

import copy
import csv
import hashlib
import html as html_lib
import io
import itertools
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import consent
from app.database import engine, init_db
from app.document import SEED_TITLE
from app.main import app
from app.models import (
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    STATUS_WITHDRAWN,
    Agenda,
    ConsentAnswer,
    ConsentProvision,
    ConsentSubmission,
    ConsentVersion,
    OpinionDocument,
    OpinionSubmission,
)

from .conftest import ADMIN_PASSWORD, FETCH_HEADERS, ORIGIN, csrf_from, signature_data_url

CODE = consent.NEW_AGENDA_CODE
V1, V2 = f"{CODE}_V1", f"{CODE}_V2"
COMBOS = list(itertools.product(("AGREE", "DISAGREE"), repeat=3))  # the 8 answer combinations of q1..q3


# ------------------------------------------------------------------------------------------------ helpers
def ready_content() -> dict:
    """The brief's wording with virtual operator settings (tests only)."""
    content = consent.seed_content()
    content["participant_target"] = "테스트용 참여 대상(가상)"
    content["privacy"].update(
        controller="테스트 운영주체(가상)", officer="가상 책임자", contact="test@example.invalid",
        retention_records="테스트용 보유기간", retention_access="테스트용 30일", storage_location="테스트용 저장 위치",
        destruction_plan="테스트용 파기 계획",
    )
    for recipient in content["recipients"]:
        recipient["retention"] = f"테스트용 {recipient['short_name']} 보유기간"
        recipient["delivery"] = f"테스트용 {recipient['short_name']} 제출 방식"
    content["overseas"]["alternative"] = "테스트용 다른 참여 방법"
    return content


def agenda_of(db: Session) -> Agenda:
    db.expire_all()
    return consent.agenda_by_code(db, CODE)


def publish_ready(db: Session, *, public: bool = True, content: dict | None = None) -> ConsentVersion:
    agenda = agenda_of(db)
    consent.save_draft(db, agenda, content or ready_content(), "127.0.0.1")
    version = consent.publish(db, agenda, "127.0.0.1")
    if public:
        consent.make_public(db, agenda)
    return version


def consent_form(answers=("AGREE", "AGREE", "AGREE"), company="AGREE", district="AGREE", **overrides) -> dict:
    data = {
        "agenda_code": CODE,
        "version_label": V1,
        "client_token": secrets.token_urlsafe(18),
        "building": "101",
        "unit": "1203",
        "resident_name": "가상주민",
        "signature": signature_data_url(),
        "answer_q1": answers[0],
        "answer_q2": answers[1],
        "answer_q3": answers[2],
        "privacy_consent": "AGREE",
        "overseas_consent": "AGREE",
        "provide_company": company,
        "provide_district": district,
        "final_confirmed": "on",
    }
    data.update(overrides)
    return data


def post_consent(client: TestClient, data: dict | None = None, **kwargs):
    return client.post("/opinion/consent", data=data or consent_form(**kwargs), headers=FETCH_HEADERS)


def rows_of(db: Session) -> list[ConsentSubmission]:
    db.expire_all()
    return db.scalars(select(ConsentSubmission).order_by(ConsentSubmission.id)).all()


def admin_csrf(client: TestClient) -> str:
    return csrf_from(client.get("/admin/agendas").text)


def read_csv(response) -> list[list[str]]:
    assert response.status_code == 200
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


def pdf_answers(text: str) -> list[str]:
    """The marked choice of each question in a record PDF (● marks the chosen option)."""
    out = []
    for block in re.split(r"질문 \d+\. ", text)[1:]:
        line = next(line for line in block.splitlines() if "●" in line or "○" in line)
        out.append("AGREE" if line.strip().startswith("●") else "DISAGREE")
    return out


def editor_fields(page: str) -> dict[str, str]:
    """Every field of the draft editor form, as a browser would send it unchanged."""
    start = page.index('id="draft-form"')
    form = page[start:page.index("</form>", start)]
    fields: dict[str, str] = {}
    for match in re.finditer(r"<input[^>]*>", form):
        tag = match.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        if 'type="checkbox"' in tag:
            if " checked" in tag:
                fields[name.group(1)] = value.group(1)
            continue
        fields[name.group(1)] = value.group(1) if value else ""
    for match in re.finditer(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', form, re.S):
        fields[match.group(1)] = match.group(2)
    for match in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        selected = re.search(r'<option value="([^"]*)"[^>]*selected', match.group(2))
        fields[match.group(1)] = selected.group(1) if selected else ""
    return {key: html_lib.unescape(value) for key, value in fields.items()}


def legacy_fingerprint(db: Session) -> tuple[str, int, int]:
    """Every stored column of the legacy tables (documents and submissions, signatures included)."""
    db.expire_all()
    submissions = db.execute(select(OpinionSubmission.__table__).order_by(OpinionSubmission.id)).all()
    documents = db.execute(select(OpinionDocument.__table__).order_by(OpinionDocument.id)).all()
    digest = hashlib.sha256(repr((submissions, documents)).encode("utf-8")).hexdigest()
    return digest, len(submissions), len(documents)


# ------------------------------------------------------------------------------ agendas and migration
def test_start_keeps_the_legacy_form_public_and_the_new_agenda_unpublished(client, db):
    agendas = {a.code: a for a in db.scalars(select(Agenda)).all()}
    legacy, new = agendas[consent.LEGACY_AGENDA_CODE], agendas[CODE]
    assert legacy.kind == "legacy_opinion" and legacy.is_public and legacy.accepting
    assert new.kind == "consent" and not new.is_public
    assert db.scalar(select(func.count()).select_from(ConsentVersion)) == 0
    assert consent.draft_of(db, new).content == consent.seed_content()
    page = client.get("/opinion")
    assert 'id="opinion-form"' in page.text and "consent-form" not in page.text
    # The new agenda is not public: its submission endpoint refuses.
    response = post_consent(client)
    assert response.status_code == 409 and response.json()["code"] == "document_changed"


def test_repeated_start_is_additive_and_keeps_legacy_rows_byte_for_byte(client, submit, admin, db):
    assert submit(unit="1201").status_code == 201
    assert submit(unit="1202", opinion_choice="DISAGREE", additional_comment="가상 의견").status_code == 201
    row = db.scalar(select(OpinionSubmission).where(OpinionSubmission.unit == "1202"))
    page = admin.get("/admin")
    admin.post(f"/admin/submissions/{row.public_id}/invalidate",
               data={"csrf": csrf_from(page.text), "reason": "가상 사유"}, follow_redirects=False)
    before = legacy_fingerprint(db)
    assert before[1:] == (2, 1)

    init_db()  # a restart / redeploy runs this again
    init_db()
    assert legacy_fingerprint(db) == before
    assert db.scalar(select(func.count()).select_from(Agenda)) == 2

    publish_ready(db)
    assert post_consent(client, unit="1201").status_code == 201
    assert legacy_fingerprint(db) == before  # the new agenda never touches the legacy rows


def test_publish_is_blocked_until_the_operator_settings_are_filled(admin, db):
    agenda = agenda_of(db)
    with pytest.raises(consent.PublishError) as caught:
        consent.publish(db, agenda, None)
    db.rollback()
    message = str(caught.value)
    for label in ("참여 대상", "수집·관리 주체", "관리책임자", "연락처", "동의자료 보유", "접속 IP", "케이비아주",
                  "강동구", "파기 대상", "제출 방식", "다른 참여 방법"):
        assert label in message
    assert "저장 위치" not in message  # the overseas-transfer section says where the data is stored
    csrf = admin_csrf(admin)
    page = admin.get(f"/admin/consent/{CODE}/edit").text
    assert "게시 전에 확인할 항목" in page and "disabled" in page
    draft_hash = re.search(r'name="draft_hash" value="([^"]+)"', page).group(1)
    admin.post(f"/admin/consent/{CODE}/publish", data={"csrf": csrf, "draft_hash": draft_hash})
    admin.post(f"/admin/agendas/{CODE}/public", data={"csrf": csrf})
    assert db.scalar(select(func.count()).select_from(ConsentVersion)) == 0
    assert not agenda_of(db).is_public
    assert 'id="opinion-form"' in admin.get("/opinion").text  # residents still see the legacy form
    # The admin preview marks what is missing; the public page never shows such a marker.
    preview = admin.get(f"/admin/consent/{CODE}/preview").text
    assert "[게시 전 입력: 실제 운영 주체의 명칭]" in preview


def test_placeholder_bold_and_access_info_consistency_block_publishing():
    content = ready_content()
    assert consent.publish_blockers(content) == []
    placeholder = copy.deepcopy(content)
    placeholder["intro"] = "[게시 전 입력: 무엇]"
    assert any("[게시 전 입력" in p for p in consent.publish_blockers(placeholder))
    bold = copy.deepcopy(content)
    bold["sections"][0]["body"] = "**굵게 시작만"
    assert any("굵게" in p for p in consent.publish_blockers(bold))
    no_access = copy.deepcopy(content)
    no_access["privacy"]["collect_access_info"] = False
    assert any("접속 IP" in p for p in consent.publish_blockers(no_access))  # wording still mentions IP
    no_access["principles"] = no_access["principles"].replace(
        "접속 IP 주소와 브라우저 정보는 위 제출자료에 포함하지 않습니다.\n\n", "")
    assert consent.publish_blockers(no_access) == []
    assert "IP" not in consent.generated_items(no_access)


def test_hash_covers_every_part_of_the_wording():
    base = ready_content()
    digest = consent.content_hash(base)
    assert digest == consent.content_hash(copy.deepcopy(base))
    assert digest == consent.content_hash(consent.normalize_content(base))
    changes = [
        lambda c: c["sections"][2].update(body=c["sections"][2]["body"] + " "),
        lambda c: c["sections"][2].update(body=c["sections"][2]["body"] + "추가"),
        lambda c: c["questions"][0].update(agree_label="찬성"),
        lambda c: c["questions"][1].update(active=False),
        lambda c: c["privacy"].update(collect_access_info=False),
        lambda c: c["privacy"].update(refusal="다른 안내"),
        lambda c: c["recipients"][1].update(retention="다른 기간"),
        lambda c: c.update(final_checkbox="다른 확인 문구"),
        lambda c: c.update(recipient_line="다른 수신"),
        lambda c: c["overseas"].update(country="다른 국가"),
    ]
    changed = 0
    for change in changes:
        content = copy.deepcopy(base)
        change(content)
        if consent.content_hash(consent.normalize_content(content)) != digest:  # as publish() does
            changed += 1
    assert changed == len(changes) - 1  # trailing spaces are normalised away, everything else counts


def test_a_stored_version_verifies_on_its_stored_json_even_after_the_schema_grows(admin, db, monkeypatch):
    version = publish_ready(db)
    assert consent.version_hash_ok(version)
    monkeypatch.setitem(consent.SEED_CONTENT, "later_field", "")  # a field added by some later change
    assert "later_field" in consent.normalize_content(version.content)
    db.expire_all()
    assert consent.version_hash_ok(db.get(ConsentVersion, version.id))
    assert "저장된 원문과 확인값 일치" in admin.get(f"/admin/consent/{CODE}/versions/{V1}").text


def test_bold_opened_on_one_line_continues_on_the_next_line_of_the_paragraph():
    blocks = consent.rich_blocks("**첫째 줄\n둘째 줄** 보통\n\n다음 문단")
    assert blocks == [[[("첫째 줄", True)], [("둘째 줄", True), (" 보통", False)]], [[("다음 문단", False)]]]


# ---------------------------------------------------------------------------------- resident page and form
def test_public_page_after_the_switch_shows_real_unselected_choices(client, db):
    publish_ready(db)
    page = client.get("/opinion").text
    assert 'id="consent-form"' in page and f'name="version_label" value="{V1}"' in page
    assert page.count('<fieldset class="question"') == 7  # 3 questions + privacy + overseas + 2 recipients
    radios = re.findall(r'<input class="choice-input" type="radio"[^>]*>', page)
    assert len(radios) == 14 and not any("checked" in radio for radio in radios)
    assert "5-1. 개인정보 국외 이전 동의" in page and "Render Services, Inc." in page and "테스트용 다른 참여 방법" in page
    assert page.index("5. 개인정보 수집·이용 동의") < page.index("5-1. 개인정보 국외 이전") < page.index("6. 개인정보 제3자")
    assert 'name="final_confirmed"' in page and "checked" not in re.search(r'<input[^>]*name="final_confirmed"[^>]*>', page).group(0)
    for word in ("전자투표", "온라인 투표", "해임 확정", "[게시 전 입력", "□ 동의"):
        assert word not in page
    assert "관리사무소장 교체 및 관리업무 특별조사 요청" in page and "온라인 동의서" in page
    assert "수신" in page and "주식회사 케이비아주 대표이사 귀하" in page


def test_the_legacy_form_is_refused_after_the_switch(client, submit, db):
    publish_ready(db)
    response = submit()
    assert response.status_code == 409 and response.json()["code"] == "document_changed"
    assert db.scalar(select(func.count()).select_from(OpinionSubmission)) == 0


@pytest.mark.parametrize(
    ("change", "error_key"),
    [
        ({"answer_q2": ""}, "answer_q2"),
        ({"answer_q3": "MAYBE"}, "answer_q3"),
        ({"privacy_consent": "DISAGREE"}, "privacy_consent"),
        ({"privacy_consent": ""}, "privacy_consent"),
        ({"overseas_consent": ""}, "overseas_consent"),
        ({"overseas_consent": "DISAGREE"}, "overseas_consent"),
        ({"provide_district": ""}, "provide_district"),
        ({"final_confirmed": ""}, "final_confirmed"),
        ({"building": ""}, "residence"),
        ({"resident_name": "가"}, "resident_name"),
        ({"signature": signature_data_url(blank=True)}, "signature"),
    ],
)
def test_incomplete_forms_are_rejected_with_the_field(client, db, change, error_key):
    publish_ready(db)
    response = post_consent(client, consent_form(**change))
    assert response.status_code == 422 and error_key in response.json()["errors"]
    assert rows_of(db) == []


def test_eight_answer_combinations_are_stored_counted_exported_and_printed(admin, db):
    publish_ready(db)
    for index, combo in enumerate(COMBOS):
        assert post_consent(admin, answers=combo, unit=str(1201 + index)).status_code == 201
    rows = rows_of(db)
    assert [tuple(row.answer_map()[k] for k in ("q1", "q2", "q3")) for row in rows] == COMBOS

    stats = consent.consent_stats(db, agenda_of(db))
    assert stats.active == 8
    assert [(q.key, q.agree, q.disagree) for q in stats.questions] == [("q1", 4, 4), ("q2", 4, 4), ("q3", 4, 4)]
    assert (stats.all_agree, stats.some_agree, stats.none_agree) == (1, 6, 1)
    assert "전체 동의 세대수" not in admin.get(f"/admin/consent/{CODE}").text.replace("'전체 동의 세대수'가 아닙니다", "")

    table = read_csv(admin.get(f"/admin/consent/{CODE}/export.csv"))
    header, body = table[0], table[1:]
    columns = [header.index(title) for title in header if title.startswith("질문 ")]
    label = {"AGREE": "동의", "DISAGREE": "동의하지 않음"}
    assert [tuple(line[i] for i in columns) for line in body] == [tuple(label[a] for a in c) for c in COMBOS]
    assert not any("IP" in title or "브라우저" in title for title in header)

    for row, combo in zip(rows, COMBOS):
        response = admin.get(f"/admin/consent-submissions/{row.public_id}/pdf")
        assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
        text = pdf_text(response.content)
        assert pdf_answers(text) == list(combo)
        assert row.receipt_no in text and f"{row.building}동 {row.unit}호" in text and "가상주민" in text
        assert row.content_hash in text.replace("\n", "") and V1 in text
        assert "내부 열람용" in text


def test_four_provision_combinations_decide_each_recipients_files(admin, db):
    publish_ready(db)
    combos = list(itertools.product(("AGREE", "DISAGREE"), repeat=2))
    for index, (company, district) in enumerate(combos):
        response = post_consent(admin, unit=str(2101 + index), company=company, district=district,
                                answers=("AGREE", "DISAGREE", "AGREE"))
        assert response.status_code == 201
    rows = {row.unit: row for row in rows_of(db)}
    expected = {
        "company": {str(2101 + i) for i, (c, _d) in enumerate(combos) if c == "AGREE"},
        "district": {str(2101 + i) for i, (_c, d) in enumerate(combos) if d == "AGREE"},
    }
    agenda = agenda_of(db)
    stats = {r.key: r for r in consent.consent_stats(db, agenda).recipients}
    for key, units in expected.items():
        assert {row.unit for row in consent.eligible_submissions(db, agenda, key)} == units
        assert stats[key].agreed == 2 and stats[key].declined == 2 and stats[key].eligible == 2
        assert stats[key].named == {"q1": 2, "q3": 2}  # q2 was answered DISAGREE by everyone
        table = read_csv(admin.get(f"/admin/consent/{CODE}/recipients/{key}.csv"))
        assert {line[2] for line in table[1:]} == units
        header = " ".join(table[0])
        assert "IP" not in header and "브라우저" not in header and "제공" not in header and "상태" not in header
        listing = admin.get(f"/admin/consent/{CODE}/recipients/{key}.pdf")
        text = pdf_text(listing.content)
        assert listing.status_code == 200 and "기명 동의자 명단" in text
        for unit, row in rows.items():
            assert (row.receipt_no in text) == (unit in units)
            one = admin.get(f"/admin/consent-submissions/{row.public_id}/pdf/{key}")
            assert one.status_code == (200 if unit in units else 409)
            if unit in units:
                copy_text = pdf_text(one.content)
                other = "나. 서울특별시 강동구에 대한 제공" if key == "company" else "가. 주식회사 케이비아주에 대한 제공"
                assert "제출용" in copy_text and other not in copy_text  # only this recipient's part
        filtered = admin.get(f"/admin/consent/{CODE}?recipient={key}&provision=declined").text
        assert filtered.count("/pdf\" target") == 2


def test_an_all_disagree_record_is_accepted_but_never_an_agreer(admin, db):
    publish_ready(db)
    assert post_consent(admin, answers=("DISAGREE",) * 3).status_code == 201
    row = rows_of(db)[0]
    agenda = agenda_of(db)
    stats = consent.consent_stats(db, agenda)
    assert stats.active == 1 and stats.none_agree == 1
    assert all(q.agree == 0 for q in stats.questions)
    for recipient in stats.recipients:
        assert recipient.agreed == 1 and recipient.named == {} and recipient.eligible == 0
        assert not consent.eligible_for(row, recipient.key)
        assert len(read_csv(admin.get(f"/admin/consent/{CODE}/recipients/{recipient.key}.csv"))) == 1
        assert admin.get(f"/admin/consent-submissions/{row.public_id}/pdf/{recipient.key}").status_code == 409


def test_declining_both_recipients_still_submits_internally(client, db):
    publish_ready(db)
    response = post_consent(client, company="DISAGREE", district="DISAGREE")
    assert response.status_code == 201
    row = rows_of(db)[0]
    assert {p.recipient_key: p.agreed for p in row.provisions} == {"company": False, "district": False}
    assert row.privacy_consent and row.final_confirmed


def test_formula_like_names_are_neutralised_in_every_csv(admin, db):
    publish_ready(db)
    assert post_consent(admin, resident_name="=HYPERLINK(1)").status_code == 201
    internal = read_csv(admin.get(f"/admin/consent/{CODE}/export.csv"))
    company = read_csv(admin.get(f"/admin/consent/{CODE}/recipients/company.csv"))
    assert internal[1][3] == "'=HYPERLINK(1)" and company[1][3] == "'=HYPERLINK(1)"


# --------------------------------------------------------------------------- duplicates, versions, retries
def test_a_unit_that_joined_the_old_agenda_can_join_the_new_one(client, submit, db):
    assert submit(building="101", unit="1203").status_code == 201
    publish_ready(db)
    assert post_consent(client, building="101", unit="1203").status_code == 201
    legacy = db.scalar(select(OpinionSubmission))
    assert legacy.status == STATUS_ACTIVE and legacy.opinion_choice == "AGREE"


def test_one_valid_record_per_unit_even_after_a_new_version(client, db):
    publish_ready(db)
    assert post_consent(client, unit="1203").status_code == 201
    duplicate = post_consent(client, unit="1203")
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "duplicate"

    content = ready_content()
    content["intro"] = "문구를 고친 두 번째 버전"
    agenda = agenda_of(db)
    consent.save_draft(db, agenda, content, None)
    assert consent.publish(db, agenda, None).label == V2
    again = post_consent(client, unit="1203", version_label=V2)
    assert again.status_code == 409 and again.json()["code"] == "duplicate"
    assert post_consent(client, unit="1204", version_label=V2).status_code == 201
    assert [row.version_label for row in rows_of(db)] == [V1, V2]


def test_a_form_opened_before_a_new_version_is_not_stored(client, db):
    publish_ready(db)
    page = client.get("/opinion").text
    assert f'value="{V1}"' in page
    content = ready_content()
    content["questions"][0]["text"] = "바뀐 질문 1 문구"
    agenda = agenda_of(db)
    consent.save_draft(db, agenda, content, None)
    consent.publish(db, agenda, None, merge_reworded=True)
    response = post_consent(client)  # still carries V1
    assert response.status_code == 409 and response.json()["code"] == "document_changed"
    assert "다시 입력" in response.json()["message"]
    assert rows_of(db) == []


def test_a_retry_after_a_lost_answer_gets_its_receipt_even_after_a_new_version(client, db):
    publish_ready(db)
    form = consent_form(unit="2203")
    assert post_consent(client, form).status_code == 201  # stored, but pretend the answer never arrived
    content = ready_content()
    content["intro"] = "그사이 게시된 두 번째 버전"
    agenda = agenda_of(db)
    consent.save_draft(db, agenda, content, None)
    consent.publish(db, agenda, None)
    consent.set_accepting(db, agenda_of(db), False)
    retry = post_consent(client, form)
    assert retry.status_code == 201 and rows_of(db)[0].receipt_no in client.get(retry.json()["redirect"]).text
    assert len(rows_of(db)) == 1


def test_a_retried_request_returns_the_first_receipt_without_a_second_record(client, db):
    publish_ready(db)
    form = consent_form()
    first, second = post_consent(client, form), post_consent(client, form)
    assert first.status_code == second.status_code == 201
    receipts = [re.search(r"제출번호[^A-F0-9]*([A-F0-9]{8})", client.get(r.json()["redirect"]).text).group(1)
                for r in (first, second)]
    assert receipts[0] == receipts[1] == rows_of(db)[0].receipt_no
    changed = post_consent(client, dict(form, answer_q1="DISAGREE"))
    assert changed.status_code == 409 and changed.json()["code"] == "already_submitted"
    assert len(rows_of(db)) == 1


def test_simultaneous_submissions_for_one_unit_leave_one_valid_record(db):
    publish_ready(db)
    clients = [TestClient(app) for _ in range(6)]
    barrier = threading.Barrier(len(clients))

    def send(client: TestClient):
        barrier.wait()
        return post_consent(client, unit="3303").status_code

    with ThreadPoolExecutor(len(clients)) as pool:
        codes = list(pool.map(send, clients))
    assert codes.count(201) == 1 and all(code in (201, 409) for code in codes)
    assert [row.unit for row in rows_of(db)] == ["3303"]


def test_a_storage_failure_is_never_reported_as_success(client, db, monkeypatch):
    publish_ready(db)

    def broken_commit(self):
        raise OperationalError("INSERT INTO consent_submissions", {}, Exception("simulated failure"))

    monkeypatch.setattr(Session, "commit", broken_commit)
    response = post_consent(client)
    monkeypatch.undo()
    assert response.status_code == 503 and response.json()["ok"] is False and "redirect" not in response.json()
    assert rows_of(db) == []


def test_records_and_signatures_survive_a_restart(db):
    publish_ready(db)
    with TestClient(app) as first:
        assert post_consent(first, unit="1501").status_code == 201
    stored = db.scalar(select(ConsentSubmission.signature_data))
    engine.dispose()  # drop every pooled connection, as a process restart would
    with TestClient(app) as second:
        page = second.get("/admin/login")
        second.post("/admin/login", data={"password": ADMIN_PASSWORD, "csrf": csrf_from(page.text)},
                    headers={"Origin": ORIGIN})
        row = rows_of(db)[0]
        detail = second.get(f"/admin/consent-submissions/{row.public_id}")
        assert detail.status_code == 200 and "1501호" in detail.text
        image = second.get(f"/admin/consent-submissions/{row.public_id}/signature.png")
        assert image.status_code == 200 and image.content == bytes(stored)


def test_paused_agenda_keeps_the_page_but_refuses_submissions(client, db):
    publish_ready(db)
    consent.set_accepting(db, agenda_of(db), False)
    page = client.get("/opinion").text
    assert "일시 중지" in page and re.search(r'id="submit-button"[^>]*disabled', page)
    response = post_consent(client)
    assert response.status_code == 423 and rows_of(db) == []


def test_completion_page_names_the_consent_form(client, db):
    publish_ready(db)
    response = post_consent(client)
    page = client.get(response.json()["redirect"]).text
    assert "온라인 동의서" in page and rows_of(db)[0].receipt_no in page


# ------------------------------------------------------------------------------ admin status and withdrawal
def test_invalidation_and_withdrawal_keep_the_record_and_leave_the_counts(admin, db):
    publish_ready(db)
    post_consent(admin, unit="1201")
    post_consent(admin, unit="1202")
    first, second = rows_of(db)
    csrf = admin_csrf(admin)
    back = f"/admin/consent-submissions/{first.public_id}"
    missing_reason = admin.post(f"{back}/status", data={"csrf": csrf, "action": "invalidate", "reason": "", "back": back})
    assert missing_reason.status_code == 200 and "사유를 입력" in missing_reason.text
    admin.post(f"{back}/status", data={"csrf": csrf, "action": "invalidate", "reason": "가상 중복 확인", "back": back})
    admin.post(f"/admin/consent-submissions/{second.public_id}/status",
               data={"csrf": csrf, "action": "withdraw", "reason": "가상 본인 요청", "delivered": "1", "back": back})
    first, second = rows_of(db)
    assert (first.status, first.status_reason, first.already_delivered) == (STATUS_INVALIDATED, "가상 중복 확인", False)
    assert (second.status, second.status_reason, second.already_delivered) == (STATUS_WITHDRAWN, "가상 본인 요청", True)
    assert first.status_changed_at is not None and bytes(first.signature_data)
    stats = consent.consent_stats(db, agenda_of(db))
    assert (stats.active, stats.invalidated, stats.withdrawn) == (0, 1, 1)
    assert all(q.agree == 0 for q in stats.questions)
    assert len(read_csv(admin.get(f"/admin/consent/{CODE}/recipients/company.csv"))) == 1
    internal = read_csv(admin.get(f"/admin/consent/{CODE}/export.csv"))
    assert [line[internal[0].index("상태")] for line in internal[1:]] == ["무효", "철회"]
    pdf = pdf_text(admin.get(f"/admin/consent-submissions/{first.public_id}/pdf").content)
    assert "무효 처리된 제출입니다" in pdf and "가상 중복 확인" in pdf
    # The unit may submit again within this agenda.
    assert post_consent(admin, unit="1201").status_code == 201


def test_withdrawing_one_recipient_removes_the_record_from_that_recipient_only(admin, db):
    publish_ready(db)
    post_consent(admin, unit="1601")
    row = rows_of(db)[0]
    csrf = admin_csrf(admin)
    url = f"/admin/consent-submissions/{row.public_id}/provisions/company/withdraw"
    admin.post(url, data={"csrf": csrf, "reason": "가상 철회 요청", "delivered": "1"})
    row = rows_of(db)[0]
    provision = row.provision_map()["company"]
    assert provision.withdrawn_at is not None and provision.withdrawn_reason == "가상 철회 요청"
    assert provision.already_delivered is True and provision.agreed is True
    stats = {r.key: r for r in consent.consent_stats(db, agenda_of(db)).recipients}
    assert (stats["company"].agreed, stats["company"].withdrawn, stats["company"].named) == (0, 1, {})
    assert stats["district"].named == {"q1": 1, "q2": 1, "q3": 1}
    assert consent.consent_stats(db, agenda_of(db)).active == 1  # still counted internally
    assert len(read_csv(admin.get(f"/admin/consent/{CODE}/recipients/company.csv"))) == 1
    assert len(read_csv(admin.get(f"/admin/consent/{CODE}/recipients/district.csv"))) == 2
    assert admin.get(f"/admin/consent-submissions/{row.public_id}/pdf/company").status_code == 409


def test_access_info_can_be_erased_after_its_retention_period(admin, db):
    publish_ready(db)
    post_consent(admin, unit="1901")
    post_consent(admin, unit="1902")
    csrf = admin_csrf(admin)
    refused = admin.post(f"/admin/consent/{CODE}/access-info/purge", data={"csrf": csrf, "days": "0"})
    assert "1 이상" in refused.text and all(row.ip_address for row in rows_of(db))
    dashboard = admin.get(f"/admin/consent/{CODE}").text
    assert "접속 정보가 남아 있는 기록: <strong>2건</strong>" in dashboard and "js/admin.js" in dashboard
    admin.post(f"/admin/consent/{CODE}/access-info/purge", data={"csrf": csrf, "days": "30"})
    assert all(row.ip_address and row.user_agent for row in rows_of(db))  # both are younger than 30 days
    old = rows_of(db)[0]
    db.execute(ConsentSubmission.__table__.update().where(ConsentSubmission.id == old.id).values(
        submitted_at=old.submitted_at.replace(year=old.submitted_at.year - 1)))
    db.commit()
    page = admin.post(f"/admin/consent/{CODE}/access-info/purge", data={"csrf": csrf, "days": "30"})
    assert "1건의 접속 IP·브라우저 정보를 지웠습니다" in page.text
    first, second = rows_of(db)
    assert (first.ip_address, first.user_agent) == (None, None) and second.ip_address and second.user_agent
    assert first.resident_name == "가상주민" and bytes(first.signature_data) and first.answer_map()
    anonymous = TestClient(app)
    response = anonymous.post(f"/admin/consent/{CODE}/access-info/purge", data={"days": "0"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert rows_of(db)[1].ip_address


# ------------------------------------------------------------------------------------------ access control
def test_nothing_personal_or_unpublished_is_reachable_without_login(client, db):
    publish_ready(db)
    post_consent(client, resident_name="가상비밀")
    row = rows_of(db)[0]
    record = f"/admin/consent-submissions/{row.public_id}"
    pages = [
        "/admin/agendas", "/admin/consent", "/admin/consent/edit", f"/admin/consent/{CODE}",
        f"/admin/consent/{CODE}/edit", f"/admin/consent/{CODE}/preview", f"/admin/consent/{CODE}/versions/{V1}",
        f"/admin/consent/{CODE}/export.csv", f"/admin/consent/{CODE}/recipients/company.csv",
        f"/admin/consent/{CODE}/recipients/district.pdf", record, f"{record}/pdf", f"{record}/pdf/company",
    ]
    for url in pages:
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login", url
        assert "가상비밀" not in response.text
    assert client.get(f"{record}/signature.png").status_code == 401
    actions = [
        (f"/admin/agendas/{consent.LEGACY_AGENDA_CODE}/public", {}),
        (f"/admin/agendas/{CODE}/accepting", {"value": "0"}),
        ("/admin/agendas/new", {"name": "가상 안건"}),
        (f"/admin/consent/{CODE}/draft", {"title": "변조"}),
        (f"/admin/consent/{CODE}/draft/load", {"label": V1}),
        (f"/admin/consent/{CODE}/publish", {"draft_hash": "x"}),
        (f"{record}/status", {"action": "invalidate", "reason": "변조"}),
        (f"{record}/provisions/company/withdraw", {"reason": "변조"}),
    ]
    for url, data in actions:
        response = client.post(url, data=data, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login", url
    agenda = agenda_of(db)
    assert agenda.is_public and agenda.accepting
    assert db.scalar(select(func.count()).select_from(Agenda)) == 2
    assert consent.draft_of(db, agenda).content["title"] != "변조"
    assert rows_of(db)[0].status == STATUS_ACTIVE and rows_of(db)[0].provision_map()["company"].withdrawn_at is None


def test_admin_actions_need_the_csrf_token(admin, db):
    publish_ready(db, public=False)
    admin.post(f"/admin/agendas/{CODE}/public", data={"csrf": "wrong"})
    assert not agenda_of(db).is_public


# ------------------------------------------------------------------------------------------- admin editor
def test_editor_round_trip_changes_nothing(admin, db):
    consent.save_draft(db, agenda_of(db), ready_content(), None)
    before = consent.content_hash(consent.draft_of(db, agenda_of(db)).content)
    page = admin.get(f"/admin/consent/{CODE}/edit").text
    fields = editor_fields(page)
    for name in ("apartment_name", "recipient_line", "cc_line", "s0_body", "q2_title", "q3_agree", "privacy_controller",
                 "privacy_collect_access_info", "r1_retention", "r0_refusal", "hint_signature", "final_checkbox"):
        assert name in fields
    response = admin.post(f"/admin/consent/{CODE}/draft", data=fields, follow_redirects=False)
    assert response.status_code == 303
    assert consent.content_hash(consent.draft_of(db, agenda_of(db)).content) == before


def test_editor_edits_every_part_and_publishes_a_new_immutable_version(admin, db):
    v1 = publish_ready(db)
    v1_content, v1_hash = copy.deepcopy(v1.content), v1.content_hash
    fields = editor_fields(admin.get(f"/admin/consent/{CODE}/edit").text)
    # Form slots are positions: slot 0/1/2 hold question keys q1/q2/q3, slot 3 is the blank "new question".
    fields.update({
        "apartment_name": "가상 아파트", "title": "가상 제목", "recipient_line": "가상 수신", "cc_line": "가상 참조",
        "s0_title": "1. 가상 절 제목", "s0_body": "가상 **굵은** 문단\n다음 줄\n\n둘째 문단",
        "q2_order": "0",  # question q3 first
        "q3_key": "", "q3_title": "새 가상 질문", "q3_text": "새 질문에 동의하십니까?", "q3_active": "1", "q3_order": "9",
        "hint_name": "주민등록상 성명", "privacy_contact": "010-0000-0000", "r1_retention": "가상 보존기준",
        "final_checkbox": "가상 최종 확인", "footer_note": "가상 하단 안내",
    })
    fields.pop("q1_active")  # hide question q2 (its key and its old answers stay)
    response = admin.post(f"/admin/consent/{CODE}/draft", data=fields, follow_redirects=False)
    assert response.status_code == 303
    draft = consent.draft_of(db, agenda_of(db)).content
    assert [(q["key"], q["active"]) for q in draft["questions"]] == [("q3", True), ("q1", True), ("q2", False), ("q4", True)]
    assert draft["questions"][3]["title"] == "새 가상 질문"
    assert draft["sections"][0] == {"level": 1, "title": "1. 가상 절 제목", "body": "가상 **굵은** 문단\n다음 줄\n\n둘째 문단"}
    assert (draft["apartment_name"], draft["title"], draft["recipient_line"], draft["cc_line"]) == (
        "가상 아파트", "가상 제목", "가상 수신", "가상 참조")
    assert draft["field_hints"]["name"] == "주민등록상 성명" and draft["recipients"][1]["retention"] == "가상 보존기준"

    preview = admin.get(f"/admin/consent/{CODE}/preview").text
    assert "가상 <strong>굵은</strong> 문단<br>다음 줄" in preview and "미리보기" in preview
    assert preview.index("질문 1. 관련 자료 보존") < preview.index("질문 2. 관리사무소장 교체") < preview.index("질문 3. 새 가상 질문")
    assert "계약체결 과정에 대한 특별조사를" not in preview.split("3. 요청사항에 대한 온라인 동의")[1].split("4. 참여자 확인")[0]

    page = admin.get(f"/admin/consent/{CODE}/edit").text
    draft_hash = re.search(r'name="draft_hash" value="([^"]+)"', page).group(1)
    admin.post(f"/admin/consent/{CODE}/publish", data={"csrf": csrf_from(page), "draft_hash": draft_hash})
    db.expire_all()
    versions = db.scalars(select(ConsentVersion).order_by(ConsentVersion.version_no)).all()
    assert [v.label for v in versions] == [V1, V2]
    assert versions[0].content == v1_content and versions[0].content_hash == v1_hash  # V1 untouched
    assert versions[1].content_hash == consent.content_hash(versions[1].content)
    version_page = admin.get(f"/admin/consent/{CODE}/versions/{V1}").text
    assert "확인값 일치" in version_page and "계약체결 과정에 대한 특별조사를" in version_page

    # A V1 record keeps its q2 answer; the old question stays countable under its own key.
    assert post_consent(admin, unit="1701", version_label=V2,
                        answer_q1="AGREE", answer_q3="DISAGREE", answer_q4="AGREE").status_code == 201
    answers = rows_of(db)[0].answer_map()
    assert answers == {"q1": "AGREE", "q3": "DISAGREE", "q4": "AGREE"}


def test_editor_escapes_markup_and_scripts(admin, db):
    fields = editor_fields(admin.get(f"/admin/consent/{CODE}/edit").text)
    fields["intro"] = '<script>alert(1)</script> <img src=x onerror=alert(2)> **굵게**'
    admin.post(f"/admin/consent/{CODE}/draft", data=fields)
    preview = admin.get(f"/admin/consent/{CODE}/preview").text
    assert "<script>alert(1)" not in preview and "<img src=x" not in preview
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in preview and "<strong>굵게</strong>" in preview


def test_a_stale_editor_page_does_not_silently_overwrite_the_draft(admin, db):
    page = admin.get(f"/admin/consent/{CODE}/edit").text
    first = editor_fields(page)
    second = dict(first)
    first["title"] = "먼저 저장한 제목"
    second["title"] = "나중에 저장하려던 제목"
    assert admin.post(f"/admin/consent/{CODE}/draft", data=first, follow_redirects=False).status_code == 303
    conflict = admin.post(f"/admin/consent/{CODE}/draft", data=second, follow_redirects=False)
    assert conflict.status_code == 409 and "먼저 저장" in conflict.text
    assert consent.draft_of(db, agenda_of(db)).content["title"] == "먼저 저장한 제목"


def test_publishing_needs_the_draft_that_was_checked(admin, db):
    consent.save_draft(db, agenda_of(db), ready_content(), None)
    page = admin.get(f"/admin/consent/{CODE}/edit").text
    stale_hash = re.search(r'name="draft_hash" value="([^"]+)"', page).group(1)
    changed = ready_content()
    changed["intro"] = "미리보기 뒤에 바뀐 초안"
    consent.save_draft(db, agenda_of(db), changed, None)
    admin.post(f"/admin/consent/{CODE}/publish", data={"csrf": csrf_from(page), "draft_hash": stale_hash})
    assert db.scalar(select(func.count()).select_from(ConsentVersion)) == 0


def test_loading_a_version_into_the_draft_and_creating_a_new_agenda(admin, db):
    publish_ready(db)
    changed = ready_content()
    changed["title"] = "초안에서만 바꾼 제목"
    consent.save_draft(db, agenda_of(db), changed, None)
    csrf = admin_csrf(admin)
    admin.post(f"/admin/consent/{CODE}/draft/load", data={"csrf": csrf, "label": V1})
    assert consent.draft_of(db, agenda_of(db)).content["title"] == ready_content()["title"]

    response = admin.post("/admin/agendas/new", data={"csrf": csrf, "name": "가상 새 안건", "source": CODE},
                          follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("/edit")
    db.expire_all()
    created = db.scalar(select(Agenda).where(Agenda.name == "가상 새 안건"))
    assert created.kind == "consent" and not created.is_public
    assert consent.current_version(db, created) is None
    assert consent.draft_of(db, created).content == consent.draft_of(db, agenda_of(db)).content
    assert db.scalar(select(func.count()).select_from(ConsentSubmission).where(
        ConsentSubmission.agenda_id == created.id)) == 0


def test_a_question_key_is_never_handed_out_twice(admin, db):
    publish_ready(db)
    with_q4 = ready_content()
    with_q4["questions"].append({"key": "q4", "title": "넷째 질문", "text": "넷째 질문 내용", "active": True,
                                 "agree_label": "동의합니다", "disagree_label": "동의하지 않습니다"})
    agenda = agenda_of(db)
    consent.save_draft(db, agenda, with_q4, None)
    assert consent.publish(db, agenda, None).label == V2
    # Back to V1's wording (no q4), then a new question: it must not become "q4" with another meaning.
    admin.post(f"/admin/consent/{CODE}/draft/load", data={"csrf": admin_csrf(admin), "label": V1})
    fields = editor_fields(admin.get(f"/admin/consent/{CODE}/edit").text)
    fields.update({"q3_key": "", "q3_title": "다른 새 질문", "q3_text": "다른 새 질문 내용", "q3_active": "1"})
    admin.post(f"/admin/consent/{CODE}/draft", data=fields)
    keys = [q["key"] for q in consent.draft_of(db, agenda_of(db)).content["questions"]]
    assert keys == ["q1", "q2", "q3", "q5"]


def test_a_reworded_question_is_either_split_or_explicitly_merged(admin, db):
    publish_ready(db)
    for unit in ("2301", "2302", "2303"):
        post_consent(admin, unit=unit, answers=("AGREE", "DISAGREE", "AGREE"))
    agenda = agenda_of(db)
    reworded = ready_content()
    reworded["questions"][0]["title"] = "관리사무소장 업무 개선 권고 요청"
    reworded["questions"][0]["text"] = "업무 개선 권고에 동의하십니까?"
    consent.save_draft(db, agenda, reworded, None)
    page = admin.get(f"/admin/consent/{CODE}/edit").text
    assert "게시 버전과 문구가 달라진 질문 1개" in page and 'name="merge_reworded"' in page
    draft_hash = re.search(r'name="draft_hash" value="([^"]+)"', page).group(1)
    admin.post(f"/admin/consent/{CODE}/publish", data={"csrf": csrf_from(page), "draft_hash": draft_hash})
    assert db.scalar(select(func.count()).select_from(ConsentVersion)) == 1  # not without a choice

    # Split: the edited wording gets a new key, q1 keeps V1's wording and answers.
    fields = editor_fields(page)
    fields["q0_split"] = "1"
    admin.post(f"/admin/consent/{CODE}/draft", data=fields)
    draft = consent.draft_of(db, agenda_of(db)).content
    assert [(q["key"], q["active"], q["title"]) for q in draft["questions"]] == [
        ("q4", True, "관리사무소장 업무 개선 권고 요청"), ("q2", True, ready_content()["questions"][1]["title"]),
        ("q3", True, ready_content()["questions"][2]["title"]), ("q1", False, ready_content()["questions"][0]["title"])]
    page = admin.get(f"/admin/consent/{CODE}/edit").text
    assert "게시 버전과 문구가 달라진 질문" not in page
    draft_hash = re.search(r'name="draft_hash" value="([^"]+)"', page).group(1)
    admin.post(f"/admin/consent/{CODE}/publish", data={"csrf": csrf_from(page), "draft_hash": draft_hash})
    assert consent.current_version(db, agenda_of(db)).label == V2
    assert post_consent(admin, unit="2304", version_label=V2, answer_q4="AGREE").status_code == 201
    stats = {q.key: (q.title, q.agree, q.disagree) for q in consent.consent_stats(db, agenda_of(db)).questions}
    assert stats["q4"] == ("질문 1. 관리사무소장 업무 개선 권고 요청", 1, 0)
    assert stats["q1"] == ("관리사무소장 교체 요청 (이전 버전 질문)", 3, 0)  # never merged into the new wording
    by_version = {q.key: q.agree for q in consent.consent_stats(
        db, agenda_of(db), consent.version_by_label(db, agenda_of(db), V1)).questions}
    assert by_version == {"q1": 3, "q2": 0, "q3": 3}
    filtered = admin.get(f"/admin/consent/{CODE}?version={V1}").text
    assert f"{V1}만" in filtered and "질문 1. 관리사무소장 교체 요청" in filtered

    # Merge: a typo fix may keep the key, but only with the explicit confirmation.
    typo = consent.normalize_content(consent.current_version(db, agenda_of(db)).content)
    typo["questions"][1]["text"] = typo["questions"][1]["text"].replace("요청하는", "요청 하는")
    consent.save_draft(db, agenda_of(db), typo, None)
    with pytest.raises(consent.PublishError):
        consent.publish(db, agenda_of(db), None)
    db.rollback()
    assert consent.publish(db, agenda_of(db), None, merge_reworded=True).label == f"{CODE}_V3"


def test_the_question_limit_counts_each_question_once(admin, db):
    content = ready_content()
    for number in (4, 5):
        content["questions"].append({"key": f"q{number}", "title": f"가상 질문 {number}", "text": "내용", "active": True,
                                     "agree_label": "동의합니다", "disagree_label": "동의하지 않습니다"})
    consent.save_draft(db, agenda_of(db), content, None)
    fields = editor_fields(admin.get(f"/admin/consent/{CODE}/edit").text)
    fields.update({"q5_key": "", "q5_title": "여섯째 질문", "q5_text": "여섯째 질문 내용", "q5_active": "1"})
    admin.post(f"/admin/consent/{CODE}/draft", data=fields)
    keys = [q["key"] for q in consent.draft_of(db, agenda_of(db)).content["questions"]]
    assert keys == ["q1", "q2", "q3", "q4", "q5", "q6"]


def test_switching_back_to_the_legacy_agenda_keeps_everything(admin, submit, db):
    publish_ready(db)
    post_consent(admin, unit="1801")
    csrf = admin_csrf(admin)
    admin.post(f"/admin/agendas/{consent.LEGACY_AGENDA_CODE}/public", data={"csrf": csrf})
    assert 'id="opinion-form"' in admin.get("/opinion").text
    assert submit(unit="1801").status_code == 201  # separate agenda, separate rule
    assert len(rows_of(db)) == 1 and rows_of(db)[0].status == STATUS_ACTIVE
    page = admin.get("/admin/agendas").text
    assert "가상주민" not in page and "공개 중" in page


# ------------------------------------------------------------------------------------ legacy compatibility
def test_legacy_admin_exports_and_pdf_keep_their_content(admin, submit, db):
    assert submit(unit="1203").status_code == 201
    assert submit(unit="1204", opinion_choice="DISAGREE", additional_comment="가상 기타 의견").status_code == 201
    legacy_row = db.scalar(select(OpinionSubmission).where(OpinionSubmission.unit == "1204"))
    csv_before = admin.get("/admin/export.csv").content
    pdf_before = pdf_text(admin.get(f"/admin/submissions/{legacy_row.public_id}/pdf").content)

    publish_ready(db)
    post_consent(admin, unit="1203")
    post_consent(admin, unit="1204", answers=("DISAGREE",) * 3)

    assert admin.get("/admin/export.csv").content == csv_before
    pdf_after = pdf_text(admin.get(f"/admin/submissions/{legacy_row.public_id}/pdf").content)
    strip = lambda text: re.sub(r"출력 \d{4}-\d{2}-\d{2} \d{2}:\d{2}", "", text)  # noqa: E731
    assert strip(pdf_after) == strip(pdf_before)
    assert SEED_TITLE in pdf_after and "관리사무소장 교체" not in pdf_after
    dashboard = admin.get("/admin").text
    assert "기존 안건 단일 문항" in dashboard and dashboard.count("tag tag-") >= 2


def test_answers_and_provisions_are_stored_per_question_key(client, db):
    publish_ready(db)
    post_consent(client, answers=("AGREE", "DISAGREE", "AGREE"), company="DISAGREE")
    row = rows_of(db)[0]
    stored = db.execute(select(ConsentAnswer.question_key, ConsentAnswer.answer)
                        .where(ConsentAnswer.submission_id == row.id).order_by(ConsentAnswer.question_key)).all()
    assert stored == [("q1", "AGREE"), ("q2", "DISAGREE"), ("q3", "AGREE")]
    provisions = db.execute(select(ConsentProvision.recipient_key, ConsentProvision.agreed)
                            .where(ConsentProvision.submission_id == row.id)
                            .order_by(ConsentProvision.recipient_key)).all()
    assert provisions == [("company", False), ("district", True)]
    assert (row.agenda_id, row.version_label, row.content_hash) == (agenda_of(db).id, V1, row.version.content_hash)
    assert row.access_info_collected and row.ip_address and row.user_agent
    assert row.overseas_consent is True


def test_overseas_transfer_consent_is_asked_stored_exported_and_printed(admin, db):
    publish_ready(db)
    refused = post_consent(admin, overseas_consent="DISAGREE")
    assert refused.status_code == 422 and "국외 이전" in refused.json()["errors"]["overseas_consent"]
    assert post_consent(admin, unit="2401").status_code == 201
    row = rows_of(db)[0]
    assert row.overseas_consent is True
    table = read_csv(admin.get(f"/admin/consent/{CODE}/export.csv"))
    assert table[1][table[0].index("개인정보 국외 이전")] == "동의"
    assert "개인정보 국외 이전</dt><dd>동의" in admin.get(f"/admin/consent-submissions/{row.public_id}").text
    text = pdf_text(admin.get(f"/admin/consent-submissions/{row.public_id}/pdf").content)
    assert "5-1. 개인정보 국외 이전 동의" in text and "Render Services, Inc." in text
    after = text.split("국외 이전(보관)에 동의하십니까?")[1]
    assert after.lstrip().startswith("● 동의합니다")
    listing = pdf_text(admin.get(f"/admin/consent/{CODE}/recipients/company.pdf").content)
    assert "5-1. 개인정보 국외 이전 동의" in listing  # the wording appendix shows it too


def test_wording_without_the_overseas_section_does_not_ask_it(admin, db):
    content = ready_content()
    content["overseas"]["enabled"] = False
    content["privacy"]["storage_location"] = ""
    assert any("저장 위치" in problem for problem in consent.publish_blockers(content))  # then it must be stated
    content["privacy"]["storage_location"] = "테스트용 국내 서버"
    publish_ready(db, content=content)
    page = admin.get("/opinion").text
    assert 'data-field="overseas_consent"' not in page and "테스트용 국내 서버" in page
    form = consent_form(unit="2402")
    del form["overseas_consent"]
    assert post_consent(admin, form).status_code == 201
    row = rows_of(db)[0]
    assert row.overseas_consent is None
    table = read_csv(admin.get(f"/admin/consent/{CODE}/export.csv"))
    assert table[1][table[0].index("개인정보 국외 이전")] == "해당 없음"
    assert "이 버전은 묻지 않음" in admin.get(f"/admin/consent-submissions/{row.public_id}").text
    assert "국외 이전" not in pdf_text(admin.get(f"/admin/consent-submissions/{row.public_id}/pdf").content)


def test_a_draft_saved_before_the_overseas_section_gets_it_and_a_stale_page_cannot_erase_it(admin, db):
    from app.consent import seed_agendas
    from app.models import ConsentDraft

    agenda = agenda_of(db)
    old = {key: value for key, value in ready_content().items() if key != "overseas"}
    draft = db.get(ConsentDraft, agenda.id)
    draft.content = old
    db.commit()
    stale = editor_fields(admin.get(f"/admin/consent/{CODE}/edit").text)
    stale = {key: value for key, value in stale.items() if not key.startswith("overseas_")}  # an old editor page

    seed_agendas(db)  # what the next start does
    db.expire_all()
    upgraded = db.get(ConsentDraft, agenda.id).content
    assert upgraded["overseas"] == consent.SEED_CONTENT["overseas"]
    assert {key: value for key, value in upgraded.items() if key != "overseas"} == old  # nothing else touched

    conflict = admin.post(f"/admin/consent/{CODE}/draft", data=stale, follow_redirects=False)
    assert conflict.status_code == 409
    db.expire_all()
    assert db.get(ConsentDraft, agenda.id).content["overseas"]["enabled"] is True


def test_a_column_added_later_is_created_on_an_existing_table(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.database import Base

    engine_ = create_engine("sqlite:///" + (tmp_path / "old_test").as_posix())
    Base.metadata.create_all(engine_)
    with engine_.begin() as conn:
        conn.execute(text("ALTER TABLE consent_submissions DROP COLUMN overseas_consent"))  # the table as first deployed
    assert "overseas_consent" not in {c["name"] for c in inspect(engine_).get_columns("consent_submissions")}
    init_db(engine_)
    init_db(engine_)  # a second start changes nothing
    assert "overseas_consent" in {c["name"] for c in inspect(engine_).get_columns("consent_submissions")}
    engine_.dispose()
