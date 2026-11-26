# CLAUDE.md — EasyQ 서비스(백엔드)

EasyQ: 강연 중 청중 질문에 교안 · 발화 근거로 AI가 답하고, 근거가 부족하면 강연자에게 넘기는 서비스.
**이 레포지토리에는 서비스(auth · backend · worker)만 담는다.** frontend · caddy · trainer(학습서버)는 범위 밖이다.

## 역할

- 너는 백엔드 파트(설계서 기준 담당: 신재우 · 조현준)를 개발한다.
- AI(엄윤종 · 최의찬) · 프론트엔드(심주혜) 영역은 만들지 않는다. 그쪽과 맞물리는 계약(학습서버 API 7.4, WebSocket 7.3.10, 응답 객체 7.3.1)은 설계서대로 맞추고 바꾸지 않는다.

## 기준 문서

- 원본: `설계서/(CodingAnt팀)설계서_v1.0(엄윤종,심주혜,신재우,조현준,최의찬).hwp`
- 백엔드 v1.2 반영본: `docs/CodingAnt팀_설계서_v1.0_백엔드v1.2반영.hwpx` (읽을 때는 압축을 풀어 `Contents/section0.xml`의 `<hp:t>` 텍스트를 뽑는다)
- DB 스키마: `DB 스키마파일/easyq_schema_v1.2.sql` (내용은 원본 그대로, PostgreSQL 적용을 위해 UNIQUE · 인덱스 블록만 FK 앞으로 옮김)
- **설계 v1.3 작업본은 2026-11-25 팀 회의에서 반려됐다.** v1.2 반영본을 따른다. 반려본은 로컬 `docs/archive/v1.3-반려/`에만 있고 레포에는 없다.
- **AI팀 협업 규칙: `docs/EasyQ_AI팀_협업규칙.md`** — 교안 추출 · 정리 · 청킹(400토큰), 발화 30초, retrieval 패키지 · 학습서버 연동 경계. 교안 처리 · 발화 색인 · 검색 · 판정 · 학습서버 작업 전에 먼저 읽는다.
- 참고: `docs/EasyQ_팀공유_보류사항.md`, `docs/EasyQ_통합검토_유스케이스_API.md`, `docs/EasyQ_Mermaid_원본.md`

주로 보는 절:

| 절 | 내용 |
|---|---|
| 6.1 | 시스템 아키텍처(구성, 비동기 작업 · 실시간 전달, 외부 연동, 질문 판정, 공용 검색 모듈, 평가용 강의 판정) |
| 6.2 | 사용자 기능 시퀀스(6.2.1~6.2.15) |
| 6.3 | 관리자 기능 시퀀스(6.3.1~6.3.7) |
| 7.1 | 서버/클라이언트 구조, 권한 · 인증, 공통 규칙, 동의, 공개 경로 분기 |
| 7.2 | 서비스 REST API 65개 목록 |
| 7.3 | REST API 상세, 공통 응답 객체(7.3.1), WebSocket(7.3.10), auth 내부 API(7.3.11) |
| 8.1 | ERD |
| 8.2 | 테이블 명세(8.2.1 공통 규칙 ~ 8.2.38) |
| 8.3 | 로그 파일 설계 |
| 8.4 | 오류 코드 · 오류 파일 |

**설계와 다르게 구현해야 하면 코드부터 바꾸지 말고 먼저 알린다.** 어느 절의 무엇이 왜 안 맞는지, 대안을 적어 사용자에게 묻고, 답을 받은 뒤 구현한다. 설계서에 없는 API · 테이블 · 컬럼 · 상태값 · 오류 코드를 임의로 추가하지 않는다.

## 기술 스택 (설계서 10.1)

Python 3.12 · uv(lock 파일로 버전 고정) · FastAPI + Uvicorn · SQLAlchemy 2(async) · Pydantic v2 · Alembic · Ruff · pytest
PostgreSQL 16 + pgvector(RDS) · S3 · kiwipiepy · PyMuPDF · python-pptx · google-genai(STT · 임베딩) · anthropic(Sonnet) · fastapi-mail(Gmail SMTP 587)

