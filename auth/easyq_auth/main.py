"""auth container entry point.  uvicorn --factory easyq_auth.main:create_app"""
from easyq_common.http import build_app

ROUTES = [
    ('POST', '/users', 'API-01', '회원가입'),
    ('POST', '/auth/email-verifications', 'API-02', '가입 인증 메일 재발송'),
    ('POST', '/auth/email-verifications/confirm', 'API-03', '이메일 인증'),
    ('POST', '/auth/sessions', 'API-04', '로그인'),
    ('DELETE', '/auth/sessions/current', 'API-05', '로그아웃'),
    ('POST', '/auth/csrf-tokens', 'API-06', 'CSRF 복구'),
    ('POST', '/auth/password-reset-requests', 'API-07', '비밀번호 재설정 메일 요청'),
    ('POST', '/auth/password-resets', 'API-08', '비밀번호 재설정'),
    ('GET', '/me', 'API-09', '현재 회원 조회: 로그인 복구 지원'),
]


def create_app(service=None):
    if service is None:
        from easyq_common.auth_client import AuthClient
        from easyq_common.config import Settings
        from easyq_common.db import Database
        from easyq_common.storage import storage_from
        from .service import AuthService
        settings = Settings.from_env()
        service = AuthService(Database(settings.database_url), settings, storage_from(settings), AuthClient(settings))
    return build_app('auth', service, ROUTES)
