# e편한세상강동에코포레 온라인 주민의견서 (MVP)

입주민이 링크 하나로 접속해 주민의견서를 읽고, 의견을 선택한 뒤 동·호수·성명과 직접 서명을 입력해 제출하는 웹페이지입니다.
회원가입·로그인·휴대폰 인증·GPS 수집은 없습니다. 선거나 전자투표 시스템이 아니라, 종이 주민의견서를 온라인으로 접수해 DB에 보관하는 서비스입니다.

- 주민 페이지: `/` → `/opinion`
- 관리자: `/admin` (비밀번호 로그인)

## 주요 기능

**주민 페이지 (모바일 우선)**
- 의견서 본문 표시 (PDF 원문 그대로, 굵은 글씨 유지)
- 의견 선택: `위 주민의견서 내용에 동의합니다.`(AGREE) / `위 주민의견서 내용에 동의하지 않습니다.`(DISAGREE)
- 동·호수(숫자, 뒤에 `동`/`호` 자동 표시), 성명, 손가락·마우스 직접 서명(Canvas), 기타 의견(선택, 1,000자)
- 본인 의사 확인·제출 및 활용 동의 체크 (둘 다 체크해야 제출 버튼 활성화)
- 제출 전 확인 창 → 최종 제출 → 완료 화면(제출일시, 제출번호 8자리)
- 필드별 오류 표시, 중복 제출 차단, 저장 실패 시 성공 화면을 띄우지 않음

**관리자 페이지 (PC 표 / 휴대폰 카드형)**
- 집계: 전체 제출 / 의견서 내용 동의 / 의견서 내용 동의하지 않음 (유효 건만)
- 목록: 최신순, 동·호수·성명 검색, 의견·상태 필터, 서명 확대 보기, 접속 IP·브라우저(필요할 때만 펼쳐 보기)
- 무효 처리(삭제 아님, 사유 기록 가능) → 집계 제외, 같은 동·호수 재제출 가능
- CSV 다운로드 (엑셀에서 한글이 깨지지 않도록 UTF-8 BOM 포함)
- **의견서 문구 수정**: 제목·본문·하단 안내·제출처를 고쳐 미리보기 후 게시 → 새 버전(V2, V3…)으로 주민 페이지에 즉시 반영. 버전별 전체 문구·해시·제출 건수 확인, 이전 문구로 되돌리기(새 버전으로 재게시)

## 폴더 구조

```
app/
  main.py            앱 생성, 보안 헤더, 세션, 라우터 연결
  config.py          환경변수
  database.py        DB 연결 풀, 테이블 생성(삭제 없음)
  models.py          opinion_documents, opinion_submissions
  document.py        의견서 문구·버전·해시 (첫 버전 원문 포함)
  schemas.py         제출값 검증, 서명 PNG 검증·재압축
  security.py        관리자 세션, CSRF, 접속 IP, 로그인 시도 제한
  templating.py      템플릿, 한국시간 표시
  init_db.py         python -m app.init_db (테이블 생성)
  routes/public.py   주민 페이지·제출·완료
  routes/admin.py    관리자 로그인·목록·서명·무효 처리·CSV
  routes/admin_document.py  의견서 문구 수정·게시·버전 보기
  templates/         HTML (Jinja2)
  static/css, static/js
tests/               pytest (39개)
render.yaml          Render Blueprint
requirements.txt     운영 의존성 / requirements-dev.txt 테스트용
```

## 로컬 실행

1. Python 3.12 (3.13도 동작)로 가상환경을 만들고 의존성을 설치합니다.
   ```bash
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements-dev.txt
   ```
2. PostgreSQL에 DB를 만들고 `.env.example`을 `.env`로 복사해 값을 채웁니다 (`DATABASE_URL`, `SECRET_KEY`, `ADMIN_PASSWORD`, `APP_ENV=development`).
3. 실행합니다. 첫 실행 때 테이블과 의견서 첫 버전이 자동으로 만들어집니다 (`python -m app.init_db`로 따로 만들 수도 있음).
   ```bash
   .venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
   ```
4. 브라우저에서 `http://127.0.0.1:8000/` (주민), `http://127.0.0.1:8000/admin` (관리자)

### 테스트

```bash
.venv\Scripts\python -m pytest
```

`TEST_DATABASE_URL`(환경변수 또는 `.env`)이 있으면 그 PostgreSQL DB로, 없으면 임시 SQLite로 실행합니다.
테스트는 테이블을 지우고 다시 만들기 때문에 **DB 이름이 `_test`로 끝나야만** 실행됩니다.

## 환경변수

