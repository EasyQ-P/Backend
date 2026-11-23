# v0.3 자동 검증 결과

- 오프라인 API·처리 테스트: 48개 통과
- 실패: 0개, 오류: 0개
- 실제 PostgreSQL 테스트: 50개 skip (DB DSN 미설정)
- 실행 시간: 10.625초

실행 근거는 test-results.json의 개별 테스트 결과입니다. ASGI 요청 처리와 실제 업무/워커 코드, 테스트용 SQLite SQL 어댑터를 사용했습니다. 실제 PostgreSQL·FastAPI/Uvicorn 기동·SMTP·S3·Gemini·Kiwi는 실연결 검증하지 못했습니다.

DB 생성 후 실행 절차는 runbook.md, API별 동작과 코드 선택 이유는 implementation-v0.3.md를 참고하세요.
