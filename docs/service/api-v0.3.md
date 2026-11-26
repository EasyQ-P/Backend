# EasyQ 백엔드 구현 API 정리 (v0.3 프로토타입)

- 기준: `backend/easyq/service.py`(REST) · `backend/easyq/live.py`(WebSocket) (2026-11-25, 설계서 v1.2 반영본 기준)
- 범위: 공개 REST API 23개, WebSocket 1개, auth 내부 API 3개, 헬스 체크 2개. 설계서 7.2의 65개 중 23개를 구현했다.

## 공통 동작

- 요청 · 응답 필드는 Swagger(`/docs`)에 인증 API-01~09와 입장 API-18 · 19부터 Pydantic 모델로 들어 있다(`easyq/schemas.py`). 로컬 개발 서버는 `frontend-dev.md` 참고.

- 모든 공개 경로 앞에 `/api/v1`이 붙는다.
- 성공 응답은 `{request_id, data}`, 실패 응답은 `{request_id, error:{code, message, details, retryable}}`이다. 204는 본문이 없다.
- 변경 요청(GET 외)은 모두 `Origin`을 검사한다. 로그인이 필요한 변경 요청은 `X-CSRF-Token`도 검사한다.
  - CSRF 토큰이 없거나 틀리면 `403 FORBIDDEN`에 `details: {"reason": "CSRF"}`가 붙는다. 이때 클라이언트는 API-06으로 토큰을 다시 받아 한 번만 재시도한다(다른 FORBIDDEN에는 reason이 없다).
  - 예외: 가입 · 인증 · 로그인 · 입장 · 재설정은 Origin 검사만 한다.
- 인증 쿠키는 `easyq_session`(Secure · HttpOnly · SameSite=Lax · Path=/)이다. DB에는 토큰 해시만 저장한다.
- 모든 응답에 `Cache-Control: no-store`와 `X-Request-ID`를 붙인다.

## auth 서버 — 공개 API (9개)

| ID | 메서드 · 경로 | 권한 | 하는 일 | 주요 응답 · 오류 |
|---|---|---|---|---|
| API-01 | POST `/users` | 누구나 | 회원가입. SERVICE 동의 필수, TRAINING 동의 선택. 인증 메일 작업 등록 | 201 `PENDING_VERIFICATION` · 409 `EMAIL_ALREADY_EXISTS` · 422 `CONSENT_REQUIRED` · 429 |
| API-02 | POST `/auth/email-verifications` | 누구나 | 인증 메일 재발송. 계정이 있는지 여부는 알려주지 않음 | 202 · 429 |
| API-03 | POST `/auth/email-verifications/confirm` | 누구나 | 6자리 코드 확인. 만료 10분, 5회 실패 시 잠금 | 200 `next_action=LOGIN` · 422 `EMAIL_VERIFICATION_INVALID` · 429 `EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED` |
| API-04 | POST `/auth/sessions` | 누구나 | 로그인. 쿠키와 CSRF 토큰 발급(12시간) | 201 · 401 `AUTH_INVALID_CREDENTIALS` · 403 `EMAIL_NOT_VERIFIED` / `ACCOUNT_SUSPENDED` · 429 |
| API-05 | DELETE `/auth/sessions/current` | 로그인 | 로그아웃. 이미 만료된 세션이어도 204 | 204 |
| API-06 | POST `/auth/csrf-tokens` | 로그인 | 새로고침 후 CSRF 토큰 재발급(`X-EasyQ-Client: web` 필요) | 200 |
| API-07 | POST `/auth/password-reset-requests` | 누구나 | 재설정 메일 요청(토큰 유효 30분). 계정이 있는지 여부는 알려주지 않음 | 202 |
| API-08 | POST `/auth/password-resets` | 누구나 | 비밀번호 재설정 후 그 계정의 모든 세션 폐기 | 204 · 422 `RESET_TOKEN_INVALID` |
| API-09 | GET `/me` | 회원 | 내 정보와 계정 동의 상태 | 200 |

## backend 서버 — 공개 API (14개)