## 구조 (컨테이너별, 2026-11-26 사용자 요청으로 재정리)

```
compose.yaml              로컬 개발: 이미지에는 라이브러리만, 코드는 컨테이너마다 바인드 마운트(자동 재시작)
common/easyq_common/      세 컨테이너 공용: core.py(ServiceCore: 실행 · 세션 확인 · CSRF · 동의 · 이벤트 · 작업 · 요청 한도 ·
                          강연 접근 · 강연 종료), contracts, security, config, db, http(ASGI 전송 · OpenAPI), schemas(Swagger),
                          storage(S3 · 로컬), auth_client(INT-01~03 호출)
auth/                     인증 컨테이너(내부 8000, 개발 :8001) — Dockerfile, requirements.txt
  easyq_auth/             main(앱), accounts(가입 · 인증 · 로그인 · 재설정), sessions(세션 · auth 경로 · 내부 API),
                          admin(ADMIN 생성 명령), devseed(개발 테스트 계정)
backend/                  backend 컨테이너(내부 8000) — Dockerfile, requirements.txt
  easyq_backend/          main(앱 · WebSocket), routes, lectures, participation, consents, documents, stt(Gemini Live),
                          live/(protocol · events · audio · connection: 7.3.10 WebSocket)
worker/                   worker 컨테이너(수신 포트 없음) — Dockerfile, requirements.txt
  easyq_worker/           main(루프), runner(점유 · lease · 재시도 · 유지보수), worker(조립), ingest, transcript, mail,
                          cleanup, retrieval, terms, budget, embedding
migrations/               001 인증 뷰, 002 실행 계정 권한(예시)
dev/                      db-init(개발 계정), caddy(개발 Caddyfile), trainer-mock(학습서버 목업)
deploy/                   Caddyfile.example(운영)
tests/ · scripts/         SQLite 하네스 · PostgreSQL 시험, run_tests.py
docs/service/             API 정리, 구현 설명, 실행 안내, 프론트 연동, 시험 결과
```

