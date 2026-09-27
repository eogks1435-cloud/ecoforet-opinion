"""Admin: login, counts, list/search, signature view, invalidation and CSV export."""

from __future__ import annotations

import csv
import dataclasses
import io
import re

from sqlalchemy import select

from app import security
from app.models import OpinionSubmission
from app.routes import admin as admin_routes

from .conftest import ADMIN_PASSWORD, ORIGIN, csrf_from


def _invalidate(client, row: OpinionSubmission, reason: str = "", csrf: str | None = None):
    page = client.get("/admin")
    return client.post(
        f"/admin/submissions/{row.public_id}/invalidate",
        data={"csrf": csrf if csrf is not None else csrf_from(page.text), "reason": reason, "back": "/admin"},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )


def _stat_values(html: str) -> list[str]:
    return re.findall(r'<p class="stat-value">([^<]+)</p>', html)


def test_admin_pages_require_login(client, submit, db):
    submit()
    row = db.scalars(select(OpinionSubmission)).one()
    for path in ("/admin", "/admin/export.csv"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert client.get(f"/admin/signatures/{row.public_id}.png").status_code == 401
    response = client.post(f"/admin/submissions/{row.public_id}/invalidate", data={}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"


def test_login_rejects_wrong_password_and_missing_csrf(client, monkeypatch):
    monkeypatch.setattr(admin_routes, "FAILED_LOGIN_DELAY", 0)
    token = csrf_from(client.get("/admin/login").text)
    wrong = client.post("/admin/login", data={"password": "nope-nope", "csrf": token}, headers={"Origin": ORIGIN})
    assert wrong.status_code == 401 and "비밀번호가 올바르지 않습니다." in wrong.text
    no_csrf = client.post("/admin/login", data={"password": ADMIN_PASSWORD}, headers={"Origin": ORIGIN})
    assert no_csrf.status_code == 400
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_login_is_throttled_after_repeated_failures(client, monkeypatch):
    monkeypatch.setattr(admin_routes, "FAILED_LOGIN_DELAY", 0)
    token = csrf_from(client.get("/admin/login").text)
    for _ in range(10):
        client.post("/admin/login", data={"password": "wrong-pass", "csrf": token}, headers={"Origin": ORIGIN})
    blocked = client.post("/admin/login", data={"password": ADMIN_PASSWORD, "csrf": token}, headers={"Origin": ORIGIN})
    assert blocked.status_code == 429


def test_dashboard_counts_and_lists_newest_first(admin, submit):
    submit(building="101", unit="1203", resident_name="홍길동", opinion_choice="AGREE")
    submit(building="102", unit="1203", resident_name="김영희", opinion_choice="DISAGREE")
    submit(building="103", unit="501", resident_name="이민수", opinion_choice="AGREE", additional_comment="의견 있음")
    html = admin.get("/admin").text
    assert _stat_values(html) == ["총 3건", "2건", "1건"]
    assert html.index("이민수") < html.index("김영희") < html.index("홍길동")
    assert "동의하지 않음" in html and "의견 있음" in html
    assert html.count("/admin/signatures/") == 3
    for word in ("투표", "찬성", "반대"):
        assert word not in html


def test_search_and_filters(admin, submit):
    submit(building="101", unit="1203", resident_name="홍길동", opinion_choice="AGREE")
    submit(building="102", unit="1203", resident_name="김영희", opinion_choice="DISAGREE")
    submit(building="103", unit="501", resident_name="홍민수", opinion_choice="AGREE")

    def names(query: str) -> set[str]:
        html = admin.get("/admin" + query).text
        return {name for name in ("홍길동", "김영희", "홍민수") if f'data-label="성명">{name}</td>' in html}

    assert names("?building=101동") == {"홍길동"}
    assert names("?unit=1203") == {"홍길동", "김영희"}
    assert names("?name=홍") == {"홍길동", "홍민수"}
    assert names("?opinion=DISAGREE") == {"김영희"}
    assert names("?name=%25") == set()  # LIKE wildcards are matched literally
    assert names("?status=INVALIDATED") == set()


def test_names_are_escaped(admin, submit):
    submit(resident_name="<script>alert(1)</script>")
    html = admin.get("/admin").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_signature_image_is_served_to_admins(admin, submit, db):
    submit()
    row = db.scalars(select(OpinionSubmission)).one()
    response = admin.get(f"/admin/signatures/{row.public_id}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == row.signature_data and response.content.startswith(b"\x89PNG")


def test_invalidation_excludes_the_row_and_frees_the_unit(admin, submit, db):
    submit(building="101", unit="1203", resident_name="홍길동")
    submit(building="102", unit="1203", resident_name="김영희", opinion_choice="DISAGREE")
    row = db.scalars(select(OpinionSubmission).where(OpinionSubmission.building == "101")).one()

    rejected = _invalidate(admin, row, csrf="forged")
    assert rejected.status_code == 303
    db.refresh(row)
    assert row.status == "ACTIVE"

    done = _invalidate(admin, row, reason="본인 요청으로 재제출")
    assert done.status_code == 303 and done.headers["location"] == "/admin"
    db.refresh(row)
    assert row.status == "INVALIDATED" and row.invalidated_at is not None
    assert row.invalidated_reason == "본인 요청으로 재제출"

    html = admin.get("/admin").text
    assert "무효 처리했습니다" in html
    assert _stat_values(html) == ["총 1건", "0건", "1건"]
    assert "무효 처리된 1건" in html

    again = submit(building="101", unit="1203", resident_name="홍길동", opinion_choice="DISAGREE")
    assert again.status_code == 201
    html = admin.get("/admin").text
    assert _stat_values(html) == ["총 2건", "0건", "2건"]
    assert html.count('data-label="성명">홍길동</td>') == 2  # the invalidated record stays listed

    repeat = _invalidate(admin, row)
    assert repeat.status_code == 303
    assert "이미 무효 처리된 제출입니다." in admin.get("/admin").text


def test_csv_export(admin, submit):
    submit(building="101", unit="1203", resident_name="홍길동", additional_comment="=HYPERLINK(\"http://x\")")
    submit(building="102", unit="7", resident_name="김영희", opinion_choice="DISAGREE")
    response = admin.get("/admin/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM for Excel

    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["번호", "동", "호수", "성명", "의견", "기타 의견", "제출일시", "의견서 버전", "상태", "제출번호"]
    assert [r[0] for r in rows[1:]] == ["1", "2"]  # oldest first
    first, second = rows[1], rows[2]
    assert first[1:5] == ["101", "1203", "홍길동", "의견서 내용에 동의"]
    assert first[5] == "'=HYPERLINK(\"http://x\")"  # formula neutralised
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", first[6])
    assert first[7:9] == ["EPOXY_OPINION_V1", "유효"]
    assert re.fullmatch(r"[0-9A-F]{8}", first[9])
    assert second[4] == "의견서 내용에 동의하지 않음"

    filtered = admin.get("/admin/export.csv?opinion=DISAGREE").content.decode("utf-8-sig")
    assert "김영희" in filtered and "홍길동" not in filtered


def test_logout_and_password_rotation_end_sessions(admin, monkeypatch):
    assert admin.get("/admin", follow_redirects=False).status_code == 200
    rotated = dataclasses.replace(security.settings, admin_password="a-brand-new-password")
    monkeypatch.setattr(security, "settings", rotated)
    assert admin.get("/admin", follow_redirects=False).status_code == 303
    monkeypatch.undo()

    token = csrf_from(admin.get("/admin").text)
    response = admin.post("/admin/logout", data={"csrf": token}, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    assert admin.get("/admin", follow_redirects=False).status_code == 303
