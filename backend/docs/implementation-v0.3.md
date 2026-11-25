# EasyQ v0.3 백엔드 구현 설명서

## 1. 기준 자료와 범위

설계 기준은 `설계서/(CodingAnt팀)설계서_v1.0(엄윤종,심주혜,신재우,조현준,최의찬).hwp` 하나다. 기존 `docs`의 이전 설계 문서는 구현 기준에서 제외했다. HWP의 6.1~6.2, 7.1~7.3, 8.2를 읽고 원본 진도표의 **v0.3 열에 O가 있는 행** 중 신재우 또는 조현준이 담당자로 적힌 행을 선정했다.

DB 기준은 `DB 스키마파일/easyq_schema_v1.3.sql`(설계 검토 반영 작업본, 권한은 `easyq_grants_v1.3.sql`)이다. v1.2 원본은 복합 FK가 참조하는 UNIQUE 인덱스보다 먼저 만들어져 PostgreSQL에 적용되지 않는다. 학과 서버용 `easyq_training_registry_v1.2.sql`은 이번 서비스 서버의 DB 초기화에 적용하지 않는다. 두 SQL의 소유 서비스가 다르기 때문이다.

| v0.3 기능 | 담당자(백엔드) | 구현 위치 | 관련 API |
| --- | --- | --- | --- |
| 회원가입·이메일 인증 | 신재우 | service.register / confirm / queue_mail, worker.mail | API-01~03 |
| 로그인·로그아웃·비밀번호 재설정 | 신재우 | service.login / reset_password / auth_route | API-04~08 |
| 동의 수집·기록 | 조현준 | service.save_consent / effective / change_consent | API-11·20, 가입·입장 |
| 강연 생성 | 조현준 | service.create_lecture | API-13 |
| 입장 QR 발급·재발급 | 신재우 | service.code, join-links 분기 | API-13·17 |
| 강연 상태 관리 | 조현준 | service.patch_lecture / end, worker AUTO_END | API-15·16 |
| 교안 업로드 | 조현준 | service.upload, retrieval.validate_document | API-21 |
| 텍스트 추출·청킹 | 조현준 | retrieval.extract_pages / chunks | INGEST worker |
| 임베딩·색인 | 조현준 | worker.ingest, adapters.GeminiEmbedder, budget | INGEST worker |
| 청중 입장(회원·게스트) | 신재우 | service.join / join_lookup / internal | API-18~20, INT-01 |

API-06·09·15·18·22·41과 내부 세션 발급·폐기는 위 기능을 실행하고 검증하는 데 필요한 지원 API다. 진도표의 다른 버전 항목인 계정 수정·탈퇴, 질문·답변·공감·필기·보관함·관리자·음성 전송·전문용어 사전과 AI 담당자의 학습·판정 서버는 이번 구현에 포함하지 않는다.

## 2. 실행 구조와 코드 선택

| 파일 | 역할과 선택 이유 |
| --- | --- |
| `easyq/auth_app.py` | 계정 인증용 FastAPI 진입점. 업무 서버와 인증 라우팅을 분리한다. |
| `easyq/backend_app.py` | 강연·입장·동의·교안 접수용 FastAPI 진입점. |
| `easyq/http.py` | FastAPI에 마운트하는 ASGI 처리기. JSON·multipart 크기 제한, 공통 응답과 오류를 한 곳에서 처리하고 실제 사용 경로를 오프라인에서도 검증한다. DB·해시 처리는 `asyncio.to_thread`로 실행한다. |
| `easyq/service.py` | 인증·권한과 트랜잭션 단위 업무 처리. SQL 값은 파라미터로 바인딩하고 사용자 입력을 SQL 식별자로 사용하지 않는다. |
| `easyq/db.py` | psycopg의 명시적 트랜잭션·행 잠금·advisory lock·NOTIFY. 제공된 스키마와 직접 통신하기 위해 ORM의 자동 스키마 생성을 사용하지 않는다. |
| `easyq/security.py` | scrypt 비밀번호 해시, SHA-256 세션·CSRF 해시, HMAC 인증 코드·재설정 토큰, Fernet 발송용 암호문. |
| `easyq/worker.py` | DB 작업 점유, lease 갱신, 재시도, 교안 처리·메일·자동 종료·동의 철회 정리. 별도 메시지 브로커 없이 설계서의 jobs를 사용한다. |
| `easyq/retrieval.py` | PDF/PPTX 파싱·페이지 보존 청킹·Kiwi 키워드 정규화·768차원 검증·평가 자료 겹침 함수. 인증·UI와 분리해 AI 담당자가 같은 함수를 사용할 수 있다. |
| `easyq/adapters.py` | S3, Gemini, fastapi-mail, auth 내부 HTTP 호출. 실제 제공자 설정을 주입하고 테스트에서는 이 경계만 대역으로 바꾼다. |
| `easyq/budget.py` | MONTHLY_API_BUDGET 행을 잠근 뒤 호출 비용을 먼저 예약한다. 동시 호출로 상한을 넘기는 것을 줄인다. |
| `easyq/contracts.py` | 입력 검증·요청·응답·오류 타입. HTTP 처리와 업무 처리 사이의 계약이다. |

