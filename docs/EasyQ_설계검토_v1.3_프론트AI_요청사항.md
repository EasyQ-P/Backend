# EasyQ 설계 검토 v1.3 — 프론트엔드 · AI 요청 사항

백엔드 설계 검토(설계서 6.1~8.4) 결과 중 **다른 파트의 변경이나 확인이 필요한 것**만 모았다.
백엔드만으로 끝나는 수정은 이미 반영했다.

- 설계서 작업본: `docs/CodingAnt팀_설계서_v1.0_백엔드v1.3작업본.hwpx`
- DB: `DB 스키마파일/easyq_schema_v1.3.sql`, `easyq_grants_v1.3.sql`

---

## 1. 프론트엔드 (심주혜)

### 1-1. 수정이 필요한 것

| # | 화면 · API | 바뀐 점 | 프론트 할 일 | 크기 |
|---|---|---|---|---|
| F1 | 강연 준비 · 대시보드 / API-24 교안 삭제 | 강연 중(LIVE)에도 **조각이 없는 FAILED 교안**은 지울 수 있다 | LIVE에서 FAILED 교안에 [삭제] 버튼을 보여 준다. 이 버튼으로 "삭제 후 새 파일 업로드"를 할 수 있다 | 소 |
| F2 | 교안 처리 상태 / API-22, `document.updated` | 예산 상한으로 보류된 교안은 `status = PENDING`, `error_code = BUDGET_EXCEEDED`다(FAILED가 아니다). 요약 노트는 QUEUED로 남는다 | PENDING이면서 error_code가 BUDGET_EXCEEDED인 교안에는 "예산 한도로 처리가 보류되었어요"를 보여 준다 | 소 |
| F3 | 계정 관리 / API-10 비밀번호 변경 | 현재 비밀번호 확인 실패는 계정당 15분 10회로 제한한다 | `429 RATE_LIMITED`가 오면 `Retry-After`로 남은 시간을 안내한다 | 소 |
| F4 | 강연자 대시보드 / WebSocket | 로그인 후 24시간 상한 때문에 강연 중에 강연자 세션이 만료될 수 있다. 이때 음성 연결은 `1008`로 닫히고 REST는 401을 돌려준다 | 다시 로그인하도록 안내하고, 로그인한 뒤 같은 강연 대시보드로 돌아가 음성 전송을 다시 시작한다 | 중 |
| F5 | 공통 / API-06 CSRF 재발급 | auth 장애 중에는 새로고침한 탭이 CSRF 토큰을 다시 받지 못한다(`503 AUTH_SERVICE_UNAVAILABLE`). 조회와 WebSocket 수신은 계속 된다 | API-06이 503이면 "인증 서버 점검 중, 조회만 가능해요"를 보여 주고 변경 버튼을 잠시 비활성화한다. 주기적으로 다시 시도한다 | 소 |
| F6 | 강연 시작 / API-16 | 강연자가 이 강연의 SERVICE 동의를 철회했다면(API-20), DRAFT 강연을 시작할 때 `403 CONSENT_REQUIRED`가 온다 | 403 CONSENT_REQUIRED를 받으면 동의를 다시 받고(API-20) 시작을 재시도한다 | 소 |
| F7 | 지난 강연 · 준비 화면 / API-40 SESSION | LIVE 강연의 원자료 삭제는 `409 SESSION_STATE_CONFLICT`다. DRAFT · ENDED · PURGED에서는 삭제할 수 있다 | LIVE에서는 삭제 버튼을 숨긴다. (선택) 준비 화면에 DRAFT 강연 삭제 버튼을 둔다 | 소 |
| F8 | 관리자 계정 / API-40 ACCOUNT | ADMIN 계정은 웹에서 탈퇴할 수 없다(403 FORBIDDEN) | 관리자 계정 화면에서 탈퇴 버튼을 숨긴다 | 소 |
| F9 | 공통 / 비동기 POST(API-21 · 23 · 24 · 27 · 40 · 55 · 57) | 같은 Idempotency-Key에 다른 본문을 보내면 `409 IDEMPOTENCY_CONFLICT`다 | 키는 요청마다 새로 만들고, **같은 요청을 재시도할 때만** 같은 키를 다시 쓴다 | 소 |
| F10 | 질문 상태 표시 | 질문 상태에서 `FAILED`를 뺐다. 실패는 모두 ESCALATED로 끝난다 | FAILED 분기 · 문구가 있으면 지운다 | 소 |
| F11 | 목업 데이터 | 7.3 요청 · 응답 예시의 상태값을 실제 값으로 고쳤다(`"ACTIVE"` → QUEUED · LIVE 등, `"VALUE"` · `"version"` 자리표시자 제거) | 예시를 복사해 만든 목업이 있으면 갱신한다 | 소 |

