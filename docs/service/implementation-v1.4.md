# 백엔드 전체 구현 (설계 v1.4 작업본 기준, 2026-11-27)

- 브랜치: `dev-현준` (origin/dev에서 분기, 커밋 전)
- 기준: `설계서/CodingAnt팀_설계서_v1.0_백엔드v1.4작업본.hwpx`, `DB 스키마파일/easyq_schema_v1.4.sql`, `migrations/002_runtime_grants.sql`
- 범위: 서비스 REST API 65개 전부, WebSocket(7.3.10), auth 내부 API 3개, worker 작업 15종 중 서비스 쪽 전부
- 진도표 버전과 무관하게 전체를 구현했다. 실제 외부 서비스(Claude · Gemini · 학습서버 · SMTP · S3)와는 아직 연결해 보지 않았고, 시험은 가짜 객체로 한다.

## 1. 구조

| 컨테이너 | 파일 | 담당 |
|---|---|---|
| auth | `accounts.py` | API-01~03, 07, 08, **API-10(표시명 · 비밀번호 변경)** |
| | `sessions.py` | API-04~06, 09, INT-01~03 |
| backend | `lectures.py` · `participation.py` · `consents.py` · `documents.py` · `routes.py` | API-11, 13~23, 25, 41 (v0.3) |
| | `questions.py` | API-12, 26~36, 38 |
| | `reports.py` | API-37, 39 |
| | `deletion.py` | API-24, 40 |
| | `notices.py` | API-42~44, 49~52 |
| | `admin.py` | API-45~48, 64, 65 |
| | `training.py` | API-53~63 |
| | `live/` | WebSocket: 질문 · 답변 이벤트는 질문자(검토용은 HOST)에게만 |
| worker | `qa.py` | QA: 하이브리드 검색 · 판정 · 답변 · 인용 검증 |
| | `finalize.py` | AUTO_END · FINALIZE · SUMMARY |
| | `purge.py` · `cleanup.py` | PURGE(삭제 요청 · 보관 기간 파기), CLEANUP(일일 정리 · TRAINING 철회) |
| | `sync.py` | DATASET · TRAIN · DEPLOY · SYNC |
| common | `trainer.py` | 학습서버 7.4 고정 어댑터(API마다 메서드 하나, 임의 URL 없음), 현재 서빙 설정 읽기 |
| | `candidates.py` | 라벨 후보 생성 · 재확인(API-33 · 34와 worker가 같은 함수 사용) |

- backend 경로는 영역 파일의 `ROUTES` 표로 등록한다(`router.py`). Swagger 목록은 각 파일의 `OPENAPI`를 `main.py`가 합친다.
- worker 작업은 영역 파일의 `JOB_HANDLERS`로 등록한다(`runner.py`). 질문 판정(QA) · 발화 색인이 다른 작업보다 먼저 점유된다.
- `runner.maintenance()`(약 1분마다): 일일 PURGE · CLEANUP 등록(하루 한 번), 학습서버 SYNC 1시간 주기 유지, 예산 보류 작업 재개.

## 2. v1.4로 바뀐 것 (v0.3 코드 수정)

- 스키마 v1.4: `documents.sha256` 저장, 비동기 API 중복 요청은 `jobs.client_request_key`(주체 · 작업 종류 · 키의 HMAC) + `request_hash`로 확인(`core.idempotent()`).
- 권한 v1.4: backend는 조각을 지울 수 없어 교안 재처리(API-23) 때 이전 조각 삭제를 worker가 한다. auth는 `jobs` UPDATE가 없어 메일 재발송 때 이전 메일 작업을 취소하지 않고 worker가 폐기된 코드를 건너뛴다. auth는 `ws_events` SELECT가 없어 이벤트 번호를 시퀀스에서 먼저 받는다(검토 D-2, 권한 추가 없이 해결).
- 게스트 재입장 쿠키 `easyq_join_retry`: participant_id · session_id · 만료(입장 후 24시간)를 서명. 입장 확인 외에는 쓰지 않는다.
- 정원(API-19): ACTIVE 회원 + 유효한 게스트 세션이 있거나 입장 후 10분이 안 된 ACTIVE 게스트.
- 강연자 SERVICE 철회(API-20): LIVE면 종료(end_reason HOST, If-Match 없음), DRAFT는 그대로 두고 시작(API-16)을 403 CONSENT_REQUIRED로 막는다.
- 지난 강연(API-14 hosted): ENDED · PURGED, 강연자가 지운 강연은 빠진다(N1). `host_deleted_at`은 강연자에게만 404이고 보관함 회원은 계속 본다(6.2.11-3).
- 교안 목록(API-22)에서 DELETING은 빠진다.