성공은 `{request_id,data}`, 실패는 `{request_id,error:{code,message,details,retryable}}`다. 204에는 본문이 없다. 모든 응답에 no-store와 요청 ID를 붙이고 예외의 SQL·비밀번호·서비스 키를 응답하지 않는다.

## 3. 구현한 API와 동작

모든 공개 경로 앞에 `/api/v1`이 붙는다. 변경 요청은 정확한 `Origin`을 검사한다. 인증된 변경에는 세션 쿠키와 CSRF를 검사하되, 설계서의 예외인 가입·인증·로그인·입장·재설정·CSRF 복구는 각 Origin 전용 규칙을 따른다.

| ID | Method·경로 | 입력 | 정상 응답 |
| --- | --- | --- | --- |
| 01 | POST `/users` | email, password, display_name, consents.SERVICE, 선택 TRAINING | 201 user_id, PENDING_VERIFICATION, resend_available_at |
| 02 | POST `/auth/email-verifications` | email | 202 계정 유무와 관계없는 안내 |
| 03 | POST `/auth/email-verifications/confirm` | email, 6자리 code | 200 email_verified=true, next_action=LOGIN |
| 04 | POST `/auth/sessions` | email, password | 201 User, expires_at, csrf_token + 쿠키 |
| 05 | DELETE `/auth/sessions/current` | 쿠키·CSRF | 204 + 쿠키 만료 |
| 06 | POST `/auth/csrf-tokens` | 쿠키, Origin, X-EasyQ-Client: web | 200 새 csrf_token |
| 07 | POST `/auth/password-reset-requests` | email | 202 빈 객체 |
| 08 | POST `/auth/password-resets` | token, new_password | 204 |
| 09 | GET `/me` | 회원 쿠키 | 200 User와 계정 동의 |
| 11 | POST `/me/consents` | purpose=SERVICE/TRAINING, policy_version, granted | 201 Consent, 필요 시 deletion_job_id |
| 13 | POST `/sessions` | title, scheduled_at, 선택 description·training_consent | 201 Session, join_code, join_url, ETag |
| 15 | GET `/sessions/{sid}` | 소유자 또는 참가자 쿠키 | 200 Session, ETag |
| 16 | PATCH `/sessions/{sid}` | If-Match, title·scheduled_at·description·status·stt_status 중 1개 이상 | 200 Session, 새 ETag |
| 17 | POST `/sessions/{sid}/join-links` | If-Match | 201 join_code, join_url, version |
| 18 | GET `/join-codes/{code}` | 5자리 코드 | 200 강연 미리보기·정원 |
| 19 | POST `/participations` | join_code, 게스트 nickname·service_consent, 선택 training_consent | 201 최초 / 200 기존 참가, 게스트 쿠키·CSRF |
| 20 | POST `/sessions/{sid}/consents` | purpose=SERVICE/TRAINING/STT, policy_version, granted | 201 Consent |
| 21 | POST `/sessions/{sid}/documents` | multipart file, UUID Idempotency-Key | 202 resource_id, job_id, status + Location |
| 22 | GET `/sessions/{sid}/documents` | 강연자 쿠키 | 200 문서 상태 목록 |
| 41 | GET `/jobs/{job_id}` | 작업 요청자 쿠키 | 200 작업 상태·안전한 오류 코드 |

입력 길이는 비밀번호 8~20자·영문과 숫자 포함, 표시명 1~50자, 강연명 1~100자, 설명 500자 이하, 게스트 닉네임 1~20자다. scheduled_at에는 시간대가 있어야 한다. 일반 회원의 role·is_eval_lecture·course_id 직접 지정 등 계약에 없는 필드는 422로 거부한다.

### 회원가입과 인증