### 1-2. 확인만 하면 되는 것

| # | 내용 | 확인할 것 |
|---|---|---|
| C1 | API-01 회원가입의 CONSENT_REQUIRED가 **422에서 403**으로 바뀌었다(8.4.2 기준) | `error.code`로 분기하고 있으면 바꿀 것이 없다. HTTP 상태로 분기하고 있으면 고친다 |
| C2 | 게스트 재입장(H8): backend가 `easyq_join_retry` HttpOnly 쿠키(입장 후 24시간)를 함께 내려 준다 | 브라우저가 알아서 처리하므로 바꿀 것이 없다. fetch의 `credentials`가 쿠키를 보내는 설정인지만 확인한다 |
| C3 | 유사 질문 검색(API-26)은 ESCALATED 질문만 돌려준다 | 기존 명세 그대로다. 6.2.6 문구만 고쳤다 |
| C4 | 입장 코드 오입력 한도가 같은 IP 15분 **20회에서 100회**로 바뀌었다 | 화면 변경 없음 |

### 1-3. 결정 대기(바뀌면 수정 필요)

- **API-63 서빙 이력 응답 필드명:** `requested_by`(내부 회원 ID)를 응답에 그대로 내보내고 있고, `requested_mode` · `route` · `requested_model_version`은 DB 이름(mode · operation · model_version)과 다르다. 바꾸게 되면 관리자 서빙 화면을 수정해야 한다.

---

## 2. AI (최의찬 · 엄윤종)

### 2-1. 학습서버 변경이 필요한 것

| # | API · 대상 | 요청 | 이유 |
|---|---|---|---|
| A1 | 7.4.22 서빙 전환(PUT /deployment), 7.4.23 롤백(POST /rollbacks) | `request_id`(= 서비스 deployment_id)를 받아, 같은 request_id는 **한 번만 적용**하고 같은 결과를 돌려준다. `reg_deployments`에 request_id 컬럼과 UNIQUE를 둔다 | backend가 응답을 받지 못해 다시 보내면 `target: "previous"` 롤백이 두 번 적용될 수 있다 |
| A2 | 7.4.22 입력 | `reason`을 받는다 | `reg_deployments.reason`은 NOT NULL인데 7.4.22에는 reason 입력이 없다 |
| A3 | 7.4.13 평가 결과 형식 | 평가 한 건에 들어 있는 평가자(CUSTOM · SIMILARITY · PRETRAINED · SONNET_ONLY) · 범위(QUESTION · PAIR · SERVICE) · 분할(VALIDATION · TEST)별 결과를 구분할 수 있게 응답 구조를 확정해 준다 | 서비스는 (external_evaluation_id, evaluator, scope, split)마다 한 행으로 나눠 저장하고 UNIQUE로 중복을 막는다(8.2.26) |

### 2-2. 레지스트리 SQL 정합(학습서버 내부, 권장)

- `reg_jobs.status` · `stage`에 CHECK 제약이 없다. 서비스 쪽은 QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/HELD, train/export/eval/register로 막는다.
- `reg_deployments`는 LLM_ONLY일 때 serving_version을 NULL로 강제하지 않는다. 서비스 deployments는 CHECK로 강제한다.

### 2-3. 공유(학습서버 변경 없음)

- **컬럼 길이:** 서비스 복사본의 길이를 레지스트리 기준으로 맞췄다. retrieval_config_version 50, course_id 100, 학습 job_id · evaluation_id 30, hold_reason 40.
- **학습 중 강연 시작(M4):** 학습 중에 강연을 시작하는 것은 막지 않는다. 그동안 판정 추론이 800 ms를 넘긴 질문은 그 질문만 LLM_ONLY로 처리된다. 학과 서버에서 직접 학습 · 평가 · 변환할 때도 강연이 없는 시간에 해 달라.
- **라벨 후보(L4):** 유효 인용이 0개여서 이관한 질문은 `escalation_reason = PROVIDER_FAILURE`이고 라벨 후보를 만들지 않는다. SECOND_STAGE_INSUFFICIENT 후보의 정의는 그대로다.
- **미뤄 둔 후보(M12):** 평가용 강의 검사가 PENDING인 동안 생긴 사건은, 검사가 끝난 뒤 한꺼번에 후보로 만들어진다. 후보가 시간 순서와 다르게 들어올 수 있다.
- **복사본 동기화(M9):** SYNC 작업이 1시간마다, 그리고 관리자 화면을 조회할 때 7.4.3 · 7.4.4 · 7.4.13 · 7.4.17 · 7.4.18을 부른다. 호출 빈도가 늘어난다.
- **시각 형식:** 7.4는 `+09:00`, 서비스 DB는 UTC다. 서비스가 변환하므로 바꿀 것이 없다.
