"""LOCAL DEVELOPMENT test accounts (compose.dev.yaml `seed` service). Never for shared or production DBs.

Creates, only if missing:
* a verified MEMBER (no verification mail needed) with SERVICE consent, and
* an ADMIN, through the same path as the server command (easyq.admin.create_admin).

Refuses to run unless EASYQ_DEV_SEED=1 and PUBLIC_ORIGIN is a localhost origin. Credentials are fixed
development values, overridable with DEV_MEMBER_EMAIL / DEV_MEMBER_PASSWORD / DEV_ADMIN_EMAIL / DEV_ADMIN_PASSWORD.
"""
import os
import sys
from urllib.parse import urlparse

from .admin import create_admin
from .contracts import APIError, dumps, email, now, text, uid
from .security import password_hash, valid_password

DEFAULTS = {
    'DEV_MEMBER_EMAIL': 'member@easyq.local', 'DEV_MEMBER_PASSWORD': 'easyq2026', 'DEV_MEMBER_NAME': '테스트회원',
    'DEV_ADMIN_EMAIL': 'admin@easyq.local', 'DEV_ADMIN_PASSWORD': 'admin2026', 'DEV_ADMIN_NAME': '테스트관리자',
}


def create_member(db, address, display_name, password, service_policy='service-v1', training_policy='training-v1', clock=now):
    """Verified MEMBER as if sign-up (API-01) and e-mail verification (API-03) had completed."""
    address, display_name = email(address), text(display_name, 50)
    if not valid_password(password):
        raise APIError(422, 'VALIDATION_ERROR')
    with db.transaction() as tx:
        tx.lock('email:' + address)
        if tx.one('SELECT user_id FROM users WHERE email=%s', (address,)):
            raise APIError(409, 'EMAIL_ALREADY_EXISTS')
        user_id, moment = uid(), clock()
        tx.execute("""INSERT INTO users(user_id,email,password_hash,display_name,role,status,email_verified_at,created_at,updated_at)
            VALUES(%s,%s,%s,%s,'MEMBER','ACTIVE',%s,%s,%s)""", (user_id, address, password_hash(password), display_name, moment, moment, moment))
        for purpose, version, granted in (('SERVICE', service_policy, True), ('TRAINING', training_policy, False)):
            tx.execute("""INSERT INTO consents(consent_id,user_id,purpose,policy_version,granted,recorded_at)
                VALUES(%s,%s,%s,%s,%s,%s)""", (uid(), user_id, purpose, version, granted, moment))
    return user_id


def check_development(environ):
    if environ.get('EASYQ_DEV_SEED') != '1':
        raise SystemExit('refusing: set EASYQ_DEV_SEED=1 (local development only)')
    host = urlparse(environ.get('PUBLIC_ORIGIN', '')).hostname
    if host not in ('localhost', '127.0.0.1') and not (host or '').endswith('.localhost'):
        raise SystemExit('refusing: PUBLIC_ORIGIN must be a localhost origin for development seed data')


def main(environ=os.environ):
    check_development(environ)
    from .config import Settings
    from .db import Database
    settings = Settings.from_env()
    db = Database(settings.database_url)
    value = lambda key: environ.get(key) or DEFAULTS[key]
    results = {}
    for kind, create in (('member', lambda: create_member(db, value('DEV_MEMBER_EMAIL'), value('DEV_MEMBER_NAME'), value('DEV_MEMBER_PASSWORD'),
                                                         settings.service_policy_version, settings.training_policy_version)),
                         ('admin', lambda: create_admin(db, value('DEV_ADMIN_EMAIL'), value('DEV_ADMIN_NAME'), value('DEV_ADMIN_PASSWORD')))):
        try:
            create()
            results[kind] = 'created'
        except APIError as exc:
            if exc.code != 'EMAIL_ALREADY_EXISTS':
                print(dumps({'event': 'devseed.failed', 'kind': kind, 'code': exc.code}), file=sys.stderr)
                return 1
            results[kind] = 'exists'
    # Only outcome per account kind; never e-mail addresses or passwords in logs.
    print(dumps({'event': 'devseed.done', **results}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