1. `register`가 이메일을 소문자로 바꾸고 필수 SERVICE 동의를 확인한다. 선택 TRAINING 누락은 false다.
2. 이메일 단위 잠금 뒤 users·consents·email_verifications·EMAIL_VERIFICATION job을 하나의 트랜잭션에 저장한다. 중간 실패 시 가입 전체를 롤백한다.
3. 비밀번호는 salt가 있는 scrypt로 저장한다. 6자리 코드는 HMAC 검증값만 보관하고, worker 발송에 필요한 원문은 별도 키로 암호화한다. jobs.payload에는 코드를 넣지 않는다.
4. worker는 fastapi-mail로 발송하고 전달용 암호문을 지운다. SMTP 전달은 작업 접수와 별개다. SMTP 재시도로 메일이 중복 전달될 가능성은 있으며 인증 상태는 DB의 일회용 기록으로 보호한다.
5. `confirm`은 회원·인증 기록을 잠그고 10분 만료·일회용·5회 실패 한도를 검사한다. 성공하면 ACTIVE와 인증 시각을 함께 저장한다. 실패 횟수는 오류 응답에서도 커밋한다.
6. 재발송은 이전 기록·미발송 작업을 취소하고 새 기록을 만든다. 이메일 60초 간격·시간당 5회, IP 시간당 20회 제한을 DB에 기록한다. 24시간 미인증 가입과 만료 발송 암호문은 maintenance가 정리한다.

인증 완료로 자동 로그인하지 않는다. 기존 이메일로 다시 가입해도 비밀번호를 덮어쓰지 않는다.

### 로그인·로그아웃·재설정

`login`은 비밀번호를 먼저 확인한 뒤 ACTIVE·email_verified_at을 확인한다. 존재하지 않는 이메일도 해시 검증 비용을 수행하며 동일한 401을 반환한다. 올바른 비밀번호의 미인증·정지 계정은 설계서의 403을 반환한다. 회원 세션은 12시간 절대 만료다.

쿠키는 Secure·HttpOnly·SameSite=Lax·Path=/이며 세션 토큰은 JSON에 넣지 않는다. DB에는 token_hash와 csrf_token_hash만 들어간다. 로그인 실패 제한은 이메일 15분 10회·IP 15분 100회이고, 성공한 로그인은 실패 횟수를 늘리지 않는다.

로그아웃은 revoked_at과 session.revoked 이벤트를 함께 저장한다. 만료·폐기 쿠키의 반복 로그아웃도 동일 Origin에서 204다. CSRF 복구는 `X-EasyQ-Client: web`과 동일 Origin을 확인해 새 해시를 저장한다.

재설정 요청은 가입 여부를 노출하지 않는 202다. ACTIVE 회원만 30분짜리 HMAC 토큰과 암호화 발송 기록을 받는다. 재설정 성공은 비밀번호 교체·토큰 사용 처리·모든 인증 세션 폐기를 한 트랜잭션에서 수행한다. 이메일 인증 시각은 유지한다.

### 강연 생성·수정·시작·종료

`create_lecture`는 계정 SERVICE 동의를 확인하고 DRAFT, 예약 일시, 소유자 표시명 스냅샷, 서버 발급 course_id와 5자리 코드를 저장한다. scheduled_at은 표시용이며 자동 시작을 예약하지 않는다.

`patch_lecture`는 소유권과 `If-Match: "N"`을 확인한다. 정보 수정은 DRAFT에서만 가능하고 상태는 DRAFT → LIVE → ENDED다. **시작에 READY 교안은 필요하지 않다.** 시작과 함께 AUTO_END를 12시간 후로 예약한다.

종료는 end_reason=HOST/AUTO_TIMEOUT, ended_at, raw_data_expires_at=종료+90일과 STOPPED를 저장한다. 게스트 참가를 즉시 REVOKED로 바꾸고 auth 폐기 작업을 등록한다. auth 장애 중에도 backend가 현재 상태를 재확인하므로 게스트 접근은 즉시 거부된다.

STT의 RUNNING 요청은 HOST의 강연별 STT 동의가 있을 때 CONNECTING으로만 바꾼다. 실제 audio.start 승인·음성 중계는 다음 버전의 작업이다. FINALIZE 작업은 v0.3에서 종료 핸드오프를 기록한다. 진행 중 QA·마지막 STT 정리·요약 생성은 해당 기능이 들어오는 버전에서 연결해야 하며, 현재 생성되었다고 표시하지 않는다.

