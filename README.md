# EasyQ 서비스 백엔드 (auth · backend · worker)

강연 중 청중 질문에 교안 · 발화 근거로 AI가 답하고, 근거가 부족하면 강연자에게 넘기는 서비스 **EasyQ**의 백엔드 레포입니다.
frontend · caddy · trainer(학습서버)는 이 레포의 범위가 아닙니다. 공용 검색 모듈 `retrieval`은 [AI 레포](https://github.com/EasyQ-P/AI)에서 태그로 고정해 설치합니다.

- 담당: 신재우 · 조현준 (AI 연계: 최의찬 · 엄윤종, 화면: 심주혜)
- 작업 규칙: [CONTRIBUTING.md](CONTRIBUTING.md) · 개발 기준: [CLAUDE.md](CLAUDE.md)

## 현재 진행 상황 (2026-11-25 기준)

기준 설계는 **백엔드 v1.2 반영 설계서**다. 설계 v1.3 작업본은 팀 회의에서 반려되어 되돌렸다.

`backend/easyq/`에 **v0.3 프로토타입**이 있습니다. 설계서 10.2의 목표 구조(`auth/`, `backend/app/…`, SQLAlchemy · Alembic · uv)와는 아직 다르며, 옮길 때는 팀에 먼저 알리고 진행합니다.

| 상태 | 뜻 |
|---|---|
| ✅ | 구현 · 오프라인 테스트 통과 |
| △ | 일부만 구현 |
| ❌ | 미구현 |

### v0.3 (12/2 수) 항목별 상태

| 구분 | 항목 | 담당 | 상태 | 비고 |
|---|---|---|---|---|
| 공통 | 회원가입 · 이메일 인증 | 신재우 | ✅ | API-01~03, 메일 worker |
| 공통 | 로그인 · 로그아웃 · 비밀번호 재설정 | 신재우 | ✅ | API-04~08, CSRF 복구 API-06 |
| 공통 | 동의 수집 · 기록 | 조현준 | ✅ | API-11 · 20, 가입 · 입장 시 기록 |
| 공통 | 음성 원본 미저장 | 신재우 | ✅ | 음성은 메모리에서 STT로만 중계, 저장 안 함 |
| 강연자 | 강연 생성 | 조현준 | ✅ | API-13 |
| 강연자 | 입장 QR 발급 · 재발급 | 신재우 | ✅ | API-13 · 17 · 18 |
| 강연자 | 강연 상태 관리 | 조현준 | ✅ | API-15 · 16, 12시간 자동 종료 |
| 교안 | 교안 업로드 | 조현준 | ✅ | API-21, PDF/PPTX 검증 |
| 교안 | 텍스트 추출 · 청킹 | 조현준 | △ | 800자/100자 겹침 유지. AI 레포 retrieval 태그가 나오면 교체 |
| 교안 | 임베딩 · 색인 | 조현준 | ✅ | Gemini 768차원 + Kiwi tsvector (실제 제공자 미검증) |
| 교안 | 전문용어 사전 | 조현준 | ✅ | API-25, Kiwi 규칙 기반 용어 추출(TERMS 단계), STT 힌트 |
| 교안 | 교안 처리 상태 표시 | 조현준 | ✅ | API-22 · 23 · 41, WebSocket `document.updated` |
| 실시간 음성 | 마이크 연결 · 음성 전송 | 신재우 | ✅ | WebSocket `/api/v1/ws/sessions/{sid}` audio.* |
| 실시간 음성 | 실시간 STT 중계 | 신재우 | △ | Gemini Live 어댑터 작성, 실제 제공자 연결은 미검증 |
| 실시간 음성 | 발화 조각 색인(30초) | 신재우 | ✅ | 30초 묶음 + `TRANSCRIPT_INDEX` 작업 |
| 강연자 | 내 강연 목록 | 조현준 | ✅ | API-14(hosted · joined) |
| 청중 | 청중 입장(회원 / 게스트) | 신재우 | ✅ | API-19, INT-01 게스트 세션 |
| 관리자 | 관리자 로그인 | 신재우 | ✅ | ADMIN 생성 명령 + API-04 로그인 + 권한 확인 |

### 최근 수정 (v0.3 검토)

- 다른 강연의 게스트 쿠키가 남아 있으면 새 강연에 입장하지 못하던 문제 수정
- `Idempotency-Key`를 참가자 ID로 그대로 써서 다른 게스트 참가를 가로챌 수 있던 문제 수정
- Caddy 뒤에서 모든 요청이 같은 IP로 보여 IP별 요청 한도가 서비스 전체에 걸리던 문제 수정(`--proxy-headers`, `FORWARDED_ALLOW_IPS`)
- 강연 SERVICE 동의를 철회한 회원이 다시 입장할 때 UNIQUE 위반(503)이 나던 문제 수정 — 재입장 시 `service_consent: true` 필요

### 알려진 남은 문제

- PDF 파서 `strict=True`, 빈 비밀번호 암호화 PDF 거부
- 강연 조회 GET에서도 행 잠금(`FOR UPDATE`), 업로드 중 강연 행 잠금 상태로 S3 업로드
- DB 커넥션 풀 없음(트랜잭션마다 새 연결)
- 실제 FastAPI 기동 · SMTP · S3 · Gemini(임베딩 · STT) · Kiwi 연결은 아직 검증하지 않음(PostgreSQL 16 + pgvector는 로컬에서 검증)
- `DB 스키마파일/easyq_schema_v1.2.sql`은 PostgreSQL 적용을 위해 UNIQUE · 인덱스 블록만 FK 앞으로 옮김(내용 변경 없음)
- 설계서 v1.2의 `documents.sha256`(NOT NULL)이 v1.2 SQL에는 없어 저장하지 않음 — 팀 확인 필요

## 앞으로 구현할 내용 (진도표 v6)

| 태그 | 날짜 | 진척 | 백엔드 범위 |
|---|---|---|---|
| v0.3 | 12/2(수) | 30% | 남은 것: 청킹 교체(AI 레포 태그 대기), 실제 STT · 임베딩 연결 확인 |
| v0.4 | 12/3(목) | 40% | 하이브리드 검색, 대체 판정(LLM_ONLY), AI 답변 생성 · 전달, 2차 근거 확인, 인용 검증, 인식 상태 표시 · 일시정지, 질문 입력 · 유사 질문 확인, 질문 제출 |
| v0.5 | 12/4(금) | 50% | 판정 모델 판정, 처리 기록(decision_snapshot), 판정 추론 연결, 개인 AI 답변 보기 · 평가, 공개 질문 목록, 공감, 관리자 회원 관리 · 강연 관리 · 강제 종료 |
| v0.7 | 12/7(월) | 70% | 학습 후보 익명 처리, 라벨 후보 수집, 라이브 대시보드, 답변 완료 처리, AI 답변 확인 · 철회, 필기, 운영 지표, 서빙 전환 · 롤백, 데이터셋 현황, 학습 결과 확인, 평가용 교안 조각 수신 · 겹침 검사 |
| v0.9 | 12/8(화) | 90% | 계정 관리 · 탈퇴, 보관 기간 후 자동 파기, 탈퇴 시 개인 기록 삭제, 요약 노트 생성 · 열람, 지난 강연 조회 · 삭제, 강연 데이터 직접 삭제, 강연 리포트 · 통계, 내 보관함 목록 · 상세 · 삭제, 라벨 검수, 재학습 실행 · 중단, 버전 비교 · 배포 판단, 공지 · 문의 관리 |
| v1.0 | 12/9(수) | 100% | 통합 확인 · 버그 수정만 |

## 레포 구성

```
backend/                 v0.3 프로토타입 (easyq/ 패키지, tests/, migrations/, docs/)
DB 스키마파일/            easyq_schema_v1.2.sql (서비스 DB 기준), 학습서버 레지스트리 SQL
설계서/                   원본 설계서 (HWP)
docs/                    백엔드 v1.2 반영 설계서(HWPX), 팀 공유 보류사항 · 유스케이스 · Mermaid 원본
구현 전도표/              진도표 v6 (HWPX)
CLAUDE.md, CONTRIBUTING.md, .github/
```

## 실행 · 시험 (프로토타입)

```powershell
cd backend
python -m pip install -r requirements.txt
python -m unittest tests.test_api
```

오프라인 테스트는 SQLite 하네스와 가짜 외부 연동으로 실제 서비스 · worker 코드를 실행합니다(현재 68개 통과, PostgreSQL 테스트 70개 통과). 실제 PostgreSQL 테스트는 `EASYQ_TEST_DATABASE_URL`을 설정해야 실행됩니다. 자세한 절차는 [backend/docs/runbook.md](backend/docs/runbook.md), 구현 설명은 [backend/docs/implementation-v0.3.md](backend/docs/implementation-v0.3.md)를 참고하세요.