| ID | 메서드 · 경로 | 권한 | 하는 일 | 주요 응답 · 오류 |
|---|---|---|---|---|
| API-11 | POST `/me/consents` | 회원 | 계정 단위 SERVICE · TRAINING 동의 변경. TRAINING 철회 시 CLEANUP 작업 등록 | 201 · 422(SERVICE 철회는 탈퇴로 안내) |
| API-14 | GET `/sessions?scope=hosted\|joined` | 회원 | 내 강연(hosted, `status`로 DRAFT · LIVE · ENDED 탭) · 보관함 강연(joined, `q`로 강연명 · 강연자 검색, 항목마다 `my_question_count`). cursor · limit(기본 20, 최대 100) | 200(목록) · 422 |
| API-13 | POST `/sessions` | 회원 | 강연 생성(DRAFT). 5자리 입장 코드와 `join_url` 발급 | 201 + ETag |
| API-15 | GET `/sessions/{sid}` | 강연자 · 참가자 | 강연 상태, 내 역할, 참가자 수, 내 동의 상태. 강연자에게만 `join_code` · `join_url` · `terms` | 200 + ETag · 404 |
| API-16 | PATCH `/sessions/{sid}` | 강연자 | 정보 수정(DRAFT에서만), 시작(LIVE, 12시간 자동 종료 예약), 종료(ENDED, 90일 보관 기한 설정), STT 상태 변경(동의 확인 후 CONNECTING). `If-Match` 필요 | 200 · 409 `VERSION_CONFLICT` / `SESSION_STATE_CONFLICT` · 403 `CONSENT_REQUIRED` |
| API-17 | POST `/sessions/{sid}/join-links` | 강연자 | 입장 QR 재발급. 바로 전 코드는 410 처리. `If-Match` 필요 | 201 · 409 |
| API-18 | GET `/join-codes/{code}` | 누구나 | 입장 전 강연 미리보기, 정원 여부 | 200 · 404 `JOIN_CODE_INVALID` · 410 `JOIN_CODE_REVOKED` · 409 `SESSION_ENDED` · 429 |
| API-19 | POST `/participations` | 회원 · 게스트 | 청중 입장(정원 50명). 게스트는 INT-01로 강연 전용 세션 발급. 다시 입장하면 200. 응답 Participation: participant_id · session_id · kind · nickname(회원은 display_name) · session_status · expires_at · csrf_token(게스트 첫 입장) | 201/200 · 409 `SESSION_FULL` · 403 `CONSENT_REQUIRED` · 503 `AUTH_SERVICE_UNAVAILABLE` |
| API-20 | POST `/sessions/{sid}/consents` | 강연자 · 참가자 | 강연 단위 SERVICE · TRAINING · STT(강연자만) 동의 변경. 철회하면 참가 폐기 · 강연 종료 · STT 중지 | 201 |
| API-21 | POST `/sessions/{sid}/documents` | 강연자 | 교안 업로드(PDF/PPTX, 50MiB, `Idempotency-Key` 필수). INGEST 작업 등록 | 202 + Location · 413 `FILE_TOO_LARGE` · 415 `FILE_TYPE_UNSUPPORTED` · 409 `IDEMPOTENCY_CONFLICT` |
| API-22 | GET `/sessions/{sid}/documents` | 강연자 | 교안별 처리 상태 · 단계 · 쪽수 · 조각 수 · 오류 코드 | 200(목록) |
| API-23 | POST `/sessions/{sid}/documents/{did}/retries` | 강연자 | FAILED 교안을 같은 원본으로 처음부터 다시 처리(이전 조각 삭제, `Idempotency-Key` 필수). DRAFT · LIVE | 202 · 404 · 409 `DOCUMENT_NOT_RETRYABLE` / `SESSION_STATE_CONFLICT` |
| API-25 | PUT `/sessions/{sid}/terms` | 강연자 | 전문용어 사전 전체 교체(DRAFT에서만, `If-Match`). 앞뒤 공백 정리, 대소문자 무시 중복 제거, 최대 100개 · 각 1~40자. 지운 용어는 기억해 교안 추출로 다시 넣지 않음 | 200 `{terms, version}` · 409 `SESSION_STATE_CONFLICT` / `VERSION_CONFLICT` · 422 |
| API-41 | GET `/jobs/{job_id}` | 작업을 요청한 회원 | 비동기 작업 상태 · 오류 코드 · 결과(건수만) | 200 · 404 |