INT-03은 소유 LIVE 강연의 유효 회원 세션만 연장한다. 15분 간격을 검사하고 min(현재+12시간, 최초 발급+24시간)으로 제한한다. backend는 성공한 HOST/LIVE 응답 후 이를 요청하고 필요 시 같은 쿠키의 수명을 갱신한다. auth가 실패해도 아직 유효한 업무 요청 결과는 유지한다.

### 입장 QR·회원·게스트 입장

QR 이미지는 설계서대로 프런트엔드에서 join_url을 QR로 렌더링한다. 백엔드는 랜덤 5자리 코드와 `/join?code=...`를 발급한다. current·previous 코드 전체에서 충돌하지 않도록 할당 잠금을 사용한다. 재발급 시 버전을 올리고 직전 코드를 previous_join_code로 옮긴다. 직전 코드는 410이며 그보다 오래된 코드는 404일 수 있다. 기존 참가 세션은 유지한다.

`join_lookup`은 대소문자를 정규화하고 DRAFT/LIVE만 허용한다. 잘못되거나 폐기된 코드가 IP에서 15분 100회 누적되면(강의실은 여러 청중이 같은 IP를 쓴다) 15분 동안 입장을 차단한다. preview는 참가자를 생성하지 않는다.

`join`은 강연 행 잠금 안에서 활성 청중 50명 한도를 검사한다. 같은 회원의 기존 참가와 유효한 게스트 재입장은 200이며 정원과 무관하다. HOST 자신의 강연에는 청중 참가를 만들지 않는다. 회원은 계정 SERVICE 동의를 쓰며 게스트는 필수 동의를 별도로 기록한다.

다른 강연의 게스트 쿠키나 종료·폐기·만료된 게스트 쿠키는 입장 미리보기·입장에서 무시하고 새 게스트로 받는다. 무효인 회원 쿠키는 401로 재로그인을 안내한다. 강연 SERVICE 동의를 철회해 REVOKED가 된 회원은 `service_consent`를 다시 보내면 같은 참가 행을 ACTIVE로 되살린다(UNIQUE(session_id,user_id) 때문에 새 행을 만들지 않는다). 정원은 다시 검사한다.

HWP의 API-19 예시에 맞춰 게스트 동의의 true/false를 받는다. 버전은 서버의 SERVICE_POLICY_VERSION/TRAINING_POLICY_VERSION으로 기록한다. 명시적인 `{policy_version,granted}` 객체도 지원한다. 원문의 설명 표와 예시 타입이 불일치하는 부분을 이 방식으로 수용했다.

게스트 참가를 먼저 커밋한 후 INT-01로 auth에서 강연 전용 세션을 받는다. auth가 참가 기록을 별도 연결에서 읽을 수 있어야 하기 때문이다. 새 게스트 참가를 만들 때와 auth 실패(503) 때 서명한 `easyq_join_retry` HttpOnly 쿠키(참가 ID · 만료 시각 · HMAC, Path=/api/v1/participations, 입장 후 24시간)를 준다. 재요청은 게스트 세션 쿠키나 이 쿠키로 같은 참가자를 재사용한다. 정원은 회원 참가와, 유효한 게스트 세션이 있거나 입장 후 10분 이내인 게스트 참가만 센다. 선택적으로 UUID Idempotency-Key도 지원한다. 이 키는 참가자 ID로 그대로 쓰지 않고 HMAC(강연, 키)로 참가자 ID를 만든다. 다른 게스트의 participant_id를 알아도 그 참가를 이어받을 수 없다. 실패 응답의 쿠키까지 보존하지 못한 클라이언트에는 동일 참가 재사용을 보장할 수 없다.

INT-01은 게스트의 ACTIVE·DRAFT/LIVE·24시간 기한을 확인하고 기존 게스트 인증을 폐기한 후 새 쿠키를 발급한다. 내부 API는 서비스 키로 보호하고 Caddy 공개 경로에서 제외한다. worker 키는 INT-02 폐기에만 사용할 수 있다.

### 동의 이력

동의는 기존 행을 수정하는 대신 consents에 추가한다. 같은 주체·목적·범위의 최신 이력을 사용하며 강연별 이력과 계정 이력을 구분한다. 가입 때의 초기 TRAINING=false는 선택 거부로 취급하므로 이후 강연에서만 true를 고를 수 있다. 가입 이후 계정 수준에서 명시적으로 철회한 false는 강연별 true보다 우선한다.