## 3. 설계가 비어 있어 정한 것 (팀 확인 필요)

컬럼 · 상태값 · 오류 코드는 새로 만들지 않았다. 아래는 설계서에 수치나 규칙이 없어 구현에서 정한 값이다.

### 질문 · 답변 (API-12, 26~39)
- API-27 Idempotency-Key는 UUID만 받는다(`questions.idempotency_key`가 UUID 컬럼, 검토 P-6). 같은 키 · 같은 본문은 이전 202, 다른 본문은 409.
- 유사 질문(API-26, 검토 D-17): 임베딩을 쓰지 않고 글자 bigram Dice 유사도, 이관(ESCALATED) 질문 중 0.3 이상 상위 3개. 저장 없음.
- 남의 개인 질문은 모든 API에서 404. HOST는 질문을 등록할 수 없다(403).
- 공감 취소(API-31, 검토 P-7): LIVE 강연이면 질문 상태와 무관하게 허용. 공감은 질문 version을 올리지 않는다(API-32 If-Match 보호).
- 대시보드 상단 수치(검토 F-1)용 필드는 추가하지 않았다.
- 요약 노트(API-37): FINALIZE 전에는 `{summary_id: null, status: 'QUEUED'}`.
- 강연 통계(API-39): 저장된 통계가 없으면 계산해서 돌려주고 저장하지 않는다.

### 질문 판정 · 답변 생성 (QA)
- 서빙 설정이 아직 없을 때(검토 A-4): CUSTOM 모드, LLM_ONLY 기준 70점(0~100), 모델 `claude-sonnet-5-5`, 프롬프트 `llm-only-v1`(환경 변수로 변경). 학습서버가 τ_LLM을 0~1로 주면 100을 곱해 비교한다.
- 판정 추론이 800 ms 초과 · 611 · 오류 · 미설정이면 그 질문만 LLM_ONLY. `fallback_reason` 값(검토 A-6): TIMEOUT · MODEL_UNAVAILABLE · ERROR · NOT_CONFIGURED.
- 벡터 검색은 강연 안에서 정확 거리 정렬(전역 HNSW 사후 필터로 20개가 모자라는 문제, 검토 D-8). 키워드는 `search_vector`의 위치로 BM25(k1 1.5, b 0.75, `BM25Index`와 같은 식).
- 답변 첫 줄이 INSUFFICIENT면 버리고 이관(2차 근거 부족). 4문장 넘으면 4문장으로 자른다. 목록에 없는 인용 번호는 지우고, 유효 인용이 0개면 버리고 이관.
- `decision_snapshot`에 8.2.36 외 키 `llm_score`(0~100), `max_score`를 더 남긴다.
- 질문 임베딩 실패도 PROVIDER_FAILURE로 이관한다(더 맞는 사유 코드 없음).
- Claude 호출: `output_config.effort = low`, 거절(refusal)은 PROVIDER_FAILURE. 서버 쪽 대체 모델(fallbacks)은 켜지 않았다.

### 강연 종료 · 요약
- FINALIZE는 진행 중인 QA · 발화 색인을 종료 후 60초까지 기다린다(검토 S-6).
- 요약 입력 상한: 발화 15만 자, 교안 5만 자. 확정 발화가 1,000자 미만이면 SKIPPED.

### 라벨 후보
- 후보 키(event_key)는 사건 · 질문 · 답변 · 근거 · 행위자 · 판(revision)의 해시. 청중 평가는 평가값과 판을 빼고 하나로 본다. 평가가 바뀌면 검수 전 PENDING 후보의 종류만 바꾼다(검토 D-3, 삭제 권한 불필요).
- 평가용 강의 검사가 PENDING이면 미룬다. 1차 가림(이메일 · 전화번호 · 7~10자리 숫자 → [MASK]).
- `retrieved_evidence`에 `page_no` · `start_ms` · `end_ms`를 함께 둔다(검토 A-17).