| 이름 | 필수 | 설명 |
|---|---|---|
| `DATABASE_URL` | 예 | PostgreSQL 주소. `postgres://`, `postgresql://` 모두 가능 |
| `SECRET_KEY` | 운영 필수 | 관리자 세션·완료 화면 서명용, 32자 이상 임의 문자열. Render Blueprint가 자동 생성 |
| `ADMIN_PASSWORD` | 예 | 관리자 비밀번호, 8자 이상 (12자 이상 권장). 없으면 관리자 로그인 불가 |
| `APP_ENV` | 아니오 | 기본 `production` (보안 쿠키·HSTS). 로컬은 `development` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | 아니오 | DB 연결 풀 (기본 5 + 10) |

관리자 비밀번호를 바꾸면 로그인되어 있던 관리자는 모두 자동으로 로그아웃됩니다.

## DB 구조

**opinion_submissions** (제출)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| id | integer PK | 내부 번호 (화면에 노출 안 함) |
| public_id | uuid, unique | 제출번호의 원본 (앞 8자리를 제출번호로 표시) |
| building / unit | varchar | 동 / 호수 (`0101`, `101동`, 전각 숫자도 `101`로 정규화) |
| resident_name | varchar(50) | 성명 |
| opinion_choice | varchar | `AGREE` / `DISAGREE` (CHECK 제약) |
| signature_data | **bytea** | 서명 PNG (흰 배경 흑백, 600×300, 보통 3~8KB) |
| additional_comment | text | 기타 의견 (최대 1,000자) |
| statement_confirmed / usage_consent | boolean | 두 동의 (둘 다 true가 아니면 DB가 거부) |
| document_title / document_version / document_text_hash | varchar | 제출 당시 의견서 제목·버전·본문 SHA-256 |
| ip_address / user_agent | varchar | 접속 IP·브라우저 (보조 자료) |
| submitted_at | timestamptz | 제출 시각 = 서버 수신 시각 (UTC 저장, 화면은 한국시간) |
| created_at | timestamptz | DB 저장 시각 |
| status | varchar | `ACTIVE` / `INVALIDATED` |
| invalidated_at / invalidated_reason | | 무효 처리 시각·사유 |

- **중복 방지**: `UNIQUE (building, unit) WHERE status = 'ACTIVE'` 부분 유니크 인덱스. 같은 동·호수의 유효 제출은 DB에서 1건만 허용하고, 무효 처리된 기록은 남겨 둔 채 재제출을 허용합니다. (단순 `UNIQUE(building, unit)`이면 무효 처리 후 재제출이 불가능해서 이렇게 구성했습니다.)
- 동시에 같은 동·호수로 제출해도 DB 인덱스가 한 건만 받아들입니다.

**opinion_documents** (의견서 버전)

| 컬럼 | 설명 |
|---|---|
| version_no, version | 버전 번호, 이름 (`EPOXY_OPINION_V1`, `…_V2`) |
| title, body, notes, recipient | 제목, 본문(빈 줄로 문단 구분, `**굵게**`), 하단 안내(줄마다 한 항목), 제출처 |
| text_hash | 본문 SHA-256 |
| is_active | 주민 페이지에 게시 중인 버전 (DB가 1개만 허용) |
| created_at, created_ip | 게시 시각, 게시한 관리자 IP |

버전은 수정·삭제되지 않고 새로 쌓이기만 합니다.

## 의견서 문구 수정과 버전

- 관리자 `의견서 문구` 메뉴에서 수정 → **미리보기** → **게시**. 게시하는 순간 주민 페이지가 새 버전으로 바뀝니다.
- 이미 제출된 의견서는 제출 당시 버전 이름과 해시를 그대로 가지고 있고, 관리자는 버전별 전체 문구를 볼 수 있습니다.
- 게시 순간 작성 중이던 주민이 제출하면 "의견서 내용이 변경되었습니다. 새로고침…" 안내가 나오고 저장되지 않습니다 (옛 문구에 서명한 것으로 기록되지 않도록).
- 수집 도중 문구를 바꾸면 주민마다 서명한 문구가 달라집니다. 가능하면 링크를 배포하기 전에 문구를 확정해 주세요.
- 해시 계산: `제목`, 각 문단(굵게 표시 기호 제외), 각 하단 안내, `제출처: …`를 줄바꿈으로 이어 NFC 정규화한 UTF-8 문자열의 SHA-256. `python -m app.document`로 첫 버전 원문과 해시를 출력할 수 있습니다.
- 첫 버전(V1)은 `주민의견서에코포레.pdf`와 같습니다. 단, 하단 두 번째 안내는 개발 지시서 문구(`…제출될 수 있습니다.`)를 따랐습니다. PDF 문구는 `…제출될 수 있음에 동의합니다.`이며, 온라인에서는 이 동의를 별도 체크박스로 받습니다.

## 서명 저장

