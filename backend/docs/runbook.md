# EasyQ v0.3 실행 안내

## 1. 사전 조건

Python 3.12 이상, PostgreSQL 16 + pgvector, SMTP, 교안용 비공개 S3, AI 팀과 합의한 Gemini Embedding 2 모델과 768차원 배치 임베딩 API가 필요하다. Python 패키지는 `backend/requirements.txt`로 설치한다. 이 파일의 버전 범위는 현재 환경에서 다운로드·설치 검증하지 못했다.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 2. DB 생성 후 적용 순서

애플리케이션은 테이블을 만들지 않는다. 먼저 비어 있는 easyq DB를 준비하고 migration owner 계정으로 아래 파일을 한 번 적용한다. 이미 초기화한 DB에는 원본 CREATE TABLE SQL을 다시 실행하지 않는다.

```powershell
psql $env:MIGRATION_DATABASE_URL -v ON_ERROR_STOP=1 -f '../DB 스키마파일/easyq_schema_v1.3.sql'
psql $env:MIGRATION_DATABASE_URL -v ON_ERROR_STOP=1 -f '../DB 스키마파일/easyq_grants_v1.3.sql'
```

`easyq_training_registry_v1.2.sql`은 학과 서버의 별도 레지스트리 DB용이다. 서비스 DB에 이어서 실행하지 않는다. 서비스 DB 초기화에는 vector 확장을 생성할 수 있는 권한이 필요하다.

auth/backend/worker의 DB LOGIN 역할을 따로 만들고 `002_runtime_grants.example.sql`을 실제 역할명에 맞춰 적용한다. 런타임 계정에 migration owner나 슈퍼유저 권한을 주지 않는다. RDS에서는 같은 VPC와 EC2 보안 그룹으로만 5432 접근을 허용하고 DSN에 인증서 검증을 설정한다.

## 3. 환경 설정

`.env.example`을 `.env`로 복사하고 플레이스홀더를 채운다. `.env`는 소스 관리에서 제외했다. 각 프로세스의 DATABASE_URL을 따로 지정한다. PUBLIC_ORIGIN은 브라우저가 실제 사용하는 정확한 출처이며 끝에 `/`를 붙이지 않는다.

키 생성 예시:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

첫 명령은 HMAC·backend 내부 키·worker 내부 키 각각 별도로 실행한다. 두 번째 키는 발송용 암호화에 쓴다. 키는 auth와 worker의 필요한 처리 경계에만 제공한다. SMTP는 STARTTLS 또는 TLS와 인증서 검증을 사용한다.

| 설정 | 용도 |
| --- | --- |
| AUTH_DATABASE_URL/BACKEND_DATABASE_URL/WORKER_DATABASE_URL | Compose가 각 프로세스의 DATABASE_URL로 매핑 |
| CODE_HMAC_KEY | 코드·재설정·제한 버킷·게스트 재시도 서명 |
| DELIVERY_ENCRYPTION_KEY | 인증·재설정 발송용 Fernet 키 |
| INTERNAL_SERVICE_KEY | backend → auth 발급·폐기·HOST 연장 |
| WORKER_SERVICE_KEY | worker → auth 폐기만 |
| AUTH_BASE_URL | auth 내부 주소. 운영 내부 TLS는 인프라에서 구성 |
| STORAGE_BACKEND/S3_BUCKET | 운영 s3, 개발 시 local 선택 가능 |
| EMBEDDING_URL/API_KEY/MODEL | 합의한 모델의 batchEmbedContents endpoint·키·실제 모델 ID |
| EMBEDDING_MAX_BATCH_COST_USD/PRICE_VERSION | 32청크 배치의 보수적인 최대 예상 비용·가격 버전 |
| EVAL_REFERENCE_VERSION/EVAL_REFERENCE_RETRIEVAL_VERSION | 가져온 평가 자료의 호환 버전. 모르면 비워 PENDING 유지 |
| SERVICE_POLICY_VERSION/TRAINING_POLICY_VERSION | boolean 입장 동의의 서버 정책 버전(기본 service-v1/training-v1) |

임베딩 URL·모델 ID는 확인하지 않은 값을 코드에 고정하지 않았다. AI 담당자와 정확한 배포 ID·768차원 지원·배치 응답을 확인해 설정한다. Gemini 호출은 Google 형식 `requests[].content.parts[].text`, taskType=RETRIEVAL_DOCUMENT, outputDimensionality=768, 응답 `embeddings[].values`를 사용한다. 운영 API가 이 계약과 다르면 adapters.GeminiEmbedder를 그 실제 계약에 맞춰 수정하고 연결 테스트한다.

## 4. 로컬 또는 컨테이너 실행

각 터미널에 해당 역할의 DATABASE_URL과 필요한 환경변수를 설정한 후 실행한다. Uvicorn은 `.env`를 자동으로 읽지 않는다. 셸 환경에 값을 넣거나 Compose의 env_file을 사용한다.

