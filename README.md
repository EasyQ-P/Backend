# EasyQ 서비스 백엔드 (auth · backend · worker)

강연 중 청중 질문에 교안 · 발화 근거로 AI가 답하고, 근거가 부족하면 강연자에게 넘기는 서비스 **EasyQ**의 백엔드 레포입니다.
frontend · caddy · trainer(학습서버)는 이 레포의 범위가 아닙니다. 공용 검색 모듈 `retrieval`은 [AI 레포](https://github.com/EasyQ-P/AI)에서 태그로 고정해 설치합니다.

- 담당: 신재우 · 조현준 (AI 연계: 최의찬 · 엄윤종, 화면: 심주혜)
- 작업 규칙: [CONTRIBUTING.md](CONTRIBUTING.md) · 개발 기준: [CLAUDE.md](CLAUDE.md)

## 현재 진행 상황 (2026-11-27 기준)

기준 설계는 **백엔드 v1.4 작업본**(`설계서/CodingAnt팀_설계서_v1.0_백엔드v1.4작업본.hwpx`)이다. 진도표 버전과 상관없이 **서비스 백엔드 전체를 구현**했다(브랜치 `dev-현준`).

- 서비스 REST API 65개 전부, WebSocket(7.3.10), auth 내부 API(INT-01~03)
- worker 작업: INGEST · TRANSCRIPT_INDEX · QA(하이브리드 검색 · 판정 · Claude 답변 · 인용 검증) · AUTO_END · FINALIZE · SUMMARY · PURGE · CLEANUP · DATASET · TRAIN · DEPLOY · SYNC · 메일 · AUTH_REVOKE
- DB: `easyq_schema_v1.4.sql` + v1.4 실행 계정 권한(`migrations/002_runtime_grants.sql`). auth · backend · worker는 각자 허락된 SQL만 쓴다(PostgreSQL 시험 `EASYQ_TEST_ROLES=1`로 확인).
- 설계가 비어 있어 구현에서 정한 값과 남은 일: [docs/service/implementation-v1.4.md](docs/service/implementation-v1.4.md)

아직 확인하지 못한 것: 실제 외부 서비스(Claude · Gemini 임베딩/STT · 학습서버 · SMTP · S3) 연결, docker compose 실제 기동. 목표 기술 스택(SQLAlchemy · Alembic · uv · pytest) 전환도 아직이다.

## 레포 구성

```
common/ auth/ backend/ worker/   공용 코드와 컨테이너별 코드(각자 Dockerfile · requirements.txt)
compose.yaml             로컬 개발 스택(바인드 마운트)
migrations/ dev/ deploy/ tests/ scripts/ docs/service/
DB 스키마파일/            easyq_schema_v1.4.sql (서비스 DB 기준), 학습서버 레지스트리 SQL
설계서/                   원본 설계서 (HWP), 백엔드 v1.4 작업본 (HWPX)
docs/                    설계서-v1.4-…(사본 · 검토 결과 · 팀 요청), 이전 v1.2 반영본, 팀 공유 보류사항 · 유스케이스 · Mermaid 원본
구현 전도표/              진도표 v6 (HWPX)
CLAUDE.md, CONTRIBUTING.md, .github/
```

## 프론트 연동용 로컬 개발 서버

레포 루트에서 `docker compose up --build` — PostgreSQL · Mailpit(:8025) · auth(:8001) · backend(:8000) · worker · Caddy(http://localhost:8080)와 테스트 계정(member@easyq.local / admin@easyq.local)을 함께 띄운다. 코드는 바인드 마운트라 고치면 바로 반영된다. 자세한 내용은 [docs/service/frontend-dev.md](docs/service/frontend-dev.md).

## 실행 · 시험

```powershell
python -m pip install -r common/requirements.txt -r auth/requirements.txt -r backend/requirements.txt -r worker/requirements.txt
python scripts/run_tests.py   # 레포 루트에서, 결과는 docs/service/test-results.json
```

오프라인 테스트는 SQLite 하네스와 가짜 외부 연동(Claude · 학습서버 · 임베딩 · STT · 메일)으로 실제 서비스 · worker 코드를 실행합니다. `EASYQ_TEST_DATABASE_URL`(이름이 `_test`로 끝나는 DB)을 주면 같은 시험을 PostgreSQL 16 + pgvector에서도 돌리고, `EASYQ_TEST_ROLES=1`을 더하면 v1.4 권한을 적용해 auth · backend · worker를 각자의 DB 계정으로 실행합니다. 2026-11-27: SQLite와 PostgreSQL(권한 적용) 합계 315개 모두 통과, 권한 오류 0건. 자세한 절차는 [docs/service/runbook.md](docs/service/runbook.md), 구현 설명은 [docs/service/implementation-v1.4.md](docs/service/implementation-v1.4.md)를 참고하세요.