### 입장(API-19) 세부 동작

- 다른 강연의 게스트 쿠키나 종료 · 폐기 · 만료된 게스트 쿠키는 무시하고 새 게스트로 받는다. 무효인 회원 쿠키는 401로 다시 로그인하도록 안내한다.
- 게스트 재시도는 `Idempotency-Key`, 또는 auth 장애(503) 때 함께 내려 주는 서명된 `easyq_join_retry` 쿠키(10분)로 같은 참가자를 찾는다. `Idempotency-Key`는 참가자 ID로 그대로 쓰지 않고 HMAC으로 변환해 쓴다.
- 정원은 활성(ACTIVE) 청중 수 50명이다(강연자 제외). 잘못된 입장 코드는 같은 IP에서 15분 20회를 넘으면 15분 동안 거부한다.
- 강연 SERVICE 동의를 철회한 회원이 다시 들어오려면 `service_consent: true`를 보내야 한다. 기존 참가 기록을 다시 활성화하고 정원을 다시 확인한다.

## WebSocket — `/api/v1/ws/sessions/{sid}` (7.3.10)

- 연결: 같은 사이트 HttpOnly 쿠키, Origin, 강연 권한(강연자 또는 활성 참가자)을 확인하고 받는다. 실패하면 1008로 닫는다. 연결되면 `connection.ready`(역할, 참가자 ID, 강연 version).
- 메시지 형식: `{type, event_id, session_id, request_id, resource_version, data}`.
- 이벤트 전달: `ws_events`를 한 프로세스에서 1초마다 마지막 event_id 이후로 읽어 받는 사람 권한에 맞게 보낸다. `document.updated` · `stt.status` · `transcript.final`은 강연자만, `session.updated`는 모두, `auth.csrf_rotated`는 같은 인증 세션만, `session.revoked`면 연결을 1008로 닫는다. 30초마다 인증 · 참가 상태를 다시 확인한다.
- 연결 유지: 서버가 20초마다 `ping`, 60초 동안 아무 메시지가 없으면 종료. 클라이언트 `ping`에는 `pong`.
- 음성(강연자만, 6.2.5):
  - `audio.start {stream_id, sample_rate:16000, channels:1}` → LIVE · 이 강연 STT 동의 · `stt_status`가 CONNECTING/RUNNING/DEGRADED · 강연당 활성 스트림 1개를 확인하고 `stt_status=RUNNING`, `audio.accepted {stream_id, next_sequence}`.
  - 바이너리 프레임: 헤더(stream UUID 16바이트 + sequence uint32 + start_ms uint64, network byte order) + 16 kHz mono PCM16. 32 KiB 초과는 1009로 종료. 같은 sequence 이하는 버린다. 10프레임마다 `audio.ack`.
  - 음성은 메모리 버퍼(5초)를 거쳐 STT로만 중계하고 DB · 디스크 · S3 · 로그 · 이벤트에 저장하지 않는다. 넘치거나 STT가 실패하면 `stt.status {status: DEGRADED, error_code: STT_UNAVAILABLE}`.
  - STT 연결마다 강연 전문용어 사전을 인식 힌트로 넣는다. 10분 연결 제한 전에 다시 연다.
  - 확정 발화는 강연자에게 `transcript.final`로 보내고, 약 30초씩 묶어 `knowledge_chunks`(TRANSCRIPT, PENDING)와 `TRANSCRIPT_INDEX` 작업으로 저장한다. 시각은 강연 시작 기준 ms, `source_key = stream_id:start_ms`라 재연결해도 중복 저장되지 않는다.
  - `audio.stop` → 남은 발화를 확정 · 저장한 뒤 `stt_status=PAUSED`. 연결이 끊기면 남은 발화를 저장하고 `DEGRADED`, 같은 stream_id로 다시 `audio.start`하면 이어 받는다. API-16 일시정지 · 동의 철회 · 강연 종료는 5초 안에(`session.updated`가 오면 즉시) 스트림을 멈춘다. STT 동의를 철회한 뒤에는 남은 발화도 저장하지 않는다.
  - STT 제공자가 실패하면 `lecture_sessions.stt_status`를 한 번 `DEGRADED`로 바꾸고(이벤트도 한 번), 복구되면 `RUNNING`으로 되돌린다. 재연결은 2초 간격으로 시도한다.

