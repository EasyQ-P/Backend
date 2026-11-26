# 프론트엔드 연동용 로컬 개발 서버

프론트(Next.js)가 auth · backend를 실제로 붙여 보기 위한 로컬 구성이다. 배포용이 아니다.

## 1. 실행

Docker Desktop이 필요하다.

```powershell
cd backend
docker compose -f compose.dev.yaml up --build
```

| 서비스 | 주소 | 내용 |
|---|---|---|
| backend | http://localhost:8000 | REST · WebSocket(`/api/v1/ws/sessions/{sid}`), Swagger `http://localhost:8000/docs` |
| auth | http://localhost:8001 | 가입 · 로그인 · 재설정 · `/me`, Swagger `http://localhost:8001/docs` |
| Mailpit | http://localhost:8025 | 가입 인증 코드 · 재설정 메일이 여기로 온다(실제로 발송되지 않음) |
| PostgreSQL | localhost:5432 | DB `easyq`, 소유자 `easyq_owner` / `easyq_owner_dev` |

- 처음 뜰 때 v1.2 스키마 · 인증 뷰 · 실행 계정(auth · backend · worker) · 권한을 자동으로 만든다.
- DB를 처음 상태로 되돌리기: `docker compose -f compose.dev.yaml down -v`
- 교안 임베딩 · 음성 STT는 키가 있어야 동작한다. 없으면 교안은 `INDEX_BUILD_FAILED`, `audio.start`는 `STT_UNAVAILABLE`로 끝난다. 키가 있으면 `backend/.env`에 `EMBEDDING_URL` · `EMBEDDING_API_KEY` · `EMBEDDING_MODEL` · `STT_MODEL` · `STT_API_KEY`를 넣고 같은 명령으로 띄운다.

## 2. 프론트 쪽 설정 (필수)

백엔드는 설계서대로 교차 출처(CORS)를 허용하지 않고 `Origin`이 `PUBLIC_ORIGIN`(기본 `http://localhost:3000`)과 정확히 같아야 받는다. 쿠키도 같은 출처에서만 실린다. 그래서 **브라우저는 항상 `http://localhost:3000`으로만 요청하고, Next.js 개발 서버가 `/api/v1/*`를 auth · backend로 넘겨야 한다.** 넘기는 규칙은 운영 Caddy(7.1.4)와 같다.

```js
// next.config.js (개발용)
module.exports = {
  async rewrites() {
    return [
      { source: '/api/v1/users', destination: 'http://localhost:8001/api/v1/users' },
      { source: '/api/v1/auth/:path*', destination: 'http://localhost:8001/api/v1/auth/:path*' },
      { source: '/api/v1/me', destination: 'http://localhost:8001/api/v1/me' },          // 정확히 /me만 auth
      { source: '/api/v1/:path*', destination: 'http://localhost:8000/api/v1/:path*' },  // /me/consents 등 나머지
    ]
  },
}
```

- WebSocket(`ws://localhost:3000/api/v1/ws/sessions/{sid}`)도 같은 출처로 연결해야 쿠키와 Origin 검사가 통과한다. Next.js rewrites가 WebSocket을 넘기지 못하는 버전이면, 개발 중에만 `ws://localhost:8000/...`으로 직접 붙이지 말고 개발용 프록시(예: `http-proxy-middleware`)로 같은 출처를 유지한다.
- 다른 주소에서 프론트를 띄우면 `PUBLIC_ORIGIN=http://<주소> docker compose -f compose.dev.yaml up`으로 바꾼다.
- 로컬은 HTTP라 쿠키에 `Secure`를 붙이지 않는다(`COOKIE_SECURE=false`, 개발 compose에 설정됨).

## 3. 테스트 계정

`seed` 서비스가 처음 실행 때 한 번 만든다(이미 있으면 그대로 둔다). 메일 인증 없이 바로 로그인된다.

| 종류 | 이메일 | 비밀번호 |
|---|---|---|
| 회원(이메일 인증 완료, SERVICE 동의) | member@easyq.local | easyq2026 |
| 관리자(ADMIN) | admin@easyq.local | admin2026 |

- 로컬 개발 DB 전용 값이다. `PUBLIC_ORIGIN`이 localhost가 아니거나 `EASYQ_DEV_SEED=1`이 없으면 시드는 실행을 거부한다.
- 운영 관리자 계정은 서버에서 `python -m easyq.admin create --email … --display-name …`로만 만든다.

## 4. 요청 규칙 요약 (7.1.2)

- 변경 요청은 `Origin` 헤더가 필요하다. 로그인 후 변경 요청은 `X-CSRF-Token`도 보낸다(로그인 · 입장 응답의 `csrf_token`).
- 새로고침으로 CSRF 토큰을 잃으면 `POST /api/v1/auth/csrf-tokens`(헤더 `X-EasyQ-Client: web`)로 다시 받는다.
- 성공은 `{request_id, data}`, 실패는 `{request_id, error:{code, message, details, retryable}}`.
- 요청 · 응답 필드: Swagger(`/docs`)에 인증 API-01~09와 입장 API-18 · 19부터 들어 있다. 나머지는 `backend/docs/api-v0.3.md`.
