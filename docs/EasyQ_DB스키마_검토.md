# EasyQ DB 스키마 검토 — 팀 결정 필요 사항

- 대상: `DB 스키마파일/easyq_schema_v1.2.sql` (666줄, 테이블 33개), `migrations/002_runtime_grants.example.sql`
- 기준: 설계서 백엔드 v1.2 반영본 8.2
- 작성: 2026-11-26, 백엔드
- 상태: **아무것도 고치지 않음.** CLAUDE.md 규칙(스키마 변경은 먼저 알림)에 따라 결정을 받은 뒤 반영한다.

## 요약

| # | 구분 | 내용 | 영향 시점 | 제안 |
|---|---|---|---|---|
| 1 | **버그** | 질문이 페이지를 가리키는 교안은 삭제할 수 없음 | v0.9 교안 · 강연 삭제, 보관 기간 파기 | FK가 `page_no`도 함께 비우게 변경 |
| 2 | 설계 | `documents`에 파일 해시 컬럼 없음 | 교안 업로드 중복 판별 | `sha256 CHAR(64)` 추가 여부 결정 |
| 3 | 설계 | `label_reviews.evidence_id`가 강연 확인 없는 단일 FK | v0.9 라벨 검수 | 8.2.35 예외인지 확인, 아니면 복합 FK |
| 4 | 설계 | `ws_events`에 `(generation_id, sequence)` UNIQUE 없음 | v0.4 답변 스트리밍 | 부분 UNIQUE 추가 여부 결정 |
| 5 | 설계 | 검수 기록이 있는 관리자는 삭제 불가 | v0.5 회원 관리, v0.9 탈퇴 | ON DELETE 동작을 보관 정책과 함께 결정 |
| 6 | 권한 | worker 실행 계정 권한이 넓음(우리 쪽 파일) | 운영 배포 | 필요한 컬럼만 허용하도록 축소 |

이미 해결한 것: 원본 스키마는 복합 FK가 대상 UNIQUE보다 먼저 나와 PostgreSQL 적용이 502행에서 실패했다. UNIQUE · 인덱스 블록을 FK 앞으로 옮겨 해결했다(내용 변경 없음, PostgreSQL 16.2 + pgvector에서 적용 확인).

---

## 1. 교안 삭제가 CHECK 위반으로 실패 (버그)

관련 정의:

```sql
-- 554행
ALTER TABLE questions ADD FOREIGN KEY (session_id, document_id)
  REFERENCES documents (session_id, document_id) ON DELETE SET NULL (document_id);
-- 650행
ALTER TABLE questions ADD CHECK (... AND (page_no IS NULL OR (document_id IS NOT NULL AND page_no > 0)));
```

교안을 지우면 FK가 질문의 `document_id`만 NULL로 만든다. 그런데 그 질문에 `page_no`가 남아 있으면 CHECK가 `document_id`를 요구하므로 삭제 전체가 실패한다. 질문이 교안 페이지를 근거로 답변된 강연이라면 교안을 지울 수 없게 된다.

대안:
- **A (제안).** FK를 `ON DELETE SET NULL (document_id, page_no)`로 바꾼다. 교안이 사라지면 페이지 번호도 의미가 없으므로 함께 비우는 것이 자연스럽다. PostgreSQL 15 이상에서 지원한다.
- B. 스키마는 두고, 교안을 지우기 전에 앱에서 `UPDATE questions SET page_no = NULL ...`을 먼저 실행한다. 파기 순서(6.2.11)에 단계가 하나 늘고, 빠뜨리면 같은 오류가 난다.

## 2. `documents`에 파일 해시가 없음

`documents`에는 `file_name, object_key, media_type, size_bytes`만 있다. 같은 파일을 다시 올렸는지, 저장된 객체가 업로드 원본과 같은지 서버가 확인할 방법이 없다.

결정할 것: 업로드 중복 판별이나 무결성 확인이 필요한가?
- 필요하면 `sha256 CHAR(64)` 컬럼을 추가한다(설계서 8.2 표도 함께 수정).
- 필요 없으면 현행을 유지한다. 현재 코드는 해시를 쓰지 않는다.

