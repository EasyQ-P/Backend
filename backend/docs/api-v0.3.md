# EasyQ 백엔드 구현 API 정리 (v0.3 프로토타입)

- 기준: `backend/easyq/service.py`의 `route()` · `auth_route()` · `internal()` (2026-11-24)
- 범위: 공개 API 20개, auth 내부 API 3개, 헬스 체크 2개. 설계서 7.2의 65개 중 20개를 구현했다.

## 공통 동작

- 모든 공개 경로 앞에 `/api/v1`이 붙는다.
- 성공 응답은 `{request_id, data}`, 실패 응답은 `{request_id, error:{code, message, details, retryable}}`이다. 204는 본문이 없다.
- 변경 요청(GET 외)은 모두 `Origin`을 검사한다. 로그인이 필요한 변경 요청은 `X-CSRF-Token`도 검사한다.
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

## backend 서버 — 공개 API (11개)

| ID | 메서드 · 경로 | 권한 | 하는 일 | 주요 응답 · 오류 |
|---|---|---|---|---|
| API-11 | POST `/me/consents` | 회원 | 계정 단위 SERVICE · TRAINING 동의 변경. TRAINING 철회 시 CLEANUP 작업 등록 | 201 · 422(SERVICE 철회는 탈퇴로 안내) |
| API-13 | POST `/sessions` | 회원 | 강연 생성(DRAFT). 5자리 입장 코드와 `join_url` 발급 | 201 + ETag |
| API-15 | GET `/sessions/{sid}` | 강연자 · 참가자 | 강연 상태, 내 역할, 참가자 수, 내 동의 상태 | 200 + ETag · 404 |
| API-16 | PATCH `/sessions/{sid}` | 강연자 | 정보 수정(DRAFT에서만), 시작(LIVE, 12시간 자동 종료 예약), 종료(ENDED, 90일 보관 기한 설정), STT 상태 변경(동의 확인 후 CONNECTING). `If-Match` 필요 | 200 · 409 `VERSION_CONFLICT` / `SESSION_STATE_CONFLICT` · 403 `CONSENT_REQUIRED` |
| API-17 | POST `/sessions/{sid}/join-links` | 강연자 | 입장 QR 재발급. 바로 전 코드는 410 처리. `If-Match` 필요 | 201 · 409 |
| API-18 | GET `/join-codes/{code}` | 누구나 | 입장 전 강연 미리보기, 정원 여부 | 200 · 404 `JOIN_CODE_INVALID` · 410 `JOIN_CODE_REVOKED` · 409 `SESSION_ENDED` · 429 |
| API-19 | POST `/participations` | 회원 · 게스트 | 청중 입장(정원 50명). 게스트는 INT-01로 강연 전용 세션 발급. 다시 입장하면 200 | 201/200 · 409 `SESSION_FULL` · 403 `CONSENT_REQUIRED` · 503 `AUTH_SERVICE_UNAVAILABLE` |
| API-20 | POST `/sessions/{sid}/consents` | 강연자 · 참가자 | 강연 단위 SERVICE · TRAINING · STT(강연자만) 동의 변경. 철회하면 참가 폐기 · 강연 종료 · STT 중지 | 201 |
| API-21 | POST `/sessions/{sid}/documents` | 강연자 | 교안 업로드(PDF/PPTX, 50MiB, `Idempotency-Key` 필수). INGEST 작업 등록 | 202 + Location · 413 `FILE_TOO_LARGE` · 415 `FILE_TYPE_UNSUPPORTED` · 409 `IDEMPOTENCY_CONFLICT` |
| API-22 | GET `/sessions/{sid}/documents` | 강연자 | 교안별 처리 상태 · 단계 · 쪽수 · 조각 수 · 오류 코드 | 200(목록) |
| API-41 | GET `/jobs/{job_id}` | 작업을 요청한 회원 | 비동기 작업 상태와 오류 코드 | 200 · 404 |

### 입장(API-19) 세부 동작

- 다른 강연의 게스트 쿠키나 종료 · 폐기 · 만료된 게스트 쿠키는 무시하고 새 게스트로 받는다. 무효인 회원 쿠키는 401로 다시 로그인하도록 안내한다.
- 게스트 재입장 · 재시도는 게스트 세션 쿠키, 서명된 `easyq_join_retry` 쿠키(새 참가 · 503 때 발급, 입장 후 24시간), `Idempotency-Key` 순으로 같은 참가자를 찾는다. `Idempotency-Key`는 참가자 ID로 그대로 쓰지 않고 HMAC으로 변환해 쓴다.
- 강연 SERVICE 동의를 철회한 회원이 다시 들어오려면 `service_consent: true`를 보내야 한다. 기존 참가 기록을 다시 활성화하고 정원을 다시 확인한다.

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
| `INGEST` | 교안 추출 → 청킹 → 임베딩 · 키워드 색인 → READY, 평가용 강의 검사(목록이 없으면 PENDING) |
| `EMAIL_VERIFICATION` · `PASSWORD_RESET` | 인증 코드 · 재설정 링크 메일 발송 |
| `AUTO_END` | LIVE 시작 12시간 뒤 자동 종료 |
| `AUTH_REVOKE` | 게스트 세션 폐기(INT-02 호출) |
| `CLEANUP` | TRAINING 동의 철회 시 미병합 라벨 후보 정리 |
| `FINALIZE` | 종료 처리를 다음 작업으로 넘긴다는 기록만 남김 |

## 아직 없는 것과 주의할 점

- **v0.3 범위인데 아직 없는 API:** API-14(내 강연 목록), API-23(교안 재처리), 전문용어 사전 API, 음성 · STT WebSocket(7.3.10), 관리자 권한 확인
- **WebSocket 전달 미구현:** `ws_events`에 이벤트를 저장하고 NOTIFY까지는 하지만, WebSocket으로 보내는 부분은 없다.
- **오류 코드가 설계서와 다름:** 교안 처리 실패 코드는 설계서 8.4.2의 `DOCUMENT_PARSE_FAILED` / `INDEX_BUILD_FAILED` 대신 `DOCUMENT_INVALID` / `DOCUMENT_NO_TEXT` 등을 쓰고 있어 맞춰야 한다.
- **검증 범위:** 오프라인 테스트(SQLite 하네스) 48개로만 확인했다. 실제 PostgreSQL · FastAPI 기동 · 외부 연동(SMTP · S3 · Gemini)은 아직 검증하지 않았다.