계정 SERVICE 철회는 탈퇴 기능으로 안내하고 422를 반환한다. STT 동의는 HOST만 등록한다. 강연 SERVICE 철회는 참가자 권한을 폐기하거나 LIVE 강연을 종료하고, STT 철회는 STOPPED로 변경한다.

TRAINING 철회는 즉시 유효 동의에 반영하고 CLEANUP job을 만든다. worker는 해당 사용자의 질문과 HOST 강연에 연결된 미병합 후보를 삭제한다. MERGING 후보는 실제 병합 여부 확인이 필요하므로 `MERGE_RECONCILIATION_REQUIRED`로 보류한다. 학과 서버와의 병합 정합성 확인은 후보·학습 기능 버전의 연계 지점이다. 이미 원천 연결이 제거된 익명 데이터나 모델은 삭제하지 않는다.

### 교안 접수·텍스트 추출·청킹·색인

1. `upload`가 HOST와 DRAFT/LIVE를 확인하고 PDF/PPTX, MIME, 실제 시그니처, 50 MiB를 검증한다. PPTX는 엔트리 수·전체 해제 크기·CRC·중복 경로·매크로·암호화도 검사한다.
2. 사용자 파일명을 저장 경로로 사용하지 않는다. 원본은 `{session_id}/{document_id}` 키로 S3에 저장한다. DB 저장 실패 시 새 객체 삭제를 시도한다. 보상 삭제까지 실패한 객체는 버킷 lifecycle/운영 정리 대상이다.
3. documents와 INGEST job은 같은 트랜잭션이다. 같은 주체·강연·Idempotency-Key와 파일 지문은 기존 작업을 반환하고 다른 파일이면 409다. 202는 완료가 아니라 접수다.
4. worker가 jobs를 `FOR UPDATE SKIP LOCKED`로 점유하고 5분 lease와 고유 lease_token을 발급한다. 30초마다 연장하며 결과는 현재 lease_token이 일치할 때만 저장한다.
5. PDF는 pypdf, PPTX는 presentation 관계 순서를 따라 슬라이드 텍스트를 추출한다. 빈 스캔 PDF는 OCR 결과를 꾸미지 않고 DOCUMENT_NO_TEXT로 실패한다. 암호화·손상 파일도 안전한 오류 코드로 끝난다.
6. 페이지 번호를 유지해 문장·단어 경계를 우선하는 최대 800자, 100자 겹침 청크를 만든다. 이 규칙은 설계서의 미지정 세부값을 구현에서 고정한 것이며 retrieval_config_version에 포함한다.
7. Kiwi 명사·영어·숫자·용언 토큰을 정규화하고 PostgreSQL `to_tsvector('simple', ...)`로 색인한다. 임베딩은 설정된 Gemini 모델에서 받아 정확히 768차원·유한값·0이 아닌 벡터인지 검사한다. 설정 누락이나 잘못된 응답으로 READY를 만들지 않는다.
8. embedding·search_vector·retrieval_config_version·indexed_at과 READY를 저장한다. source_key의 UNIQUE와 UPSERT가 재실행 중복을 막는다. pgvector HNSW와 GIN 인덱스는 제공된 SQL을 그대로 사용한다.
9. API-22·41로 결과를 조회하고 document.updated를 ws_events에 저장한다. v0.3은 이벤트 생산까지 구현하며 WebSocket 소비·실시간 UI는 이후 버전에 연결한다. 전문용어 추출·사전 API도 v0.4 범위이므로 현재 단계는 INDEXING에서 끝난다.

임베딩 호출 직전 MONTHLY_API_BUDGET 행을 잠그고 `api_usage=RESERVED`를 기록한다. 고정한 최대 배치 예상 비용·가격 버전·환율이 없는데 hard cap이 켜져 있으면 호출하지 않고 BUDGET_EXCEEDED/HELD로 둔다. 성공·실패 모두 비용 기록을 유지하며 불확실한 실패는 예약액을 보수적으로 유지한다. 현재 어댑터의 비용은 실제 청구값이 아니라 설정된 상한 추정치다. 예산 설정 변경·상한 해제·다음 달에 보류 작업을 다시 접수한다.

평가용 교안 조각이 호환 버전으로 준비되어 있을 때만 5단어 묶음 겹침(청크 80%, 강연 30%)을 검사한다. 없거나 설정 버전이 다르면 PENDING으로 둔다. 학과 서버의 평가 자료 제공·동기화 기능은 v0.3 두 담당자 범위 밖이며, PENDING을 검사 완료로 취급하지 않는다.