## 3. `label_reviews.evidence_id` FK가 같은 강연인지 확인하지 않음

```sql
-- 569행
ALTER TABLE label_reviews ADD FOREIGN KEY (evidence_id) REFERENCES knowledge_chunks (chunk_id) ON DELETE SET NULL;
```

8.2 공통 규칙은 강연 소속 행을 `(session_id, 참조 ID)` 복합 FK로 묶는다. `label_reviews`에는 `session_id`가 없어서, 검수자가 다른 강연의 조각을 근거로 지정해도 DB가 막지 못한다.

결정할 것:
- 8.2.35 예외 목록에 들어 있으면 현행을 유지하고, 앱에서 후보의 강연과 조각의 강연이 같은지 검사한다.
- 예외가 아니면 `session_id`를 추가하고 복합 FK로 바꾼다.

## 4. `ws_events`에 `(generation_id, sequence)` UNIQUE가 없음

`ws_events`의 키는 `event_id`(IDENTITY) 하나다. worker가 재시도하면 같은 답변 생성의 같은 순번 `answer.delta`가 두 번 저장될 수 있고, 클라이언트는 중복 조각을 받는다.

제안: `CREATE UNIQUE INDEX ... ON ws_events (generation_id, sequence) WHERE generation_id IS NOT NULL;`
대안: 앱에서 `ON CONFLICT` 없이 미리 조회하고 넣는다. 동시 실행이 있으면 경쟁 상태가 생긴다.

## 5. 검수 기록이 있는 관리자는 삭제할 수 없음

```sql
-- 568행
ALTER TABLE label_reviews ADD FOREIGN KEY (reviewer_user_id) REFERENCES users (user_id);
```

ON DELETE 동작이 없어(기본값 NO ACTION) 검수 기록이 있는 관리자 계정은 삭제가 실패한다. 반면 `audit_events.actor_user_id`, `training_runs.requested_by` 등은 `SET NULL`이다.

결정할 것(보관 정책과 함께):
- 검수 기록을 남기고 검수자만 지운다 → `reviewer_user_id`를 NULL 허용으로 바꾸고 `SET NULL`. 단, 이 컬럼은 PK `(candidate_id, reviewer_user_id)`에 포함되어 있어 PK도 바꿔야 한다.
- 관리자는 삭제하지 않고 비활성화만 한다 → 현행 유지, 운영 규칙에 명시.

## 6. worker 실행 계정 권한이 넓음

이 항목은 설계 스키마가 아니라 우리 쪽 `migrations/002_runtime_grants.example.sql`의 문제다.

```sql
GRANT SELECT,UPDATE ON email_verifications,password_resets TO easyq_worker;  -- 25행
GRANT SELECT,DELETE ON users TO easyq_worker;                                 -- 26행
```

메일 발송(암호화된 발송 정보 읽기 · 발송 결과 기록)과 파기 작업 때문에 준 권한이지만, 이 테이블들에는 인증 코드 · 재설정 토큰 검증값이 들어 있다. CLAUDE.md는 이 값들을 auth만 읽고 쓰도록 정한다.

제안: 컬럼 단위 권한으로 줄인다. 예를 들어 worker에는 발송에 필요한 컬럼과 발송 상태 컬럼만 허용하고, 검증값 컬럼은 허용하지 않는다. 컬럼 목록은 메일 작업(`worker/easyq_worker/mail.py`) 기준으로 정리해 다시 공유한다.

---

## 함께 남아 있는 코드 쪽 항목 (스키마 변경 불필요)

- API-23 교안 재처리의 멱등 키에 `document_id`가 들어감 — 설계서 키 구성과 다시 맞출 것
- API-14 내 강연 목록의 참여 강연에 REVOKED 참가가 보임
- WebSocket 권한 재확인 쿼리가 `FOR UPDATE`로 행을 잠금 — 읽기만 하도록 변경 예정
- `MERGE_RECONCILIATION_REQUIRED` 오류 코드가 8.4.2 표에 없음 — 표에 추가할지, 기존 코드로 바꿀지 결정 필요