- 설계서 10.2는 backend와 worker를 같은 이미지로 정의하지만, 사용자 요청으로 컨테이너별 폴더 · 이미지로 나눴다. 공용 코드는 `common/`에 두고 각 이미지가 함께 담는다.
- 목표 기술 스택(SQLAlchemy async · Alembic · uv · pytest)으로의 전환은 아직이다. 전환할 때는 먼저 알린다.
- 설계서 10.2는 frontend · caddy · trainer까지 한 저장소로 정의하지만, 팀은 레포를 나눴다: 이 레포(서비스), AI 레포(https://github.com/EasyQ-P/AI — trainer + retrieval), frontend 레포.
- **공용 검색 모듈 `retrieval`(6.1.5)은 AI 레포에 있다.** 이 레포에 복사하거나 다시 만들지 않고, 태그로 고정해 설치한다.
  - `uv add "retrieval @ git+https://github.com/EasyQ-P/AI@vX.Y.Z#subdirectory=retrieval"`
  - 청킹 · 키워드 색인 · RRF 검색 · 겹침 계산 · 검색 설정 버전(rc-vN)을 바꿔야 하면 AI 레포에 요청(최의찬)하고, 새 태그가 나오면 이쪽 버전을 올린다.
- worker의 R2~R4(STT 중계 · 발화 색인 · 검색 · 판정 · 답변) 서비스 코드는 이 레포에서 작업한다(AI 레포 README와 같은 기준).

## 핵심 규칙

### 실행 단위와 경계
- auth만 비밀번호 해시 · 인증 코드 · 재설정 토큰 검증값을 읽고 쓴다. backend는 `auth_session_view`(세션 해시 · 만료 · 폐기, 회원 상태 · 역할 · 이메일 인증, 게스트 참가 상태)만 읽는다. DB 계정은 auth · backend · worker 별로 최소 권한.
- backend → auth는 내부 API(`/internal/v1`, 게스트 세션 발급 INT-01 · 세션 폐기 INT-02 · HOST 세션 연장 INT-03)로만. `/internal/v1/*`은 Caddy가 프록시하지 않는다.
- 공개 경로 분기(7.1.4): `POST /api/v1/users`, `/api/v1/auth/*`, 정확히 `GET·PATCH /api/v1/me` → auth. `/api/v1/me/questions`, `POST /api/v1/me/consents`와 나머지 `/api/v1`, `/api/v1/ws` → backend.
- auth가 죽어도 이미 발급된 세션으로 강연 · 질문은 계속 동작해야 한다. RDS 확인 실패 시 인증 불허.
- 학습서버 호출은 정해진 API만 부르는 어댑터로. 임의 URL · 명령 · 파일 경로를 입력받지 않는다. `X-API-Key`는 backend · worker만 갖고 로그에 남기지 않는다. 주소는 `TRAINING_API_BASE_URL`(Docker bridge 호스트:9000 SSH 터널).

### API 공통 규칙 (7.1.2)
- Base `/api/v1`, JSON(교안 업로드만 multipart), 시각은 ISO 8601 UTC.
- 성공 `{request_id, data}`, 목록은 `data.items · next_cursor · total_count`(cursor, limit 기본 20 · 최대 100). 오류 `{request_id, error:{code, message, details, retryable}}`.
- 변경 요청: 인증 쿠키 + Origin 검사 + `X-CSRF-Token`. 가입 · 인증 메일 · 인증 확인 · 로그인 · 입장 · 재설정 · CSRF 재발급은 Origin 검사만.
- 비동기: `202 {resource_id, job_id, status}` + `Location: /api/v1/jobs/{job_id}`. 비동기 POST는 `Idempotency-Key` 필수(다른 본문이면 409 `IDEMPOTENCY_CONFLICT`).
- 강연 · 질문 상태 변경은 `If-Match`(version). 서빙 전환은 `expected_active_deployment_id`.
- 요청 한도는 가입 · 로그인 · 인증 메일 · 코드 확인 · 재설정 메일 · 입장 코드에만, 카운터는 RDS(`auth_rate_limits`). 질문 API에는 걸지 않는다.
- 응답에 내부 FK · 토큰 해시 · S3 키를 넣지 않는다. 개인 답변 · 필기 · 관리자 응답은 별도 스키마로 직렬화한다.
- 인증 쿠키 `easyq_session`: Secure · HttpOnly · SameSite=Lax · Path=/, DB에는 토큰 해시만.

### 비동기 작업과 실시간 (6.1.2)
- backend는 검증 후 업무 데이터와 `jobs` 행을 **한 트랜잭션**으로 저장하고 202.
- worker는 행 잠금으로 점유 + lease 갱신, 결과는 점유 표식이 일치할 때만 저장. 정해진 횟수 재시도 후 실패 상태 · 오류 코드.
- 질문 판정 · 발화 색인이 교안 처리 · 요약보다 우선. 작업 종류별 동시 실행 수 분리.
- 실시간 전달: worker가 결과 + `ws_events`를 함께 저장 → `NOTIFY`로 이벤트 번호만 → backend가 읽고 수신자 현재 권한 확인 후 전송. 알림 누락은 번호 이후 재조회로 보완. `answer.delta`는 임시 이벤트(확정 · 폐기 시 삭제).
- 음성 프레임은 메모리에서 STT로 중계만 한다. DB · 디스크 · S3 · 로그 · 이벤트에 저장하지 않는다.

### 질문 판정 (6.1.4 · 6.1.5)
- 상태: QUEUED → PROCESSING → AI_ANSWERED / ESCALATED → COMPLETED. 철회 시 ESCALATED.
- 검색은 항상 같은 강연 · READY 조각 안에서만. 벡터 20 + Kiwi 키워드 20을 RRF로 합쳐 상위 20. 모자라도 다른 강연 조각으로 채우지 않는다. 후보 0개면 이관.
- CUSTOM: 판정 추론(7.4.26) 최고 점수 ≥ τ면 답변, 미만이면 이관. 800 ms 초과 · 오류면 **그 질문만** LLM_ONLY. 관리자 고정 LLM_ONLY 동안은 전부 LLM_ONLY.
- Sonnet 답변 ≤ 4문장, 첫 출력에서 근거 부족이면 조각 버리고 이관. 인용 번호를 서버 코드로 대조해 유효 인용 ≥ 1일 때만 확정.
- `decision_snapshot`에 실제 적용된 값(8.2.36)을 남긴다. 서빙 설정 원본은 학습서버.
- 예산 hard cap 도달 시 LLM · 임베딩 호출을 접수하지 않고 질문은 `BUDGET_EXCEEDED`로 이관(STT · 메일 · 판정 추론은 제외).

### 동의 · 라벨 후보 · 보관 (7.1.3 · 6.2.11 · 8.2.37)
- 동의: SERVICE(필수) · TRAINING(선택) · STT(강연자, 강연마다). 가장 최근 기록 적용, 계정 TRAINING 철회가 강연 단위보다 우선.
- 라벨 후보는 **질문자와 강연자 모두 TRAINING 동의 + 평가용 강의 아님**일 때만 생성하고, 조회 · 검수 · 병합 때마다 다시 확인한다.
- 보관 기간과 파기 순서(접근 차단 → 작업 결과 저장 차단 → 후보 · 임시 이벤트 정리 → S3 삭제 → DB 행 · 벡터 · 색인 삭제 → 확인)는 설계서 표 그대로 따른다.

### DB (8.2)
- 식별자는 서버 발급 UUID, 시각은 TIMESTAMPTZ, 상태값은 CHECK 제약.
- 강연 소속 행은 항상 `session_id` 조건으로 접근하고, `(session_id, 참조 ID)` 복합 FK를 기본으로 한다. 예외는 8.2.35 목록에 있는 것만.
- 스키마 변경은 Alembic 마이그레이션으로만, `easyq_schema_v1.2.sql`과 어긋나면 먼저 알린다.

### 로그 · 오류 (8.3 · 8.4)
- UTF-8 JSON Lines, 공통 필드(`timestamp, level, service, event, request_id, trace_id, code, message, meta` …). 파일은 종류 · 날짜 · 인스턴스별.
- **로그에 남기지 않는 것**: 질문 · 답변 · 필기 · 교안 · 발화 원문, 비밀번호, 인증 코드 · 재설정 토큰과 검증값, 쿠키, Authorization, QR 토큰, 사전 서명 URL, 외부 API 키 · SMTP 자격 증명. 이메일 · IP · 사용자 ID는 HMAC 가명, 경로는 템플릿.
- 오류 코드는 8.4.2 표에 있는 것만 쓴다. HTTP 상태는 통신 결과, `error.code`는 업무 원인. 근거 부족 · 의견 질문 이관은 오류가 아니다. 비동기 작업 실패는 202를 바꾸지 않고 작업 조회(API-41)의 `status=FAILED, error_code`로.

## 명칭 (설계서 10.3)

Python 변수 · 함수 snake_case(함수는 동사로 시작), 클래스 PascalCase, 상수 · 환경 변수 UPPER_SNAKE_CASE, 파일 snake_case.py. API · DB · 로그에서 같은 대상은 설계서 6~8장의 같은 이름으로 부른다.

## Git

자세한 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md). 요약:

