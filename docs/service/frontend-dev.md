# 프론트엔드 연동용 로컬 개발 서버

프론트(Next.js)가 auth · backend를 실제로 붙여 보기 위한 로컬 구성이다. 배포용이 아니다.

## 1. 실행

Docker Desktop이 필요하다. 레포 루트에서 실행한다.

```powershell
docker compose up --build      # 처음 (라이브러리만 담은 이미지를 빌드)
docker compose up              # 이후 (코드는 바인드 마운트라 고치면 바로 반영)
docker compose down -v         # DB까지 지우고 처음부터
```

| 서비스 | 주소 | 내용 |
|---|---|---|
| Caddy | http://localhost:8080 | **브라우저는 여기로 접속**(`PUBLIC_ORIGIN`). `/api/v1/*` → auth · backend, 나머지 → 호스트의 Next.js 개발 서버(:3000) |
| backend | http://localhost:8000/docs | REST · WebSocket Swagger(직접 확인용) |
| auth | http://localhost:8001/docs | 가입 · 로그인 · 재설정 · `/me` Swagger |
| Mailpit | http://localhost:8025 | 가입 인증 코드 · 재설정 메일(실제로 발송되지 않음) |
| PostgreSQL | localhost:15432 | DB `easyq`, 소유자 `easyq_owner` / `easyq_owner_dev` |

- 처음 뜰 때 v1.2 스키마 · 인증 뷰 · 실행 계정(auth · backend · worker) · 권한을 자동으로 만든다. 처음 띄우다 실패한 적이 있으면 `docker compose down -v`부터 실행한다(반쯤 만든 DB는 초기화를 건너뛴다).
- 포트가 이미 쓰이고 있으면 `.env`에 `HTTP_PORT` · `BACKEND_PORT` · `AUTH_PORT` · `DB_PORT`를 바꿔 넣는다. `HTTP_PORT`를 바꾸면 `PUBLIC_ORIGIN=http://localhost:<포트>`도 같이 바꾼다.
- 교안 임베딩 · 음성 STT는 키가 있어야 동작한다(없으면 `INDEX_BUILD_FAILED` · `STT_UNAVAILABLE`). 레포 루트 `.env`에 `EMBEDDING_URL` · `EMBEDDING_API_KEY` · `EMBEDDING_MODEL` · `STT_MODEL` · `STT_API_KEY`를 넣는다.

## 2. 프론트 쪽 설정

백엔드는 설계서대로 교차 출처(CORS)를 허용하지 않고 `Origin`이 `PUBLIC_ORIGIN`과 같아야 받는다. **Next.js 개발 서버(:3000)를 띄우고 브라우저는 http://localhost:8080 으로 접속**하면 Caddy가 한 출처로 묶어 주므로 rewrites가 필요 없다. WebSocket도 `ws://localhost:8080/api/v1/ws/sessions/{sid}`로 연결한다.

## 3. 테스트 계정

`seed` 서비스가 처음 실행 때 한 번 만든다(이미 있으면 그대로 둔다). 메일 인증 없이 바로 로그인된다.

| 종류 | 이메일 | 비밀번호 |
|---|---|---|
| 회원(이메일 인증 완료, SERVICE 동의) | member@easyq.local | easyq2026 |
| 관리자(ADMIN) | admin@easyq.local | admin2026 |

- 로컬 개발 DB 전용 값이다. `PUBLIC_ORIGIN`이 localhost가 아니거나 `EASYQ_DEV_SEED=1`이 없으면 시드는 실행을 거부한다.
- 이 두 계정은 로그인만 된다. `.local` 주소라 메일 라이브러리가 받는 주소로 거부해서, 비밀번호 재설정 메일은 가지 않는다.
- 새로 가입해서 시험할 때는 `@example.com` 주소를 쓴다. `.local` · `.test` · `.localhost` 주소는 가입 단계에서 422 `VALIDATION_ERROR`로 거부된다(메일을 보낼 수 없는 주소라서). 보낸 메일은 Mailpit(http://localhost:8025)에서 본다.
- 운영 관리자 계정은 서버에서 `python -m easyq.admin create --email … --display-name …`로만 만든다.

## 4. 요청 규칙 요약 (7.1.2)

- 변경 요청은 `Origin` 헤더가 필요하다. 로그인 후 변경 요청은 `X-CSRF-Token`도 보낸다(로그인 · 입장 응답의 `csrf_token`).
- CSRF 실패는 `403 FORBIDDEN` + `details.reason = "CSRF"`다. 이 경우만 API-06으로 토큰을 다시 받아 원래 요청을 한 번 재시도한다.
- 새로고침으로 CSRF 토큰을 잃으면 `POST /api/v1/auth/csrf-tokens`(헤더 `X-EasyQ-Client: web`)로 다시 받는다.
- 성공은 `{request_id, data}`, 실패는 `{request_id, error:{code, message, details, retryable}}`.
- 요청 · 응답 필드: Swagger(`/docs`)에 인증 API-01~09와 입장 API-18 · 19부터 들어 있다. 나머지는 `backend/docs/api-v0.3.md`.
