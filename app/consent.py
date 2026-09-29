"""Multi-question consent agendas: wording model, publish checks, submissions, counts and export filters.

The wording of a consent agenda is one JSON object (see SEED_CONTENT for every field). Rules:
- Text fields support only: a blank line starts a paragraph, a single line break is kept, **text** is bold.
  Nothing else is interpreted (HTML is escaped when shown).
- Questions (q1, q2, ...) and recipients (company, district) have fixed keys. A key is never reused, so an
  answer stays tied to the wording it was given under; a question is deactivated, not deleted.
- The admin edits a draft; publishing copies it into a new immutable version (consent_versions). The
  version's SHA-256 (hash scheme "consent-json-v1") covers the whole normalised JSON: every text shown to the
  resident, the questions and choice labels, the privacy and provision notices, the final confirmation and
  the access-information setting.
- Fields left empty in the seed are "[게시 전 입력]" values the operator must decide; publishing is blocked
  until they are filled (publish_blockers).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AGENDA_CONSENT,
    AGENDA_LEGACY,
    OPINION_AGREE,
    OPINION_DISAGREE,
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    STATUS_WITHDRAWN,
    Agenda,
    ConsentAnswer,
    ConsentDraft,
    ConsentProvision,
    ConsentSubmission,
    ConsentVersion,
    OpinionDocument,
)
from .schemas import NAME_MAX, NAME_MIN_LETTERS, FormErrors, SignatureError, clean_text, parse_number, process_signature

logger = logging.getLogger("opinion")

HASH_SCHEME = "consent-json-v1"
NEW_AGENDA_CODE = "MANAGER_REQUEST_2026"
NEW_AGENDA_NAME = "관리사무소장 교체 및 관리업무 특별조사 요청 온라인 동의서"
LEGACY_AGENDA_CODE = "EPOXY_OPINION"
ANSWERS = (OPINION_AGREE, OPINION_DISAGREE)
ANSWER_LABEL = {OPINION_AGREE: "동의", OPINION_DISAGREE: "동의하지 않음"}
STATUS_LABEL = {STATUS_ACTIVE: "유효", STATUS_INVALIDATED: "무효", STATUS_WITHDRAWN: "철회"}

LINE_MAX = 300
TEXT_MAX = 6000
SECTIONS_MAX = 30
QUESTIONS_MAX = 10
PLACEHOLDER_MARK = "[게시 전 입력"
ACCESS_INFO_TEXT = "접속 IP 주소, 브라우저 정보"
ACCESS_INFO_WORDS = ("IP 주소", "브라우저 정보")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,19}$")

_YES_NO = {"agree_label": "동의합니다", "disagree_label": "동의하지 않습니다"}
_RECIPIENT_REFUSAL = (
    "개인정보 제공에 동의하지 않을 권리가 있습니다.\n\n"
    "동의하지 않는 경우 {name}에 본인의 개인정보가 포함된 동의자료를 제출하지 않으며, 해당 {kind}에 제출하는 "
    "기명 동의자 명단에서도 제외합니다. 다만, 개인정보 수집·이용에 동의한 경우 내부 항목별 집계에는 답변이 "
    "반영될 수 있습니다."
)
_PROVIDED_ITEMS = "동·호수, 성명, 질문별 동의 여부, 서명 이미지, 제출번호, 제출일시 및 제출 당시 문서 버전"

# The wording from the 2026-09-29 brief (section E), split into the parts of the form. Empty strings are the
# brief's "[게시 전 입력]" values: the operator fills them in the admin before the agenda can be published.
SEED_CONTENT: dict = {
    "apartment_name": "e편한세상 강동에코포레",
    "title": "관리사무소장 교체 및 관리업무 특별조사 요청",
    "subtitle": "온라인 동의서",
    "recipient_line": "주식회사 케이비아주 대표이사 귀하",
    "cc_line": "서울특별시 강동구청 공동주택관리 담당부서",
    "intro": "",
    "sections": [
        {
            "level": 1,
            "title": "1. 온라인 동의 추진 취지",
            "body": (
                "현재 우리 아파트는 입주자대표회의 구성원들의 사퇴에 따라 입주자대표회의의 정상적인 운영과 "
                "의사결정에 공백이 발생한 상황입니다.\n\n"
                "최근 추진된 지하주차장 에폭시·도장공사의 의결, 입찰, 업체선정, 공사범위 결정 및 계약체결 과정과 "
                "관련하여, 관리업무가 적정한 권한과 절차에 따라 처리되었는지 객관적인 사실확인이 필요합니다.\n\n"
                "특히 공사 추진 과정에서 입주자대표들에게 공사금액, 구체적인 공사범위 및 관련 자료가 충분히 "
                "제공·설명되었는지, 의결된 내용과 실제 계약내용이 일치하는지, 관리사무소장이 공사 추진 및 계약 "
                "과정에서 적정한 권한과 절차에 따라 업무를 수행하였는지 등을 확인하고자 합니다.\n\n"
                "이에 아래 요청사항에 동의하는 주민들의 의사를 모아, 위탁관리업체인 주식회사 케이비아주에 현 "
                "관리사무소장의 교체 및 관리업무 특별조사를 공식 요청하고자 합니다.\n\n"
                "또한 필요한 경우, 각 참여자가 동의한 요청사항과 관련하여 강동구청에 사실확인 및 행정감독을 "
                "요청하고, 본 동의 결과를 그 근거자료로 제출하고자 합니다.\n\n"
                "본 동의서는 아래 각 요청사항에 대한 주민 개개인의 동의 여부를 확인하기 위한 문서입니다. 각 항목의 "
                "동의 여부는 구분하여 집계하며, 동의하지 않은 항목의 요청 동의자로 포함하지 않습니다."
            ),
        },
        {"level": 1, "title": "2. 주민 요구사항", "body": ""},
        {
            "level": 2,
            "title": "① 현 관리사무소장 교체 요청",
            "body": (
                "현재까지의 관리업무 수행과 주민과 관리사무소 사이의 신뢰관계 등을 고려하여, 관리업무의 투명성 "
                "확보와 주민 신뢰 회복을 위해 주식회사 케이비아주에 현 관리사무소장의 교체를 공식 요청합니다.\n\n"
                "교체 및 후임자 배치 등 구체적인 인사 조치는 관계 법령과 위·수탁관리계약 등에 따른 적법한 절차로 "
                "진행하여 주시기 바랍니다.\n\n"
                "교체 과정에서도 일상적인 관리업무와 주민 안전에 공백이 발생하지 않도록 필요한 조치를 취하여 "
                "주시기 바랍니다."
            ),
        },
        {
            "level": 2,
            "title": "② 지하주차장 에폭시·도장공사 특별조사 요청",
            "body": (
                "다음 사항에 대한 객관적인 사실조사를 요청합니다.\n\n"
                "가. 공사의 필요성 및 공사범위를 제안·결정한 과정과 그 근거\n"
                "나. 입주자대표회의 의결 당시 제출된 자료 및 회의록\n"
                "다. 공사금액과 구체적인 공사범위에 관한 자료를 동별 대표자들에게 언제, 어떤 방식으로 "
                "제공·설명하였는지 여부\n"
                "라. 입찰공고 및 현장설명회 진행 과정\n"
                "마. 입찰 참여업체의 자격 검토 및 평가 과정\n"
                "바. 견적서 및 시방서의 작성·검토 과정\n"
                "사. 낙찰업체 선정 과정 및 선정 근거\n"
                "아. 계약서 작성·검토 및 계약체결 과정\n"
                "자. 입주자대표회의 의결내용과 실제 계약내용의 일치 여부\n"
                "차. 위 과정에서 관리사무소장이 수행한 역할 및 권한의 적정성\n\n"
                "조사는 해당 업무를 직접 처리한 담당자의 설명만으로 종결하지 말고, 위탁관리업체 본사 차원에서 "
                "관련 원자료와 관계자의 설명을 대조하여 실시하여 주시기 바랍니다."
            ),
        },
        {
            "level": 2,
            "title": "③ 관련 자료 보존 및 공개·확인 요청",
            "body": (
                "공사 및 관리업무와 관련된 회의록, 의결자료, 견적서, 입찰서류, 시방서, 현장설명회 자료, "
                "업체선정 자료, 계약서 및 관련 전자문서 등을 보존하고, 조사 과정에서 그 내용을 확인하여 주시기 "
                "바랍니다.\n\n"
                "관련 자료가 임의로 폐기·삭제·변경되거나 누락되지 않도록 필요한 보존 조치를 취하여 주시기 "
                "바랍니다.\n\n"
                "관계 법령상 공개·열람이 가능한 범위에서 주민의 열람 및 사본 제공 요청에 응하여 주시고, "
                "개인정보 등 공개가 제한되는 부분은 필요한 보호조치를 하여 주시기 바랍니다. 공개할 수 없는 부분이 "
                "있는 경우에는 그 사유와 근거를 설명하여 주시기 바랍니다."
            ),
        },
        {
            "level": 2,
            "title": "④ 관리사무소장 업무수행 적정성 조사 요청",
            "body": (
                "관리사무소장이 공사의 기획·입찰·업체선정·계약 과정과 관련 관리업무를 수행하면서 공동주택관리 "
                "관계 법령, 관리규약 및 위·수탁관리계약 등에 따른 권한과 절차를 준수하였는지 조사하여 주시기 "
                "바랍니다.\n\n"
                "또한 입주자대표회의가 합리적으로 판단할 수 있도록 필요한 자료와 정보를 적시에 제공하고, 주요 "
                "계약조건과 비용부담 등을 충분히 설명하였는지도 확인하여 주시기 바랍니다."
            ),
        },
        {
            "level": 2,
            "title": "⑤ 입주자대표회의 운영 공백기간 중 중요 업무의 적정 처리 요청",
            "body": (
                "새로운 입주자대표회의가 적법하게 구성되어 정상적으로 운영될 때까지, 입주민에게 중대한 재산상 "
                "영향을 미칠 수 있는 새로운 중요 계약, 고액 비용지출, 기존 계약의 변경, 추가공사, 공사금액 또는 "
                "공사범위 변경 등에 대해서는 관계 법령과 관리규약에 따라 적법하고 신중하게 처리하여 주시기 "
                "바랍니다.\n\n"
                "필요한 의결이나 동의 절차가 있는 사항은 해당 절차를 준수하고, 처리 근거와 주요 내용을 주민들에게 "
                "투명하게 공개하여 주시기 바랍니다.\n\n"
                "이 요청은 통상적인 일상관리나 긴급한 안전조치의 중단을 요구하는 취지가 아닙니다."
            ),
        },
        {
            "level": 2,
            "title": "⑥ 조사결과에 따른 조치 및 결과 통보 요청",
            "body": (
                "조사 결과 위법·부당한 업무처리, 절차상 하자 또는 관리업무상 고의·과실 등이 확인될 경우, 관계 "
                "법령 및 위·수탁관리계약 등에 따른 적절한 조치를 취하여 주시기 바랍니다.\n\n"
                "조사 착수 여부, 담당부서 및 조사 일정을 안내하고, 조사결과와 그 근거, 후속조치 내용을 주민들에게 "
                "서면으로 알려주시기 바랍니다.\n\n"
                "본 요청은 특정인의 위법행위나 책임을 미리 확정하는 것이 아니라, 객관적인 사실확인과 그 결과에 "
                "따른 적법한 조치를 요구하는 것입니다."
            ),
        },
    ],
    "questions_title": "3. 요청사항에 대한 온라인 동의",
    "questions_intro": (
        "위 내용을 확인하였으며, 다음 각 질문에 대한 본인의 의사를 표시합니다.\n\n"
        "각 질문은 별개의 동의 항목입니다. 모든 질문에 동일하게 답변할 필요는 없으며, 각 질문에서 하나의 답변을 "
        "선택하여 주시기 바랍니다.\n\n"
        "조사 요청에는 해당 조사와 관련된 자료의 보존·확인, 조사결과 통보 및 확인된 사항에 대한 적법한 "
        "후속조치 요청이 포함됩니다."
    ),
    "questions": [
        {
            "key": "q1",
            "title": "관리사무소장 교체 요청",
            "text": "현 관리사무소장의 교체를 주식회사 케이비아주에 공식 요청하는 것에 동의하십니까?",
            "active": True,
            **_YES_NO,
        },
        {
            "key": "q2",
            "title": "지하주차장 에폭시·도장공사 특별조사 요청",
            "text": (
                "지하주차장 에폭시·도장공사의 의결·입찰·업체선정·공사범위 결정 및 계약체결 과정에 대한 특별조사를 "
                "주식회사 케이비아주에 요청하는 것에 동의하십니까?"
            ),
            "active": True,
            **_YES_NO,
        },
        {
            "key": "q3",
            "title": "관련 자료 보존 및 관리업무 사실조사 요청",
            "text": (
                "관련 자료의 보존·공개·확인과 관리업무 전반에 대한 사실조사 및 중요 업무의 적정 처리를 주식회사 "
                "케이비아주에 요청하는 것에 동의하십니까?"
            ),
            "active": True,
            **_YES_NO,
        },
    ],
    "participant_title": "4. 참여자 확인",
    "participant_target": "",
    "field_hints": {
        "building": "",
        "unit": "",
        "name": "",
        "signature": "위 영역에 손가락 또는 마우스로 직접 서명해 주세요.",
    },
    "participation_notes_title": "※ 참여 및 집계 안내",
    "participation_notes": (
        "중복 집계를 방지하기 위하여 동일 동·호수당 1회의 유효한 참여를 원칙으로 합니다.\n\n"
        "본인이 직접 작성하여 주시고, 타인의 명의를 사용하여 제출하지 마시기 바랍니다.\n\n"
        "본 동의서는 참여자 본인이 표시한 의사를 기록하며, 다른 세대원 전원의 동의나 위임이 있는 것으로 "
        "간주하지 않습니다.\n\n"
        "동일 동·호수의 중복 참여, 타인 명의 제출 또는 참여정보 오류가 확인되거나 의심되는 경우에는 "
        "사실확인을 거쳐 집계 여부를 결정합니다.\n\n"
        "정정이나 철회가 필요한 경우 아래 안내된 연락처로 요청할 수 있습니다.\n\n"
        "이전에 제출한 다른 의견서의 참여 내역은 이번 요청사항에 대한 동의로 자동 전환하거나 합산하지 않습니다."
    ),
    "privacy": {
        "title": "5. 개인정보 수집·이용 동의",
        "controller": "",
        "officer": "",
        "contact": "",
        "purpose": (
            "참여정보 확인, 중복·부정 제출 확인, 항목별 동의 결과 집계, 동의기록 보관, 요청서 작성 및 제출 관련 "
            "업무, 열람·정정·철회 요청 처리와 서비스 보안 관리를 위해 사용합니다.\n\n"
            "수집된 개인정보는 위 목적과 별도로 동의받은 제공 목적 이외의 용도로 사용하지 않습니다."
        ),
        "items_input": "동·호수, 성명, 질문별 동의 여부, 서명 이미지, 최종 확인 및 개인정보 처리에 대한 동의 여부",
        "items_generated": "제출번호, 제출일시, 제출 당시 문서 버전 및 본문 확인값",
        "collect_access_info": True,
        "retention_records": "",
        "retention_access": "",
        "retention_notes": (
            "보유기간이 종료되거나 처리 목적이 달성되어 개인정보가 불필요하게 된 경우에는 지체 없이 파기합니다.\n\n"
            "다른 법령에 따라 보존해야 하는 경우에는 해당 근거와 기간에 따라 필요한 정보만 별도로 보존합니다."
        ),
        "storage_location": "",
        # Operator confirmation, not shown to residents: what is destroyed when, how, and how backups are handled.
        "destruction_plan": "",
        "refusal": (
            "개인정보 수집·이용에 동의하지 않을 권리가 있습니다.\n\n"
            "다만, 위 정보의 수집·이용에 동의하지 않는 경우 본 온라인 동의서의 기명 접수 및 동의자 확인이 "
            "불가능하므로 제출이 제한됩니다."
        ),
        "question": "위 개인정보 수집·이용에 동의하십니까?",
        **_YES_NO,
    },
    # Separate consent to storing the data abroad: the service and its database run in Render's Singapore region
    # (Render Services, Inc., privacy@render.com per Render's privacy policy). Checked facts, still editable.
    "overseas": {
        "enabled": True,
        "title": "5-1. 개인정보 국외 이전 동의",
        "intro": (
            "온라인 동의서로 제출하신 개인정보는 해외 클라우드 서버에 저장됩니다. 국외 이전에 대한 동의는 "
            "개인정보 수집·이용 동의와 별도로 받습니다."
        ),
        "items": "위 5. ③의 수집 항목 전부",
        "country": (
            "싱가포르(Render 싱가포르 리전 서버). 서비스 제공자가 미국 법인이므로 서비스 운영 과정에서 미국 등에서 "
            "접근·처리될 수 있습니다."
        ),
        "timing": "온라인 동의서를 제출하는 즉시 암호화된 통신(HTTPS)으로 전송되어 저장됩니다.",
        "recipient": "Render Services, Inc.(미국 클라우드 서비스 사업자), 개인정보 문의 privacy@render.com",
        "purpose": "온라인 동의서 서비스의 서버 운영과 자료 보관",
        "retention": (
            "위 5. ④의 보유·이용기간 동안 보관한 뒤 삭제합니다. 삭제 후에도 서비스 복구용 자동 백업에는 최대 7일간 "
            "남을 수 있습니다."
        ),
        "refusal": (
            "개인정보 국외 이전에 동의하지 않을 권리가 있습니다. 다만 이 온라인 동의서는 해외 서버에 저장되므로, "
            "동의하지 않으시면 온라인으로 제출할 수 없습니다."
        ),
        "alternative": "",  # operator decision: how residents who decline can still take part
        "question": "위 개인정보의 국외 이전(보관)에 동의하십니까?",
        **_YES_NO,
    },
    "provision_title": "6. 개인정보 제3자 제공 동의",
    "provision_intro": (
        "본인이 동의한 요청사항의 제출과 확인을 위하여 아래와 같이 개인정보를 제공하고자 합니다.\n\n"
        "개인정보 수집·이용 동의와 별도로, 각 제출처에 대한 제공 동의 여부를 선택하여 주시기 바랍니다."
    ),
    "recipients": [
        {
            "key": "company",
            "heading": "가. 주식회사 케이비아주에 대한 제공",
            "name": "주식회사 케이비아주",
            "short_name": "케이비아주",
            "purpose": "관리사무소장 교체 및 관리업무 특별조사 요청의 접수, 동의자료 확인, 관련 조사 및 후속조치 처리",
            "items": _PROVIDED_ITEMS,
            "retention": "",
            "delivery": "",  # operator confirmation, not shown to residents: how the file is handed over
            "refusal": _RECIPIENT_REFUSAL.format(name="주식회사 케이비아주", kind="회사"),
            "question": "주식회사 케이비아주에 위 개인정보를 제공하는 것에 동의하십니까?",
            **_YES_NO,
        },
        {
            "key": "district",
            "heading": "나. 서울특별시 강동구에 대한 제공",
            "name": "서울특별시 강동구 — 공동주택관리 관련 민원 담당부서",
            "short_name": "강동구",
            "purpose": "본인이 동의한 요청사항에 관한 사실확인·행정감독 요청의 접수, 동의자료 확인 및 민원 처리",
            "items": _PROVIDED_ITEMS,
            "retention": "",
            "delivery": "",
            "refusal": _RECIPIENT_REFUSAL.format(name="서울특별시 강동구", kind="기관"),
            "question": "서울특별시 강동구에 위 개인정보를 제공하는 것에 동의하십니까?",
            **_YES_NO,
        },
    ],
    "principles_title": "※ 개인정보 제공 및 결과 공개 원칙",
    "principles": (
        "각 요청사항에 동의하고 해당 제출처에 대한 개인정보 제공에도 동의한 참여자의 자료에 한하여 기명 "
        "요청자료로 제출합니다.\n\n"
        "일부 질문에만 동의한 참여자를 전체 요청사항의 동의자로 표시하지 않습니다.\n\n"
        "내부 집계 결과와 제출처별 기명 동의자 수는 구분하여 관리합니다.\n\n"
        "접속 IP 주소와 브라우저 정보는 위 제출자료에 포함하지 않습니다.\n\n"
        "주민들에게 결과를 안내할 때에는 개인을 식별할 수 없는 항목별 집계 결과를 사용하며, 개별 참여자의 "
        "성명·동·호수·서명 및 답변을 공개 게시하지 않습니다."
    ),
    "final_title": "7. 최종 확인",
    "final_statements": (
        "본인은 위 내용을 확인하였으며, 각 질문에 표시한 답변이 본인의 의사임을 확인합니다.\n\n"
        "본인이 동의한 요청사항은 주식회사 케이비아주에 대한 관리사무소장 교체 및 관리업무 특별조사 요청과, 해당 "
        "사항에 관한 강동구청의 사실확인·행정감독 요청에 활용될 수 있음을 확인합니다.\n\n"
        "개인정보가 포함된 동의자료는 본인이 제3자 제공에 동의한 제출처에 한하여 제출됩니다.\n\n"
        "본인이 동의하지 않은 항목에 대해서는 해당 요청의 동의자로 집계되지 않습니다.\n\n"
        "본 동의서는 교체 및 조사 등을 요청하기 위한 문서이며, 제출 자체로 관리사무소장의 교체나 공사계약의 "
        "해지·중지가 확정되는 것은 아닙니다."
    ),
    "final_checkbox": "위 내용을 확인하였으며, 본인의 의사에 따라 제출합니다.",
    "footer_note": "제출일시는 시스템에 기록된 제출일시로 갈음합니다.",
}

# Operator decisions that must be filled before publishing: (path, label shown in the admin checklist).
REQUIRED_SETTINGS = (
    (("participant_target",), "참여 대상 (소유자·임차인·세대원 등 실제 참여 대상과 동일 세대 참여 기준)"),
    (("privacy", "controller"), "개인정보 수집·관리 주체 (실제 운영 주체의 명칭)"),
    (("privacy", "officer"), "개인정보 관리책임자 (성명)"),
    (("privacy", "contact"), "문의·열람·정정·철회 요청 연락처 (전화번호 또는 이메일)"),
    (("privacy", "retention_records"), "동의자료 보유·이용기간 (또는 명확한 종료 기준)"),
    (("privacy", "destruction_plan"), "보유기간 종료 후 파기 대상·실행 방법·백업 처리 (운영 확인, 주민 화면 미표시)"),
)
_OVERSEAS_ALTERNATIVE = (("overseas", "alternative"),
                         "국외 이전에 동의하지 않는 주민의 다른 참여 방법 (없으면 '온라인 외 다른 참여 방법은 없습니다'처럼 입력)")
_STORAGE_LOCATION = (("privacy", "storage_location"), "개인정보 저장 위치·국외 이전 안내 (서버 소재지 등)")


def required_settings(content: Mapping) -> list[tuple[tuple[str, ...], str]]:
    """Operator values this wording needs before it can be published (they depend on its own settings)."""
    overseas_on = bool((content.get("overseas") or {}).get("enabled"))
    return [*REQUIRED_SETTINGS, _OVERSEAS_ALTERNATIVE if overseas_on else _STORAGE_LOCATION]


# ------------------------------------------------------------------------------------------- text helpers
def _text(value: object, limit: int) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return text[:limit]


def _line(value: object) -> str:
    return " ".join(_text(value, LINE_MAX * 2).split())[:LINE_MAX]


def rich_blocks(text: str) -> list[list[list[tuple[str, bool]]]]:
    """paragraphs -> lines -> (text, bold) runs, for the templates and the PDF.

    A ** opened on one line stays open over the next lines of the same paragraph (publishing is blocked while a
    paragraph has an odd number of **, so nothing stays bold past its paragraph in published wording).
    """
    blocks = []
    for paragraph in re.split(r"\n\s*\n", text or ""):
        lines = [line for line in paragraph.split("\n") if line.strip()]
        if not lines:
            continue
        bold, out = False, []
        for line in lines:
            runs = []
            for i, part in enumerate(line.split("**")):
                if i:
                    bold = not bold
                if part:
                    runs.append((part, bold))
            out.append(runs)
        blocks.append(out)
    return blocks


def plain_lines(text: str) -> list[str]:
    """The text without markup, one entry per line, blank line between paragraphs."""
    out: list[str] = []
    for paragraph in rich_blocks(text):
        if out:
            out.append("")
        out.extend("".join(run for run, _ in line) for line in paragraph)
    return out


def _unbalanced_bold(text: str) -> bool:
    return any(paragraph.count("**") % 2 for paragraph in re.split(r"\n\s*\n", text or ""))


# ------------------------------------------------------------------------------------------ content model
def normalize_content(raw: Mapping) -> dict:
    """A complete, cleaned content object (missing fields filled from the seed layout, lengths capped)."""
    raw = raw or {}
    seed = SEED_CONTENT

    def text_of(source: Mapping, key: str) -> str:
        return _text(source.get(key, ""), TEXT_MAX)

    def line_of(source: Mapping, key: str) -> str:
        return _line(source.get(key, ""))

    sections = []
    for section in list(raw.get("sections") or [])[:SECTIONS_MAX]:
        level = 2 if str(section.get("level")) == "2" else 1
        title, body = line_of(section, "title"), text_of(section, "body")
        if title or body:
            sections.append({"level": level, "title": title, "body": body})

    questions, seen = [], set()
    for question in list(raw.get("questions") or [])[:QUESTIONS_MAX]:
        key = str(question.get("key") or "")
        if not _KEY.match(key) or key in seen:
            continue
        seen.add(key)
        questions.append(
            {
                "key": key,
                "title": line_of(question, "title"),
                "text": text_of(question, "text"),
                "agree_label": line_of(question, "agree_label") or "동의합니다",
                "disagree_label": line_of(question, "disagree_label") or "동의하지 않습니다",
                "active": bool(question.get("active", True)),
            }
        )

    raw_privacy = raw.get("privacy") or {}
    privacy = {
        key: (text_of(raw_privacy, key) if key in _PRIVACY_TEXT_KEYS else line_of(raw_privacy, key))
        for key in seed["privacy"]
        if key != "collect_access_info"
    }
    privacy["collect_access_info"] = bool(raw_privacy.get("collect_access_info", True))
    privacy["agree_label"] = privacy["agree_label"] or "동의합니다"
    privacy["disagree_label"] = privacy["disagree_label"] or "동의하지 않습니다"

    recipients, seen = [], set()
    for recipient in list(raw.get("recipients") or [])[:5]:
        key = str(recipient.get("key") or "")
        if not _KEY.match(key) or key in seen:
            continue
        seen.add(key)
        item = {name: (text_of(recipient, name) if name in _RECIPIENT_TEXT_KEYS else line_of(recipient, name))
                for name in seed["recipients"][0] if name != "key"}
        item["key"] = key
        item["agree_label"] = item["agree_label"] or "동의합니다"
        item["disagree_label"] = item["disagree_label"] or "동의하지 않습니다"
        recipients.append(item)

    raw_overseas = raw.get("overseas")
    source = raw_overseas or {}
    overseas = {
        key: (text_of(source, key) if key in _OVERSEAS_TEXT_KEYS else line_of(source, key))
        for key in seed["overseas"]
        if key != "enabled"
    }
    # Wording from before this section existed has none: it stays off (versions published then never asked it).
    overseas["enabled"] = bool(source.get("enabled", False)) if raw_overseas is not None else False
    overseas["agree_label"] = overseas["agree_label"] or "동의합니다"
    overseas["disagree_label"] = overseas["disagree_label"] or "동의하지 않습니다"

    hints = raw.get("field_hints") or {}
    content = {
        key: (text_of(raw, key) if key in _TOP_TEXT_KEYS else line_of(raw, key))
        for key in seed
        if key not in ("sections", "questions", "privacy", "overseas", "recipients", "field_hints")
    }
    content.update(
        sections=sections,
        questions=questions,
        privacy=privacy,
        overseas=overseas,
        recipients=recipients,
        field_hints={name: line_of(hints, name) for name in seed["field_hints"]},
    )
    return content


_TOP_TEXT_KEYS = {
    "intro", "questions_intro", "participant_target", "participation_notes", "provision_intro", "principles",
    "final_statements",
}
_PRIVACY_TEXT_KEYS = {"purpose", "retention_notes", "refusal", "storage_location", "items_input", "items_generated",
                      "destruction_plan"}
_RECIPIENT_TEXT_KEYS = {"purpose", "items", "refusal", "retention", "delivery"}
_OVERSEAS_TEXT_KEYS = {"intro", "items", "country", "timing", "recipient", "purpose", "retention", "refusal",
                       "alternative"}


def seed_content() -> dict:
    return normalize_content(copy.deepcopy(SEED_CONTENT))


def content_hash(content: Mapping) -> str:
    """SHA-256 of the content exactly as given (callers pass normalised content; a published version is hashed
    as stored, so a later change of the wording schema never changes the result for old versions)."""
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def version_hash_ok(version: ConsentVersion) -> bool:
    """Does the stored wording still match the stored hash (checked with the version's own hash scheme)?"""
    if version.hash_scheme == HASH_SCHEME:
        return content_hash(version.content) == version.content_hash
    return False  # an unknown scheme is never reported as verified


def active_questions(content: Mapping) -> list[dict]:
    return [q for q in content.get("questions", []) if q.get("active")]


def question_titles(content: Mapping) -> dict[str, str]:
    """key -> '질문 N. 제목' for the active questions, in order."""
    return {q["key"]: f"질문 {i}. {q['title']}" for i, q in enumerate(active_questions(content), start=1)}


def generated_items(content: Mapping) -> str:
    privacy = content["privacy"]
    items = privacy.get("items_generated", "")
    if privacy.get("collect_access_info"):
        items = f"{items}, {ACCESS_INFO_TEXT}" if items else ACCESS_INFO_TEXT
    return items


def _get(content: Mapping, path: tuple[str, ...]) -> str:
    value: object = content
    for part in path:
        value = value.get(part, "") if isinstance(value, Mapping) else ""
    return str(value or "")


def _all_texts(content: Mapping) -> list[str]:
    texts: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(content)
    return texts


def publish_blockers(content: Mapping) -> list[str]:
    """Why this wording may not be published yet (empty list = publishable)."""
    content = normalize_content(content)
    problems = [f"미입력: {label}" for path, label in required_settings(content) if not _get(content, path).strip()]
    privacy = content["privacy"]
    overseas = content["overseas"]
    if overseas["enabled"]:
        for name, label in (("title", "제목"), ("items", "이전되는 항목"), ("country", "이전되는 국가"),
                            ("timing", "이전 시기와 방법"), ("recipient", "이전받는 자"), ("purpose", "이용 목적"),
                            ("retention", "보유·이용기간"), ("refusal", "거부 방법과 효과"), ("question", "선택 질문")):
            if not overseas[name].strip():
                problems.append(f"미입력: 개인정보 국외 이전 안내의 {label}")
    if privacy["collect_access_info"] and not privacy["retention_access"].strip():
        problems.append("미입력: 접속 IP 주소·브라우저 정보 보유기간 (보안 목적에 필요한 실제 기간)")
    for recipient in content["recipients"]:
        if not recipient["retention"].strip():
            problems.append(f"미입력: {recipient['name'] or recipient['key']}의 보유·이용기간 (또는 보존기준)")
        if not recipient["delivery"].strip():
            problems.append(f"미입력: {recipient['name'] or recipient['key']}에 대한 제출 방식 (운영 확인, 주민 화면 미표시)")
        for name, label in (("name", "제공받는 자"), ("purpose", "이용 목적"), ("items", "제공 항목"),
                            ("refusal", "거부 시 영향"), ("question", "선택 질문")):
            if not recipient[name].strip():
                problems.append(f"미입력: 제공처 '{recipient['key']}'의 {label}")
    if not content["recipients"]:
        problems.append("제공처가 없습니다.")
    for name, label in (("title", "제목"), ("final_checkbox", "최종 확인 문구")):
        if not content[name].strip():
            problems.append(f"미입력: {label}")
    for name, label in (("question", "개인정보 수집·이용 선택 질문"), ("refusal", "개인정보 동의 거부권 안내"),
                        ("purpose", "개인정보 수집·이용 목적")):
        if not privacy[name].strip():
            problems.append(f"미입력: {label}")
    questions = active_questions(content)
    if not questions:
        problems.append("활성화된 질문이 없습니다.")
    for number, question in enumerate(questions, start=1):
        if not question["title"].strip() or not question["text"].strip():
            problems.append(f"질문 {number}의 제목과 내용을 입력해 주세요.")
    texts = _all_texts(content)
    if any(PLACEHOLDER_MARK in text for text in texts):
        problems.append("'[게시 전 입력' 표시가 남아 있는 문구가 있습니다.")
    if any(_unbalanced_bold(text) for text in texts):
        problems.append("굵게 표시(**)의 앞뒤 짝이 맞지 않는 문구가 있습니다.")
    if not privacy["collect_access_info"] and any(word in text for text in texts for word in ACCESS_INFO_WORDS):
        problems.append(
            "접속 IP·브라우저 정보를 수집하지 않도록 설정했는데 안내 문구에 해당 내용이 남아 있습니다 "
            "(실제 수집과 안내가 달라짐)."
        )
    return problems


# ------------------------------------------------------------------------------ admin editor form parsing
def content_from_form(form: Mapping[str, str], base: Mapping, reserved: Collection[str] = (),
                      published: Mapping | None = None) -> dict:
    """Build a draft from the admin editor's fields. Keys of questions/recipients come from `base` only.

    `reserved`: question keys the agenda ever used (every published version); a new question never gets one.
    `published`: the current version; a question split off ("새 질문으로 분리") leaves its old key with the
    wording published under it, not the edited one.
    """
    base = normalize_content(base)
    published_questions = {q["key"]: q for q in normalize_content(published)["questions"]} if published else {}
    value = lambda name: form.get(name, "")  # noqa: E731

    sections = []
    for index in range(SECTIONS_MAX + 1):
        prefix = f"s{index}_"
        if f"{prefix}title" not in form and f"{prefix}body" not in form:
            continue
        if value(f"{prefix}delete"):
            continue
        order = value(f"{prefix}order").strip()
        sections.append((
            int(order) if order.lstrip("-").isdigit() else index + 1,
            index,
            {"level": value(f"{prefix}level"), "title": value(f"{prefix}title"), "body": value(f"{prefix}body")},
        ))
    sections.sort(key=lambda item: (item[0], item[1]))

    known = {q["key"]: q for q in base["questions"]}
    taken = set(known) | set(reserved)
    next_number = 1 + max([int(k[1:]) for k in taken if k[1:].isdigit()] or [0])
    questions, added = [], 0
    for index in range(QUESTIONS_MAX + 1):
        prefix = f"q{index}_"
        if f"{prefix}title" not in form:
            continue
        key = value(f"{prefix}key").strip()
        title, text = value(f"{prefix}title"), value(f"{prefix}text")
        # "새 질문으로 분리": the edited wording gets a new key; the old key stays (inactive, old answers intact).
        split = key in known and bool(value(f"{prefix}split"))
        if key not in known or split:
            if not (title.strip() or text.strip()) or len(known) + added >= QUESTIONS_MAX:
                continue
            key = f"q{next_number}"
            next_number += 1
            added += 1
        order = value(f"{prefix}order").strip()
        questions.append((
            int(order) if order.lstrip("-").isdigit() else index + 1,
            index,
            {
                "key": key,
                "title": title,
                "text": text,
                "agree_label": value(f"{prefix}agree"),
                "disagree_label": value(f"{prefix}disagree"),
                "active": bool(value(f"{prefix}active")),
            },
        ))
    questions.sort(key=lambda item: (item[0], item[1]))
    kept = {q[2]["key"] for q in questions}
    missing = [q for q in base["questions"] if q["key"] not in kept]  # never drop a key: keep it, inactive
    for question in missing:
        questions.append((10_000, 0, {**published_questions.get(question["key"], question), "active": False}))

    recipients = []
    for recipient in base["recipients"]:
        index = next((i for i in range(6) if form.get(f"r{i}_key") == recipient["key"]), None)
        if index is None:
            recipients.append(recipient)
            continue
        prefix = f"r{index}_"
        recipients.append({"key": recipient["key"], **{
            name: value(prefix + name) for name in recipient if name != "key"
        }})

    privacy = {name: value(f"privacy_{name}") for name in base["privacy"] if name != "collect_access_info"}
    privacy["collect_access_info"] = bool(value("privacy_collect_access_info"))
    overseas = {name: value(f"overseas_{name}") for name in base["overseas"] if name != "enabled"}
    overseas["enabled"] = bool(value("overseas_enabled"))

    raw = {name: value(name) for name in base if isinstance(base[name], str)}
    raw.update(
        sections=[item[2] for item in sections],
        questions=[item[2] for item in questions],
        privacy=privacy,
        overseas=overseas,
        recipients=recipients,
        field_hints={name: value(f"hint_{name}") for name in base["field_hints"]},
    )
    return normalize_content(raw)


# ------------------------------------------------------------------------------------ agendas and versions
def legacy_agenda(db: Session) -> Agenda | None:
    return db.scalar(select(Agenda).where(Agenda.kind == AGENDA_LEGACY))


def public_agenda(db: Session) -> Agenda | None:
    return db.scalar(select(Agenda).where(Agenda.is_public.is_(True)))


def agenda_by_code(db: Session, code: str) -> Agenda | None:
    return db.scalar(select(Agenda).where(Agenda.code == code))


def current_version(db: Session, agenda: Agenda) -> ConsentVersion | None:
    return db.scalar(
        select(ConsentVersion).where(ConsentVersion.agenda_id == agenda.id).order_by(ConsentVersion.version_no.desc())
    )


def draft_of(db: Session, agenda: Agenda) -> ConsentDraft | None:
    return db.get(ConsentDraft, agenda.id)


def used_question_keys(db: Session, agenda: Agenda) -> set[str]:
    """Every question key in any published version of the agenda (active or not): never handed out again."""
    contents = db.scalars(select(ConsentVersion.content).where(ConsentVersion.agenda_id == agenda.id)).all()
    return {str(q.get("key")) for content in contents for q in (content or {}).get("questions", [])}


def seed_agendas(db: Session) -> None:
    """Register the legacy agenda and create the consent agenda as an unpublished draft (idempotent).

    Never changes existing agendas, versions or submissions.
    """
    try:
        _seed_agendas(db)
    except IntegrityError:
        db.rollback()  # another instance starting at the same moment registered them first


def _seed_agendas(db: Session) -> None:
    changed = False
    if legacy_agenda(db) is None:
        active = db.scalar(select(OpinionDocument).where(OpinionDocument.is_active.is_(True)))
        name = active.title if active is not None else "주민 의견서"
        db.add(Agenda(code=LEGACY_AGENDA_CODE, name=name, kind=AGENDA_LEGACY,
                      is_public=public_agenda(db) is None, accepting=True))
        changed = True
    if agenda_by_code(db, NEW_AGENDA_CODE) is None:
        agenda = Agenda(code=NEW_AGENDA_CODE, name=NEW_AGENDA_NAME, kind=AGENDA_CONSENT, is_public=False, accepting=True)
        db.add(agenda)
        db.flush()
        db.add(ConsentDraft(agenda_id=agenda.id, content=seed_content()))
        changed = True
    for draft in db.scalars(select(ConsentDraft)).all():
        if "overseas" not in (draft.content or {}):  # a draft saved before the overseas section existed
            draft.content = {**draft.content, "overseas": copy.deepcopy(SEED_CONTENT["overseas"])}
            # An editor page opened before this upgrade has no overseas fields: its save must hit the conflict check.
            draft.updated_at = datetime.now(timezone.utc)
            logger.info("consent draft of agenda %s: overseas-transfer section added", draft.agenda_id)
            changed = True
    if changed:
        db.commit()


def save_draft(db: Session, agenda: Agenda, content: Mapping, ip: str | None) -> ConsentDraft:
    draft = draft_of(db, agenda)
    content = normalize_content(content)
    if draft is None:
        draft = ConsentDraft(agenda_id=agenda.id, content=content)
        db.add(draft)
    else:
        draft.content = content
    draft.updated_at = datetime.now(timezone.utc)
    draft.updated_ip = ip
    db.commit()
    return draft


class PublishError(Exception):
    pass


_WORDING_FIELDS = ("title", "text", "agree_label", "disagree_label")


def reworded_questions(previous: Mapping | None, content: Mapping) -> list[str]:
    """Questions that keep their key (so their answers are counted together) but read differently than in the
    published version. Publishing them needs the admin's explicit "same meaning" confirmation; otherwise the
    editor's "새 질문으로 분리" gives the new wording a new key."""
    if not previous:
        return []
    before = {q["key"]: q for q in previous.get("questions", []) if q.get("active")}
    changed = []
    for number, question in enumerate(active_questions(content), start=1):
        old = before.get(question["key"])
        if old is not None and any(old.get(name) != question.get(name) for name in _WORDING_FIELDS):
            changed.append(f"질문 {number}. {question['title']}")
    return changed


def publish(db: Session, agenda: Agenda, ip: str | None, *, merge_reworded: bool = False) -> ConsentVersion:
    """Copy the draft into a new immutable version (the agenda's current version from now on)."""
    locked = db.scalar(select(Agenda).where(Agenda.id == agenda.id).with_for_update())
    draft = draft_of(db, locked)
    if draft is None:
        raise PublishError("저장된 초안이 없습니다.")
    content = normalize_content(draft.content)
    problems = publish_blockers(content)
    if problems:
        raise PublishError("게시할 수 없습니다: " + " / ".join(problems))
    digest = content_hash(content)
    current = current_version(db, locked)
    if current is not None and current.content_hash == digest:
        raise PublishError("현재 게시 버전과 달라진 점이 없습니다.")
    reworded = reworded_questions(current.content if current else None, content)
    if reworded and not merge_reworded:
        raise PublishError(
            "게시 버전과 문구가 달라진 질문이 있습니다: " + ", ".join(reworded) + ". 뜻이 같다면(오탈자·표현 수정) "
            "'이전 답변과 합산' 확인란을 체크하고, 뜻이 달라졌다면 편집 화면에서 '새 질문으로 분리'를 선택해 주세요."
        )
    number = (current.version_no if current else 0) + 1
    version = ConsentVersion(
        agenda_id=locked.id,
        version_no=number,
        label=f"{locked.code}_V{number}",
        content=content,
        content_hash=digest,
        hash_scheme=HASH_SCHEME,
        created_ip=ip,
    )
    db.add(version)
    db.commit()
    return version


def make_public(db: Session, agenda: Agenda) -> None:
    """Show this agenda at /opinion. The previously public agenda and all its records stay stored."""
    if agenda.kind == AGENDA_CONSENT:
        version = current_version(db, agenda)
        if version is None:
            raise PublishError("게시된 버전이 없어 공개할 수 없습니다. 먼저 초안을 게시해 주세요.")
        problems = publish_blockers(version.content)
        if problems:
            raise PublishError("게시 버전에 미완성 항목이 있어 공개할 수 없습니다: " + " / ".join(problems))
    current = public_agenda(db)
    if current is not None and current.id != agenda.id:
        current.is_public = False
        db.flush()  # one public agenda at a time (partial unique index)
    agenda.is_public = True
    agenda.public_since = datetime.now(timezone.utc)
    db.commit()


def set_accepting(db: Session, agenda: Agenda, accepting: bool) -> None:
    """Open or pause submissions. Pausing keeps the page readable and every record as it is."""
    agenda.accepting = accepting
    db.commit()


def create_agenda(db: Session, name: str, content: Mapping) -> Agenda:
    """A new, unpublished consent agenda (its own records and its own one-per-unit rule)."""
    name = _line(name)[:200]
    if not name:
        raise PublishError("안건 이름을 입력해 주세요.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    number = db.scalar(select(func.count()).select_from(Agenda)) + 1
    code = f"CONSENT_{stamp}_{number}"
    while agenda_by_code(db, code) is not None:
        number += 1
        code = f"CONSENT_{stamp}_{number}"
    agenda = Agenda(code=code, name=name, kind=AGENDA_CONSENT, is_public=False, accepting=True)
    db.add(agenda)
    db.flush()
    db.add(ConsentDraft(agenda_id=agenda.id, content=normalize_content(content)))
    db.commit()
    return agenda


def version_by_label(db: Session, agenda: Agenda, label: str) -> ConsentVersion | None:
    return db.scalar(
        select(ConsentVersion).where(ConsentVersion.agenda_id == agenda.id, ConsentVersion.label == label)
    )


def change_status(db: Session, submission: ConsentSubmission, status: str, reason: str,
                  already_delivered: bool) -> None:
    """Invalidate or withdraw a valid record. The record and its signature stay stored; only the status changes."""
    if status not in (STATUS_INVALIDATED, STATUS_WITHDRAWN):
        raise ValueError(status)
    submission.status = status
    submission.status_changed_at = datetime.now(timezone.utc)
    submission.status_reason = reason or None
    submission.already_delivered = already_delivered
    db.commit()


def purge_access_info(db: Session, agenda: Agenda, older_than_days: int) -> int:
    """Erase IP address and browser data of this agenda's records submitted more than N days ago.

    The retention period for this data is the operator's decision; this is how it is carried out. Returns the
    number of records changed. Backups taken earlier still hold the data until they expire.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, older_than_days))
    rows = db.scalars(
        select(ConsentSubmission).where(
            ConsentSubmission.agenda_id == agenda.id,
            ConsentSubmission.submitted_at <= cutoff,
            (ConsentSubmission.ip_address.is_not(None)) | (ConsentSubmission.user_agent.is_not(None)),
        )
    ).all()
    for row in rows:
        row.ip_address = None
        row.user_agent = None
    db.commit()
    return len(rows)


def withdraw_provision(db: Session, provision: ConsentProvision, reason: str, already_delivered: bool) -> None:
    """The resident withdrew consent to one recipient: the record leaves that recipient's list and files."""
    provision.withdrawn_at = datetime.now(timezone.utc)
    provision.withdrawn_reason = reason or None
    provision.already_delivered = already_delivered
    db.commit()


# ----------------------------------------------------------------------------------- resident submissions
@dataclass(frozen=True)
class ConsentInput:
    building: str
    unit: str
    resident_name: str
    signature_png: bytes
    answers: dict[str, str]
    provisions: dict[str, bool]
    client_token: str | None
    overseas: bool | None = None  # None: the version did not ask for overseas-transfer consent

    def request_hash(self, version_label: str) -> str:
        payload = {
            "version": version_label,
            "building": self.building,
            "unit": self.unit,
            "name": self.resident_name,
            "answers": self.answers,
            "provisions": self.provisions,
            "signature": hashlib.sha256(self.signature_png).hexdigest(),
        }
        if self.overseas is not None:
            payload["overseas"] = self.overseas
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
OVERSEAS_REQUIRED_MESSAGE = (
    "개인정보 국외 이전에 동의하지 않으면 온라인으로 제출할 수 없습니다. 안내된 다른 참여 방법을 이용해 주세요."
)


def valid_token(token: str | None) -> bool:
    return bool(token) and bool(_TOKEN.match(token))


def validate_consent(form: Mapping[str, str], content: Mapping) -> ConsentInput:
    """Server-side check of a resident's consent form against the version they were shown."""
    errors: dict[str, str] = {}
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

    answers = {}
    for number, question in enumerate(active_questions(content), start=1):
        value = (form.get(f"answer_{question['key']}") or "").strip()
        if value in ANSWERS:
            answers[question["key"]] = value
        else:
            errors[f"answer_{question['key']}"] = f"질문 {number}에 대한 답변을 선택해 주세요."

    privacy = (form.get("privacy_consent") or "").strip()
    if privacy == OPINION_DISAGREE:
        errors["privacy_consent"] = "개인정보 수집·이용에 동의하지 않으면 온라인 동의서를 제출할 수 없습니다."
    elif privacy != OPINION_AGREE:
        errors["privacy_consent"] = "개인정보 수집·이용 동의 여부를 선택해 주세요."

    overseas = None
    if (content.get("overseas") or {}).get("enabled"):
        value = (form.get("overseas_consent") or "").strip()
        if value == OPINION_DISAGREE:
            errors["overseas_consent"] = OVERSEAS_REQUIRED_MESSAGE
        elif value != OPINION_AGREE:
            errors["overseas_consent"] = "개인정보 국외 이전 동의 여부를 선택해 주세요."
        else:
            overseas = True

    provisions = {}
    for recipient in content["recipients"]:
        value = (form.get(f"provide_{recipient['key']}") or "").strip()
        if value in ANSWERS:
            provisions[recipient["key"]] = value == OPINION_AGREE
        else:
            errors[f"provide_{recipient['key']}"] = f"{recipient['short_name'] or recipient['name']}에 대한 제공 동의 여부를 선택해 주세요."

    if (form.get("final_confirmed") or "").strip().lower() not in {"on", "true", "1", "yes"}:
        errors["final_confirmed"] = "최종 확인에 체크해 주세요."

    token = (form.get("client_token") or "").strip()
    if errors:
        raise FormErrors(errors)
    return ConsentInput(
        building=building or "",
        unit=unit or "",
        resident_name=name,
        signature_png=signature_png,
        answers=answers,
        provisions=provisions,
        client_token=token if _TOKEN.match(token) else None,
        overseas=overseas,
    )


def active_submission_exists(db: Session, agenda_id: int, building: str, unit: str) -> bool:
    return db.scalar(
        select(func.count()).select_from(ConsentSubmission).where(
            ConsentSubmission.agenda_id == agenda_id,
            ConsentSubmission.building == building,
            ConsentSubmission.unit == unit,
            ConsentSubmission.status == STATUS_ACTIVE,
        )
    ) > 0


# ------------------------------------------------------------------------------------- counts and filters
@dataclass
class QuestionCount:
    key: str
    title: str
    active: bool
    agree: int = 0
    disagree: int = 0


@dataclass
class RecipientCount:
    key: str
    name: str
    short_name: str
    agreed: int = 0
    declined: int = 0
    withdrawn: int = 0
    named: dict[str, int] = field(default_factory=dict)  # question key -> named agreers for this recipient
    eligible: int = 0  # records that go into this recipient's files (provision agreed + at least one request agreed)


@dataclass
class ConsentStats:
    active: int
    invalidated: int
    withdrawn: int
    questions: list[QuestionCount]
    recipients: list[RecipientCount]
    by_version: list[tuple[str, int]]
    all_agree: int = 0  # valid records that agreed to every question they were asked
    some_agree: int = 0
    none_agree: int = 0  # valid records that agreed to no question: counted as nobody's agreer


def question_catalog(db: Session, agenda: Agenda, version: ConsentVersion | None = None) -> list[QuestionCount]:
    """Every question key ever published for the agenda: current questions in their current order and
    numbering, then keys that only older versions asked (their answers stay countable).

    With `version`: exactly that version's questions, numbered and worded as in that version.
    """
    if version is not None:
        return [QuestionCount(q["key"], f"질문 {n}. {q['title']}", True)
                for n, q in enumerate(active_questions(version.content), start=1)]
    versions = db.scalars(
        select(ConsentVersion).where(ConsentVersion.agenda_id == agenda.id).order_by(ConsentVersion.version_no.desc())
    ).all()
    catalog: dict[str, QuestionCount] = {}
    for rank, version in enumerate(versions):
        for number, question in enumerate(active_questions(version.content), start=1):
            if question["key"] not in catalog:
                title = f"질문 {number}. {question['title']}" if rank == 0 else f"{question['title']} (이전 버전 질문)"
                catalog[question["key"]] = QuestionCount(question["key"], title, rank == 0)
    return list(catalog.values())  # the current version's order first, then keys only older versions asked


def recipient_catalog(db: Session, agenda: Agenda, version: ConsentVersion | None = None) -> list[RecipientCount]:
    version = version or current_version(db, agenda)
    content = version.content if version else (draft_of(db, agenda).content if draft_of(db, agenda) else SEED_CONTENT)
    return [RecipientCount(r["key"], r["name"], r["short_name"] or r["name"]) for r in content["recipients"]]


def consent_stats(db: Session, agenda: Agenda, version: ConsentVersion | None = None) -> ConsentStats:
    """Counts of the agenda, or of one version only (its records, its questions as worded then)."""
    scope = [ConsentSubmission.agenda_id == agenda.id]
    if version is not None:
        scope.append(ConsentSubmission.version_id == version.id)
    by_status = dict(
        db.execute(
            select(ConsentSubmission.status, func.count())
            .where(*scope)
            .group_by(ConsentSubmission.status)
        ).all()
    )
    questions = question_catalog(db, agenda, version)
    index = {q.key: q for q in questions}
    for key, answer, count in db.execute(
        select(ConsentAnswer.question_key, ConsentAnswer.answer, func.count())
        .join(ConsentSubmission, ConsentSubmission.id == ConsentAnswer.submission_id)
        .where(*scope, ConsentSubmission.status == STATUS_ACTIVE)
        .group_by(ConsentAnswer.question_key, ConsentAnswer.answer)
    ).all():
        entry = index.get(key)
        if entry is None:  # answers to a key no published version lists (should not happen)
            entry = index[key] = QuestionCount(key, key, False)
            questions.append(entry)
        if answer == OPINION_AGREE:
            entry.agree = count
        else:
            entry.disagree = count

    recipients = recipient_catalog(db, agenda, version)
    by_key = {r.key: r for r in recipients}
    for key, agreed, withdrawn, count in db.execute(
        select(
            ConsentProvision.recipient_key,
            ConsentProvision.agreed,
            ConsentProvision.withdrawn_at.is_not(None),
            func.count(),
        )
        .join(ConsentSubmission, ConsentSubmission.id == ConsentProvision.submission_id)
        .where(*scope, ConsentSubmission.status == STATUS_ACTIVE)
        .group_by(ConsentProvision.recipient_key, ConsentProvision.agreed, ConsentProvision.withdrawn_at.is_not(None))
    ).all():
        entry = by_key.get(key)
        if entry is None:
            continue
        if not agreed:
            entry.declined += count
        elif withdrawn:
            entry.withdrawn += count
        else:
            entry.agreed += count
    for key, question_key, count in db.execute(
        select(ConsentProvision.recipient_key, ConsentAnswer.question_key, func.count())
        .join(ConsentSubmission, ConsentSubmission.id == ConsentProvision.submission_id)
        .join(ConsentAnswer, ConsentAnswer.submission_id == ConsentSubmission.id)
        .where(
            *scope,
            ConsentSubmission.status == STATUS_ACTIVE,
            ConsentProvision.agreed.is_(True),
            ConsentProvision.withdrawn_at.is_(None),
            ConsentAnswer.answer == OPINION_AGREE,
        )
        .group_by(ConsentProvision.recipient_key, ConsentAnswer.question_key)
    ).all():
        if key in by_key:
            by_key[key].named[question_key] = count

    for key, count in db.execute(
        select(ConsentProvision.recipient_key, func.count())
        .join(ConsentSubmission, ConsentSubmission.id == ConsentProvision.submission_id)
        .where(
            *scope,
            ConsentSubmission.status == STATUS_ACTIVE,
            ConsentProvision.agreed.is_(True),
            ConsentProvision.withdrawn_at.is_(None),
            _agreed_to_something(),
        )
        .group_by(ConsentProvision.recipient_key)
    ).all():
        if key in by_key:
            by_key[key].eligible = count

    patterns = {"all": 0, "some": 0, "none": 0}
    for agreed, answered in db.execute(
        select(func.sum(case((ConsentAnswer.answer == OPINION_AGREE, 1), else_=0)), func.count(ConsentAnswer.id))
        .join(ConsentSubmission, ConsentSubmission.id == ConsentAnswer.submission_id)
        .where(*scope, ConsentSubmission.status == STATUS_ACTIVE)
        .group_by(ConsentAnswer.submission_id)
    ).all():
        agreed = int(agreed or 0)
        patterns["none" if agreed == 0 else "all" if agreed == answered else "some"] += 1

    by_version = db.execute(
        select(ConsentSubmission.version_label, func.count())
        .where(ConsentSubmission.agenda_id == agenda.id, ConsentSubmission.status == STATUS_ACTIVE)
        .group_by(ConsentSubmission.version_label)
        .order_by(ConsentSubmission.version_label)
    ).all()
    return ConsentStats(
        active=int(by_status.get(STATUS_ACTIVE, 0)),
        invalidated=int(by_status.get(STATUS_INVALIDATED, 0)),
        withdrawn=int(by_status.get(STATUS_WITHDRAWN, 0)),
        questions=questions,
        recipients=recipients,
        by_version=[(label, int(count)) for label, count in by_version],
        all_agree=patterns["all"],
        some_agree=patterns["some"],
        none_agree=patterns["none"],
    )


def _agreed_to_something():
    return exists().where(ConsentAnswer.submission_id == ConsentSubmission.id, ConsentAnswer.answer == OPINION_AGREE)


def eligible_submissions(db: Session, agenda: Agenda, recipient_key: str) -> list[ConsentSubmission]:
    """The records that may go into this recipient's files, oldest first (see eligible_for)."""
    rows = db.scalars(
        select(ConsentSubmission)
        .where(
            ConsentSubmission.agenda_id == agenda.id,
            ConsentSubmission.status == STATUS_ACTIVE,
            exists().where(
                ConsentProvision.submission_id == ConsentSubmission.id,
                ConsentProvision.recipient_key == recipient_key,
                ConsentProvision.agreed.is_(True),
                ConsentProvision.withdrawn_at.is_(None),
            ),
            _agreed_to_something(),
        )
        .order_by(ConsentSubmission.submitted_at.asc(), ConsentSubmission.id.asc())
    ).all()
    return [row for row in rows if eligible_for(row, recipient_key)]  # the same rule, checked twice


def eligible_for(submission: ConsentSubmission, recipient_key: str) -> bool:
    """May this record go to the recipient? Valid, provision agreed and not withdrawn, and at least one
    request agreed. The file then shows the person's actual answer to every question."""
    if submission.status != STATUS_ACTIVE:
        return False
    provision = submission.provision_map().get(recipient_key)
    if provision is None or not provision.effective:
        return False
    return any(answer == OPINION_AGREE for answer in submission.answer_map().values())
