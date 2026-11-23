# EasyQ v0.8 변경 반영 기록

기준: 원래 v0.6 및 EasyQ_설계서_3파트_통합_최종.hwpx의 6.1~6.3, 7.1~7.3, 8.1~8.4. 이전 v0.7은 7.1만 일부 변경한 중간본으로 사용하지 않는다.

- 통합본: RDS 전환, 평가용 교안 7.4.28 동기화, 겹침 분모, 전체 이관 임계값, train→export→eval→register, 3개월 고정 보관, 학습 config_override, 데이터셋 버전 필드, 모델·평가 필드명, evidence_id 중복 키, 운영 지표·로그·오류 대응 반영.
- 회원가입: 한 번의 이메일 인증. users.email_verified_at 및 PENDING_VERIFICATION, email_verifications와 auth_rate_limits 신설. 기존 API-01·02와 가입·로그아웃 흐름 수정, API-49·50 추가. 로그인 때 재인증 없음.
- 개인 메모: question_notes 신설. API-38 확장, API-51·52·53 추가. 작성자만 접근하며 신규 메모는 최종 미답변 상태만 허용. 질문 상태·공개 목록·학습·요약과 분리. 삭제와 보관 만료 연동.
- 인증 분리: frontend·auth·backend·worker 4개 및 외부 RDS. auth 공개 경로 분기, INT-01 게스트 세션과 INT-02 세션 폐기, RDS 권한 조회와 WS 폐기 이벤트, SMTP 작업·비밀 설정 반영.
- SSH: EC2 호스트 서비스의 포트 전달로 학과 HTTP API 호출. SSH 전용 컨테이너 추가 없음.
- 공개 REST 52개, 내부 업무 API 2개, 내부 health 및 WS는 별도. API-46 폐기 유지.

본문과 API·DB 표는 Markdown 원본에, 수정된 그림은 Mermaid 원본 및 diagrams/source에 보관한다. 제출용 Word v0.8은 같은 원본에서 생성한다.