- 화면 크기와 상관없이 필기 좌표를 비율로 저장했다가 600×300 흰 배경 PNG로 다시 그려 보내므로, 화면 크기가 달라도 서명이 잘리지 않습니다.
- 서버에서 PNG를 다시 검사하고(형식·크기·실제 필기 여부) 흑백 PNG로 재압축해 PostgreSQL `bytea`에 저장합니다. Render 디스크에는 아무것도 저장하지 않습니다.
- 서명 영역에서는 페이지가 스크롤·확대되지 않습니다 (`touch-action: none` + 터치 이벤트 차단).

## 보안

- SQL은 모두 SQLAlchemy 파라미터 바인딩, 화면 출력은 자동 이스케이프, CSV는 수식 실행 방지 처리
- 관리자: 환경변수 비밀번호, 서명된 세션 쿠키(HttpOnly, SameSite=Lax, 운영에서 Secure), 8시간 후 만료, 같은 IP 10회 실패 시 15분 차단
- CSRF: 관리자 폼은 세션 토큰 + 출처 확인, 주민 제출은 같은 출처의 fetch 요청만 허용
- 보안 헤더: CSP(외부 스크립트 없음), 프레임 삽입 금지, HSTS(운영), 검색엔진 수집 금지(noindex), 위치·카메라 권한 차단
- API 문서(/docs) 비활성화, 디버그 모드 없음, DB 주소·비밀번호 코드에 없음
- 완료 화면 주소는 서버가 서명한 값이라 위조한 "제출 완료" 링크는 열리지 않습니다

## Render 배포

### Blueprint로 배포 (권장)

1. 이 폴더를 GitHub 저장소로 올립니다 (`.env`, `.localdb/`는 `.gitignore`로 제외됨).
2. Render 대시보드 → **New → Blueprint** → 저장소 선택.
3. `render.yaml`대로 웹 서비스 `ecoforet-opinion`과 PostgreSQL `ecoforet-opinion-db`(싱가포르)가 만들어집니다.
   - `DATABASE_URL`: DB와 자동 연결 / `SECRET_KEY`: 자동 생성 / `APP_ENV`: production
   - `ADMIN_PASSWORD`: 적용 화면에서 직접 입력 (12자 이상 권장)
4. 배포가 끝나면 확인합니다.
   - `https://ecoforet-opinion.onrender.com/healthz` → `{"status":"ok"}`
   - `https://ecoforet-opinion.onrender.com/` → 의견서 화면
   - `https://ecoforet-opinion.onrender.com/admin` → 관리자 로그인
   (서비스 이름이 이미 쓰이고 있으면 주소 뒤에 임의 문자가 붙습니다. 대시보드에 표시된 주소를 쓰세요.)

실행 명령 (render.yaml에 포함):
```
uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*" --no-access-log
```
빌드 명령: `pip install -r requirements.txt`, 헬스체크: `/healthz`, Python 버전: `.python-version` (3.12)

### 요금제 주의

- **무료 Postgres는 생성 30일 후 만료되고, 14일 뒤 데이터가 삭제됩니다 (백업 없음).** 주민 의견을 보관해야 하므로 유료 플랜(render.yaml의 `0.1c-256mb`)을 권장합니다.
- 무료 웹 서비스는 15분 동안 접속이 없으면 잠들고, 다음 접속 때 약 1분 걸려 깨어납니다. 카톡 링크를 누른 주민이 1분 동안 빈 화면을 볼 수 있어 수집 기간에는 유료(`0.5c-512mb`)를 권장합니다.
- 두 가지 모두 `render.yaml`의 `plan`을 `free`로 바꾸면 무료로 시험 배포할 수 있습니다.

### 데이터 보관

- 관리자 CSV 다운로드로 수시로 사본을 받아 두세요. 서명 이미지는 DB에만 있습니다.
- DB는 인터넷에서 직접 접속할 수 없게 막혀 있습니다(`ipAllowList: []`). PC에서 직접 조회하려면 Render 대시보드에서 해당 PC IP를 잠시 허용하세요.
- 테이블 생성 코드는 없는 테이블만 만들고 기존 테이블·데이터를 지우거나 다시 만들지 않습니다.

## 링크 배포 전 점검

- [ ] `ADMIN_PASSWORD`를 12자 이상으로 설정
- [ ] 관리자 `의견서 문구`에서 게시 중인 문구 최종 확인
- [ ] 휴대폰으로 시험 제출 1건 → 관리자에서 확인 → 무효 처리 (집계에서 빠짐)
- [ ] 실제 기기 확인: iPhone Safari, Android Chrome(삼성 인터넷 포함), 카카오톡 링크(인앱 브라우저), PC Chrome
  - 서명할 때 페이지가 위아래로 움직이지 않는지, 서명이 잘리지 않는지, 제출 확인 창과 완료 화면이 보이는지