## auth 내부 API (`/internal/v1`)

서비스 키(`X-Service-Key`)가 필요하다. Caddy가 외부로 프록시하지 않는다.

| ID | 경로 | 하는 일 |
|---|---|---|
| INT-01 | POST `/guest-sessions` | 게스트 세션 발급. 같은 게스트의 기존 세션은 폐기 |
| INT-02 | POST `/session-revocations` | 회원 · 게스트 세션 폐기. worker 키로는 이 API만 호출 가능 |
| INT-03 | POST `/host-session-renewals` | 진행 중(LIVE) 강연의 강연자 세션 연장. 15분 간격, 최대 24시간 |

## 헬스 체크

| 경로 | 하는 일 |
|---|---|
| GET `/health/live` | 프로세스 생존 확인(DB 확인 없음) |
| GET `/health/ready` | DB 연결까지 확인 |

## worker 작업 (`jobs`)

| 작업 | 하는 일 |
|---|---|
| `TRANSCRIPT_INDEX` | 30초 발화 조각 임베딩 · 키워드 색인 → READY, `stt.status`(indexed_until_ms · ingest_lag_ms). 교안 처리보다 먼저 점유 |
| `INGEST` | 교안 추출 → 청킹 → 임베딩 · 키워드 색인 → 용어 추출(Kiwi 규칙, 문서당 최대 30개를 사전 빈자리에 추가) → READY, 평가용 강의 검사(목록이 없으면 PENDING) |
| `EMAIL_VERIFICATION` · `PASSWORD_RESET` | 인증 코드 · 재설정 링크 메일 발송 |
| `AUTO_END` | LIVE 시작 12시간 뒤 자동 종료 |
| `AUTH_REVOKE` | 게스트 세션 폐기(INT-02 호출) |
| `CLEANUP` | TRAINING 동의 철회 시 미병합 라벨 후보 정리 |
| `FINALIZE` | 종료 처리를 다음 작업으로 넘긴다는 기록만 남김 |

## 아직 없는 것과 주의할 점

- **관리자:** ADMIN은 서버 명령 `python -m easyq.admin create --email … --display-name …`(비밀번호는 `EASYQ_ADMIN_PASSWORD` 또는 입력)으로만 만들고 API-04로 로그인한다. 관리자 권한 확인(`require_admin`)은 있고, 관리자 API는 v0.5부터다.
- **오류 코드:** 작업 실패는 8.4.2 코드로 저장한다(교안 원본 문제 `DOCUMENT_PARSE_FAILED`, 색인 최종 실패 `INDEX_BUILD_FAILED`, 메일 `EMAIL_DELIVERY_FAILED`, 예산 `BUDGET_EXCEEDED`). 분류되지 않은 서버 오류는 503 `SERVICE_UNAVAILABLE`.
- **청킹:** 아직 800자 · 100자 겹침이다. AI 레포 `retrieval` 태그가 나오면 AI팀 규칙(400토큰 등)으로 바꾼다.
- **STT 실제 연결:** google-genai Live API 어댑터는 실제 제공자로 검증하지 않았다(테스트는 가짜 STT). `STT_MODEL` · `STT_API_KEY`가 없으면 `audio.start`가 `STT_UNAVAILABLE`.
- **활성 스트림 관리**는 backend 프로세스 1개를 전제로 한다(compose 구성과 같음).
- **기준 설계:** 백엔드 v1.2 반영 설계서와 `easyq_schema_v1.2.sql`. v1.3 작업본은 팀 회의에서 반려되어 따르지 않는다.
- **검증 범위:** 오프라인 테스트(SQLite 하네스) 72개와 실제 PostgreSQL 16 + pgvector 테스트 74개로 확인했다. FastAPI 기동 · 외부 연동(SMTP · S3 · Gemini)은 아직 검증하지 않았다.