- `main`은 PR로만 병합한다. 직접 push 하지 않는다.
- 작업은 `dev`에서 `유형/짧은-설명` 브랜치(예: `feature/question-submit`, `fix/vote-duplicate`)를 따서 하고, `dev`로 PR을 연다.
- 커밋 메시지: `유형: 요약` — 유형은 `feat` · `fix` · `docs` · `refactor` · `test`.
  - 예: `feat: 질문 등록 API(API-27) 추가`, `fix: 공감 중복 저장 방지`
- PR 전에 Ruff와 pytest를 통과시킨다.
- 커밋 · push · PR 생성은 사용자가 요청할 때만 한다.

## 점검일 태그와 범위

출처: `구현 전도표/1팀-진도표v1.0(v6)-…hwpx`. 백엔드(신재우 · 조현준)가 맡은 항목만 적는다. 화면은 심주혜, 판정 모델 · 학습서버는 최의찬 · 엄윤종과 함께 맞춘다. 태그 시점까지 해당 항목의 API · 작업 · 테이블이 동작해야 한다.

### v0.3 — 12/2(수) · 1주차 · 진척 30%
- 공통: 회원가입 · 이메일 인증(신재우), 로그인 · 로그아웃 · 비밀번호 재설정(신재우), 음성 원본 미저장(신재우), 동의 수집 · 기록(조현준)
- 강연자: 강연 생성(조현준), 입장 QR 발급 · 재발급(신재우), 강연 상태 관리(조현준)
- 교안: 교안 업로드, 텍스트 추출 · 청킹, 임베딩 · 색인, 전문용어 사전, 교안 처리 상태 표시(조현준)
- 실시간 음성: 마이크 연결 · 음성 전송, 실시간 STT 중계, 발화 조각 색인(신재우)
- 내 강연 목록(조현준), 청중 입장 회원 / 게스트(신재우), 관리자 로그인(신재우)

