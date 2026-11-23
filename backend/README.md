# EasyQ backend v0.3

최종 HWP 설계서와 진도표의 **v0.3 / 신재우·조현준 담당 10개 항목**을 구현한 소스입니다. 원본 설계서와 PostgreSQL SQL은 수정하지 않았습니다.

| 문서 | 내용 |
| --- | --- |
| [구현 설명서](docs/implementation-v0.3.md) | 범위, API, 기능별 처리 과정, DB 통신, 코드 선택 이유, 한계 |
| [실행 안내](docs/runbook.md) | DB 초기화 순서, 환경변수, auth/backend/worker 실행, 테스트 |
| [검증 결과](docs/test-results.json) | 자동 생성된 개별 테스트 결과. 실제 PostgreSQL 검증 여부 포함 |

auth와 backend는 FastAPI 앱이고, worker는 같은 패키지의 별도 실행 프로세스입니다. PostgreSQL 16 + pgvector를 사용하며 애플리케이션이 테이블을 자동 생성하지 않습니다. SMTP, S3, Gemini 설정은 환경변수로 주입합니다.

```powershell
cd backend
python -m pip install -r requirements.txt
python scripts/run_tests.py
```

실제 DB·외부 제공자 연결에는 실행 안내의 설정이 필요합니다. 오프라인 테스트 통과는 PostgreSQL·SMTP·Gemini·S3 실연결 성공을 뜻하지 않습니다.
