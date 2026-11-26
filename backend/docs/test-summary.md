# v0.3 자동 검증 결과

- 오프라인 API·처리·WebSocket 테스트(SQLite 하네스): 68개 통과
- 실제 PostgreSQL 16.2 + pgvector 테스트: 70개 통과
- 실패: 0개, 오류: 0개, skip: 0개 (2026-11-26, 총 138개)

실행 근거는 test-results.json의 개별 테스트 결과입니다. 메일·임베딩·STT·스토리지는 가짜 어댑터를 썼습니다. FastAPI/Uvicorn 기동, SMTP·S3·Gemini(임베딩·STT)·Kiwi 실연결은 아직 검증하지 않았습니다.

DB 생성 후 실행 절차는 runbook.md, API별 동작은 api-v0.3.md, 구현 설명은 implementation-v0.3.md를 참고하세요.