## 4. PostgreSQL 연결과 스키마 대응

| 업무 | 실제 사용 테이블 |
| --- | --- |
| 계정 인증 | users, auth_sessions, email_verifications, password_resets, auth_rate_limits |
| 강연·참가·동의 | lecture_sessions, participants, consents |
| 교안·벡터·키워드 | documents, knowledge_chunks, eval_reference_chunks |
| 작업·알림·예산 | jobs, ws_events, api_usage, app_settings |
| 동의 철회 시 정리 | feedback_candidates, candidate_provenance, label_reviews |

`DATABASE_URL`은 프로세스별로 주입한다. backend는 인증 원본 테이블 대신 auth_session_view를 읽는다. 제공된 뷰에는 CSRF 검증에 필요한 csrf_token_hash가 빠져 있어 `migrations/001_auth_session_view.sql`로 기존 뷰 끝에 컬럼 하나를 추가한다. 원본 SQL을 수정하거나 기존 테이블을 재생성하지 않는다. 역할별 최소 권한 예시는 `002_runtime_grants.example.sql`이다.

## 5. 검증 결과와 실제 연결의 경계

개별 테스트 이름·성공·스킵 수·실행 시각은 `test-results.json`이 최신 근거다. `scripts/run_tests.py`로 다시 생성한다.

이번 환경에서 HTTP/ASGI 디코더와 실제 service·worker 코드, SQL 트랜잭션을 연결한 오프라인 테스트를 수행했다. SQLite는 제공 SQL의 테이블을 읽어 타입·PostgreSQL 잠금 문법만 조정한 테스트 전용 하네스다. 운영 코드에는 SQLite 우회가 없다. 메일·임베딩·스토리지 외부 경계는 대역을 사용했다. Windows의 로컬 socketpair 제한 때문에 ASGI 테스트는 네트워크 리스닝 없이 메시지를 전달하고, 실행기만 인라인으로 교체했다.

실제 PostgreSQL 테스트는 같은 API 테스트를 원본 SQL과 뷰 migration을 수정 없이 적용한 임의 스키마에서 수행하도록 별도 작성했다. `EASYQ_TEST_DATABASE_URL`이 없으면 명시적으로 skip한다. 이번 환경에서는 PostgreSQL·Docker·psycopg·FastAPI 설치가 없고 패키지 저장소 DNS 접속이 실패해 **실제 PostgreSQL/pgvector, FastAPI/Uvicorn 기동, SMTP·S3·Gemini 연결, Kiwi 실제 분석은 검증하지 못했다.** 이를 성공으로 계산하지 않았다.

특히 PostgreSQL의 FK/CHECK/HNSW·GIN, 동시 트랜잭션 경합·SKIP LOCKED의 실제 동작은 SQLite 결과로 보장하지 않는다. DB 생성 후 PostgreSQL 통합 테스트와 실제 제공자 smoke 확인이 남아 있다. 이 작업에서는 사용자 DB를 생성하거나 외부 서비스를 배포하지 않았다.

## 6. 다음 버전과 연결할 지점

| 지점 | 현재 동작 | 후속 연결 |
| --- | --- | --- |
| session.updated/document.updated/session.revoked | DB 이벤트와 NOTIFY 생성 | WS LISTEN 소비·권한별 전달·폐기 |
| STT 시작 | 동의 확인 후 CONNECTING | audio.start 승인·RUNNING·메모리 음성 중계 |
| FINALIZE | v0.3 종료 핸드오프 기록 | 진행 QA·마지막 STT 정리·게스트 원문 정리·SUMMARY |
| 전문용어 | 키워드 색인만 | 자동 용어 추출·사전 편집·STT 힌트 |
| 평가 자료 | 호환 자료 없으면 PENDING | 학과 API 7.4.28 동기화 |
| MERGING 후보 철회 | HELD, 원천을 임의 삭제하지 않음 | 학과 실제 병합 상태 조회 후 정리 |
| 개인정보 90일 파기·탈퇴 | 종료 기한 기록 | 진도표 후속 버전의 PURGE·삭제 요청 |

API-10 계정 수정, API-14 내 강연 목록, API-23 교안 수동 재시도, API-24 삭제와 후속 기능은 이번 v0.3의 두 담당자 행에 포함되지 않아 공개 구현 범위를 늘리지 않았다. 내부 자동 재시도는 워커 신뢰성을 위한 동작으로 포함했다.
