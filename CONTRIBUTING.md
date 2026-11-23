# 작업 규칙 — EasyQ 서비스 백엔드(auth · backend · worker)

이 레포에는 서비스의 auth · backend · worker만 둔다. frontend · caddy · trainer는 다루지 않는다.
공용 검색 모듈 `retrieval`은 [AI 레포](https://github.com/EasyQ-P/AI)에 있고, 여기서는 태그로 고정해 설치만 한다(`git+https://github.com/EasyQ-P/AI@vX.Y.Z#subdirectory=retrieval`). 고칠 일이 있으면 AI 레포에 요청한다.
근거: 설계서 1.3(역할), 10.1(형상 관리), 10.2(디렉터리), 10.3(명칭 표준안, 표 10.6).

## 담당과 리뷰어 (설계서 1.3)

| 모듈 | 위치 | 담당 | 같이 볼 사람 |
|---|---|---|---|
| P1 강연 · 참가, P2 실시간 · R2 음성 중계, P3 관리 | `backend/app/api`, `backend/app/ws` | 신재우 | 조현준 |
| A1 인증 · 계정 | `auth/` | 신재우 | 조현준 |
| 인프라(EC2 · RDS · S3) · CI | Dockerfile, compose, `.github/workflows/` | 신재우 | 조현준 |
| R1 교안 처리 | `backend/app/jobs`(교안, 청킹 · 색인은 retrieval 패키지 호출) | 조현준 | 신재우 |
| R5 질문 · 공감 · 필기 · 피드백, 보관 · 삭제 작업 | `backend/app/services`, `backend/app/jobs`(정리 · 파기) | 조현준 | 신재우 |
| R2 발화 색인, R3 검색 · 판정, R4 답변 · 요약 | `backend/app/jobs`, `adapters/`(stt · embedding · llm · judge) | 신재우(서비스 쪽) | **최의찬**(AI) |
| L 연계(학습서버 어댑터, 동기화) | `adapters/trainer_api`, `jobs`(동기화) | 신재우 · 조현준 | **최의찬** |

- 백엔드 PR은 다른 백엔드 담당 1명이 리뷰한다.
- R2~R4 · L 연계와 학습서버 API(7.4) 계약을 건드리는 PR은 최의찬도 리뷰어로 넣는다.
- 응답 객체(7.3.1) · WebSocket 이벤트(7.3.10)를 바꾸는 PR은 심주혜에게 알린다.

## 브랜치 (설계서 10.1 · 10.3)
- `main`: 진도 점검 · 시연용. 브랜치 보호, PR 리뷰 후에만 병합하고 직접 push하지 않는다.
- `dev`: 개발 통합 브랜치. 기능 브랜치는 여기서 따고 여기로 PR한다.
- 기능 브랜치: `유형/짧은-설명` (소문자 kebab-case)
  - 예: `feature/qa-pipeline`, `fix/ws-reconnect`, `feature/join-guest`, `refactor/job-lease`
  - 유형은 커밋 유형과 맞춘다: `feature`(=feat) · `fix` · `docs` · `refactor` · `test`
- 브랜치 하나에 기능 하나. 점검일 전에 `dev`로 합친다.

## 점검일 · 태그
점검일마다 `dev` → `main` PR을 합친 뒤 `main`에 태그를 단다.

| 태그 | 날짜 | 진척 |
|---|---|---|
| `v0.3` | 12/2(수) | 30% |
| `v0.4` | 12/3(목) | 40% |
| `v0.5` | 12/4(금) | 50% |
| `v0.7` | 12/7(월) | 70% |
| `v0.9` | 12/8(화) | 90% |
| `v1.0` | 12/9(수) | 100% |

태그별 백엔드 범위는 `CLAUDE.md`의 "점검일 태그와 범위"(출처: `구현 전도표/` 진도표)를 따른다.

## 커밋 메시지 (설계서 10.3)
`유형: 요약` — 유형은 feat · fix · docs · refactor · test

- 예: `feat: 이관 질문 공감 정렬 추가`, `feat: 질문 등록 API(API-27) 추가`, `fix: WebSocket 재연결 시 이벤트 누락 보완`
- 요약은 한국어 한 줄. 관련 API ID · 설계서 절이 있으면 괄호로 붙인다.

## Issue · Pull Request (설계서 10.1: GitHub Issue · PR, GitHub Actions)
- 진도표 세부기능 단위로 Issue를 만들고, PR 본문에 `Closes #번호`로 연결한다.
- PR 대상: 기능 브랜치 → `dev`. 점검일에만 `dev` → `main`.
- PR 제목은 커밋 메시지 형식과 같다.
- PR 본문은 `.github/pull_request_template.md`를 채운다. 적을 것
  - 관련 설계서 절 · API ID · 테이블 · 작업 종류(예: 6.2.7, API-27, questions, jobs `QA`)
  - 설계와 다르게 한 부분: 팀에 먼저 공유해 합의한 것만, 내용과 이유
  - DB 변경: Alembic 마이그레이션 파일 이름, `easyq_schema_v1.2.sql`과 달라지는 점
  - 시험 방법과 결과
- 병합 조건: CI 통과 + 리뷰어 승인. 병합 후 기능 브랜치는 지운다.

## CI · 시험 (설계서 10.1)
- CI(GitHub Actions)에서 Ruff(린트 · 포맷), pytest, auth · backend Docker 이미지 빌드를 돌린다. PR 전에 로컬에서도 통과시킨다.
- 패키지는 uv로 관리하고 `uv.lock`을 함께 커밋한다. 의존성을 추가하면 PR에 이유를 적는다.
- DB 스키마 변경은 `backend/migrations/`의 Alembic 마이그레이션으로만 한다(auth · backend 공용, 한 곳에서 관리). 이미 `dev`에 합쳐진 마이그레이션 파일은 고치지 않고 새 파일을 추가한다.
- 새 API · 작업에는 성공 경로와 주요 실패 경로(8.4.2 오류 코드) 시험을 넣는다. 권한(역할 · 소유 · 참가)과 강연 범위(`session_id`) 위반 시험을 빠뜨리지 않는다.
- 외부 연동(STT · 임베딩 · Sonnet · 판정 추론 · 학습서버 · S3 · SMTP)은 `adapters/`를 가짜로 바꿔 시험한다. CI에서 실제 키로 외부 호출을 하지 않는다.
- 부하 시험(k6)과 근거 충실도 측정(RAGAS) 스크립트는 `backend/tests/`에 두고 CI에서는 돌리지 않는다.

## 설계서 기준
- 백엔드 기준 문서는 설계서 6.1~6.3, 7.1~7.3, 8.1~8.4와 `DB 스키마파일/easyq_schema_v1.2.sql`이다.
- 설계와 다르게 구현해야 하면 코드부터 바꾸지 말고 먼저 팀에 알린다. 설계서에 없는 API · 테이블 · 컬럼 · 상태값 · 오류 코드를 임의로 추가하지 않는다.

## 이름 규칙 (설계서 10.3, 표 10.6)

| 구분 | 대상 | 규칙 | 예시 |
|---|---|---|---|
| Python | 변수 · 함수 | snake_case, 함수는 동사로 시작 | `check_overlap()`, `question_id` |
| | 클래스 | PascalCase | `TrainingRun`, `EvalReport` |
| | 상수 · 환경 변수 | UPPER_SNAKE_CASE | `TRAINING_API_BASE_URL` |
| | 파일 · 모듈 | snake_case.py | `feedback_candidates.py`, `trainer_api.py` |
| REST API | URL 경로 | 소문자 kebab-case, 리소스는 복수 명사, `/api/v1` 접두어 | `/api/v1/feedback-candidates` |
| | JSON 필드 · 쿼리 | snake_case | `answer_id`, `since_version` |
| | 헤더 | 단어 첫 글자 대문자 + 하이픈, 사용자 정의는 `X-` | `X-CSRF-Token`, `Idempotency-Key` |
| DB | 테이블 | snake_case 복수형. `reg_` 접두어는 학습서버 전용이라 서비스 DB에 쓰지 않음 | `lecture_sessions`, `question_notes` |
| | 컬럼 | snake_case, 외래 키는 테이블 단수형 + `_id` | `session_id`, `raw_data_expires_at` |
| | 열거값 | UPPER_SNAKE_CASE | `ESCALATED`, `LLM_ONLY` |
| 이벤트 · 오류 | WebSocket 이벤트 · 로그 이벤트 | `대상.동작`(소문자, 점 구분) | `answer.delta`, `question.decided` |
| | 오류 코드 | UPPER_SNAKE_CASE, 8.4.2 표에 있는 것만 | `SESSION_NOT_LIVE` |
| 파일 · 버전 | 로그 파일 | `{종류}-{date}-{instance}.jsonl` | `access-2026-11-02-backend-1.jsonl` |
| | 학습서버에서 받는 버전 · 작업 번호 | 형식 그대로 저장 | `ds-v2`, `es-v1`, `judge-v3`, `train-0001` |

API · DB · 로그에서 같은 대상은 설계서 6~8장의 같은 이름으로 부른다.

## 커밋하면 안 되는 것
- `.env`와 모든 비밀값: Gemini · Anthropic API 키, 학습서버 `X-API-Key`, SMTP 자격 증명, AWS 자격 증명, DB 비밀번호, HMAC · 세션 서명 키, SSH 개인 키(터널 키는 EC2 호스트의 제한된 경로에만)
- 사용자 데이터: 교안 원본(PDF · PPTX), 음성, 질문 · 답변 · 필기, DB 덤프. 시험용 데이터는 직접 만든 가짜 데이터만 쓴다.
- 로그 파일(`*.jsonl`), `__pycache__/`, `.venv/`
- 학습 데이터 · 체크포인트 · 모델 · AI Hub 데이터(학과 서버 `/data/easyq/` 밖으로 옮기지 않는다, 3.1.3)

환경 변수는 `.env.example`에 키 이름과 설명만 적는다. 비밀값을 커밋했다면 바로 팀에 알리고 키를 폐기 · 재발급한다(기록에서 지우는 것만으로는 부족하다).