```powershell
python -m uvicorn easyq.auth_app:app --host 127.0.0.1 --port 8001 --no-access-log
python -m uvicorn easyq.backend_app:app --host 127.0.0.1 --port 8000 --no-access-log
python -m easyq.worker
```

로컬이면 AUTH_BASE_URL=http://127.0.0.1:8001로 지정한다. http 개발 도메인에서는 COOKIE_SECURE=false를 명시적으로 사용하고 PUBLIC_ORIGIN을 동일 출처 프록시 주소로 맞춘다. 운영은 HTTPS·COOKIE_SECURE=true를 유지한다.

개발 local 객체 저장을 쓸 때 backend와 worker의 OBJECT_ROOT는 같은 절대 경로다. 운영 S3는 교안 원본 버킷만 접근하는 IAM을 사용한다. 업로드 중 DB 오류가 나면 보상 삭제를 시도하며, 미완료 객체 정리를 위한 lifecycle 정책을 별도로 둔다.

컨테이너는 다음처럼 실행한다. 기존 DB를 사용하며 DB 컨테이너를 만들지 않는다.

```powershell
docker compose up --build
```

Compose에는 호스트 포트 게시가 없다. auth/backend는 Caddy가 넣는 X-Forwarded-For로 실제 청중 IP를 얻는다(`--proxy-headers`, `FORWARDED_ALLOW_IPS`, 기본 `*`). 이 설정이 없으면 모든 요청이 Caddy 컨테이너 IP로 보여 IP별 제한(잘못된 입장 코드 15분 20회 등)이 서비스 전체에 걸린다. `*`는 포트를 게시하지 않아 Caddy만 접근할 수 있을 때만 안전하므로, auth/backend 포트를 외부에 게시하면 Caddy 주소(또는 easyq-network 대역)로 좁힌다. 기존 frontend/Caddy가 easyq-network에 연결되어 Caddyfile.example의 규칙으로 프록시해야 한다. PUBLIC_ORIGIN과 실제 도메인을 맞추고 auth/backend의 포트와 `/internal/v1`을 외부로 노출하지 않는다. 내부 TLS·서비스 자격증명 배포는 운영 인프라에서 적용한다. 네트워크만 준비된 개발 환경의 내부 호출 예시는 HTTP다.

각 앱 `/docs`와 `/openapi.json`은 내부에서 API 목록을 볼 수 있는 보조 문서다. 요청 본문 세부 스키마의 권위 문서는 최종 HWP와 implementation-v0.3.md다. OpenAPI 요청 객체는 현재 일반 object로 기술되어 있어 본문 필드를 자동 채워주지 않는다. `/health/live`는 DB 없이 프로세스를, `/health/ready`는 DB 접근을 확인한다.

## 5. 자동 테스트

```powershell
python scripts/run_tests.py
```

오프라인 테스트는 cryptography·pypdf와 표준 라이브러리를 사용한다. 실제 FastAPI 마운트의 ASGI 처리기·서비스·워커를 호출하지만 HTTP 포트를 열지 않는다. 테스트 전용 SQLite 어댑터와 제공자 대역을 사용하며 `docs/test-results.json`에 결과를 기록한다.

실제 PostgreSQL용 테스트는 별도 `easyq_test` DB를 만든 후 실행한다. 테스트 계정에는 확장 설치(또는 기존 vector 확장), 임의 스키마 생성·삭제 권한이 필요하다. `*_test`로 끝나지 않는 DB명은 거부한다.

```powershell
$env:EASYQ_TEST_DATABASE_URL='postgresql://test_owner:CHANGE_ME@localhost:5432/easyq_test'
python scripts/run_tests.py
```

동일 API 테스트가 PostgreSQL에서도 실행되고 원본 SQL·뷰 migration을 그대로 검증한다. 테스트마다 `easyq_test_<random>` 스키마를 만들고 종료 시 그 스키마만 삭제한다. 운영 DB를 이 변수에 지정하지 않는다.

## 6. 실제 서비스 연결 확인 순서

1. auth/backend의 readiness와 Caddy 경로 분기를 확인한다.
2. 가입 → 실제 메일 도착 → 코드 인증 → 로그인 → CSRF 복구 → `/me`를 확인한다.
3. 강연 생성 → 미리보기 → 다른 회원·게스트 입장 → QR 재발급 후 이전 코드의 410을 확인한다.
4. 실제 PDF/PPTX 업로드 → jobs 완료 → documents READY → knowledge_chunks의 768차원·tsvector·페이지 번호를 확인한다.
5. 강연 시작 → 수정 버전 충돌 → 종료 → 게스트 접근 거부·90일 보관 시각을 확인한다.

이 확인은 이번 환경에서 실행하지 못했다. DB와 실제 SMTP·S3·Gemini 설정을 준비한 환경에서 완료해야 실연결 검증을 마칠 수 있다.