### 삭제 · 보관 · 계정
- API-10(검토 F-4): 응답은 `User` 그대로. 비밀번호를 바꾸면 그 계정의 모든 세션을 폐기하고 새 세션 쿠키를 준다. 새 CSRF 토큰은 API-06으로 받는다(403 reason CSRF → API-06 → 재시도 흐름). 현재 비밀번호 확인 계정당 15분 10회.
- API-24 · 40은 PURGE 작업(payload scope DOCUMENT · QUESTION · ARCHIVE · SESSION · ACCOUNT)으로 처리한다(설계에 작업 종류 이름 없음). `reason`은 검사만 하고 저장하지 않는다.
- ARCHIVE 삭제는 종료된 강연에서만(DRAFT · LIVE는 409).
- MERGING 후보가 있으면 PURGE · CLEANUP은 실패하지 않고 10분 뒤 다시 대기한다.
- 90일이 지난 게스트 개인 질문은 행을 남기고 내용만 지운다(`content_purged_at`).
- 공지(검토 F-6): Notice 응답에 `version`을 넣고 API-49 · 50이 ETag를 준다.
- 감사 기록 action/target_type: NOTICE_* · INQUIRY_REPLIED, TRAINING_STOPPED, BUDGET_UPDATED(target_type SETTING), SERVING_CONFIG_SYNCED. 설계 예시 목록에 없는 값이다.

### 관리자 · 학습서버
- API-46 정지: 상태 변경 · 감사 · 세션 폐기 작업 · 진행 중 강연 강제 종료를 한 트랜잭션. ACTIVE · SUSPENDED만 정지 가능(검토 D-6). 마지막 ADMIN은 정지 불가(409).
- API-46 복구(검토 P-9): INT-02를 먼저 동기 호출하고, 실패하면 503 AUTH_SERVICE_UNAVAILABLE(정지 유지).
- API-47(검토 P-10): 설계대로 `host_user_id` · `join_code`를 준다.
- API-54(검토 P-8): 검수자 2명. 세 번째는 409 REVIEW_CONFLICT. 불일치는 두 검수자 중 한 명이 다시 저장해서 푼다. 둘 다 REJECT면 REJECTED.
- API-55: 병합 실패(601/613)는 후보를 MERGING → APPROVED로 되돌린다. 학습서버 불통이면 MERGING으로 두고 1시간 SYNC가 같은 `merge_request_id`로 다시 보낸다(검토 A-3). `cleanup_cutoff_at`은 DATASET 작업 payload에 둔다.
- API-57: 데이터셋 상태 값이 없어(검토 A-12) DELETED · FAILED · INVALID만 DATASET_NOT_READY. config는 epochs 1~10, batch_size 8 · 16, learning_rate 1e-6~1e-4.
- API-62(검토 A-2): backend가 7.4.22 · 23을 동기 호출하고 DEPLOY 작업이 7.4.21로 적용을 확인한다. 첫 전환은 `expected_active_deployment_id` null 허용.
- 모델 동기화(검토 D-4): 7.4.17 · 18에 `artifact_uri`가 없어 `/home/t26112/easyq-data/models/<버전>/`를 넣고, sha256이 없으면 0 64자리.
- 운영 지표(API-64)는 서울 날짜 기준. 학습 실행 목록 API(검토 P-2)는 추가하지 않았다.

## 4. 시험

- SQLite 하네스 + PostgreSQL 16(pgvector). PostgreSQL은 `EASYQ_TEST_ROLES=1`이면 v1.4 권한을 시험 스키마에 적용하고 auth · backend 요청과 worker 작업을 각자의 최소 권한 계정으로 실행한다.
- 시험 파일: `test_api`(v0.3), `test_v14_entry`, `test_questions`, `test_qa_worker`, `test_deletion`, `test_account`, `test_notices`, `test_admin`, 각 PostgreSQL 변형.

## 5. 남은 것

- 실제 외부 서비스 연결 확인(Claude · Gemini 임베딩 · Gemini Live STT · 학습서버 · SMTP · S3), docker compose 실제 기동(이 PC에 Docker 없음).
- 만료된 `auth_sessions` 행 정리 주체가 없다(worker 권한 없음, 검토 D-21). auth 쪽 정리가 필요하다.
- 예산 월 경계: worker `budget.month_bounds`는 UTC 월, 운영 지표(API-64)는 서울 월로 서로 다르다.
- 목표 기술 스택(SQLAlchemy async · Alembic · uv · pytest) 전환은 아직이다.