### v0.4 — 12/3(목) · 2주차 · 진척 40%
- 질문 판정 · AI 답변: 하이브리드 검색, 대체 판정(LLM_ONLY), AI 답변 생성 · 전달, 2차 근거 확인, 인용 검증(신재우)
- 인식 상태 표시 · 일시정지(신재우)
- 질문 입력 · 유사 질문 확인, 질문 제출(조현준)
- 학습서버 의존: 판정 추론(7.4.26) · 서버 상태 확인(7.4.1)이 이때 학습서버에 준비됨(최의찬)

### v0.5 — 12/4(금) · 3주차 · 진척 50%
- 판정 모델 판정, 처리 기록(decision_snapshot), 판정 추론 연결(신재우)
- 개인 AI 답변 보기 · 평가, 공개 질문 목록, 공감(조현준 · 공개 목록은 신재우 함께)
- 관리자: 회원 관리(신재우), 강연 관리 · 강제 종료(조현준)

### v0.7 — 12/7(월) · 4주차 · 진척 70%
- 학습 후보 익명 처리, 라벨 후보 수집(조현준)
- 라이브 대시보드(조현준 · 신재우), 답변 완료 처리, AI 답변 확인 · 철회(조현준)
- 필기(조현준)
- 관리자: 운영 지표(신재우), 서빙 전환 · 롤백(신재우), 데이터셋 현황, 학습 결과 확인(조현준)
- 학습서버 의존: 평가용 교안 조각 목록(7.4.28) 제공 — 조현준이 서비스 쪽 수신 · 겹침 검사(6.1.6)

### v0.9 — 12/8(화) · 5주차 · 진척 90%
- 계정 관리 · 탈퇴, 보관 기간 후 자동 파기, 탈퇴 시 개인 기록 삭제(조현준)
- 요약 노트 생성(신재우), 요약 노트 열람(조현준)
- 지난 강연 조회 · 삭제, 강연 데이터 직접 삭제, 강연 리포트 · 통계(Should)(조현준)
- 내 보관함 목록 · 상세, 보관함 기록 삭제(조현준)
- 관리자: 라벨 검수, 재학습 실행 · 중단, 버전 비교 · 배포 판단, 공지 · 문의 관리(Should)(조현준)

### v1.0 — 12/9(수) · 6주차 · 진척 100%
- 통합 확인(전체): 강연자 · 청중 · 관리자 흐름 끝까지 확인, 버그 수정만. 새 기능은 넣지 않는다.

앞 태그 항목이 뒤 태그 항목의 선행 조건이다(예: v0.3 발화 색인 → v0.4 하이브리드 검색 → v0.5 판정 모델 판정). 범위를 당기거나 미뤄야 하면 먼저 알린다.
