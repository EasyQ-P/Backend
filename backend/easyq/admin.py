"""ADMIN account creation command (7.1.1 · 8.2.2: ADMIN은 서버 초기 생성 명령으로만 만든다).

    python -m easyq.admin create --email admin@example.com --display-name 관리자

Run on the server with the auth DB role (DATABASE_URL) and the usual auth settings. The password is read
from the EASYQ_ADMIN_PASSWORD environment variable or asked interactively; it is never a command-line
argument, so it does not end up in shell history or process lists. Web APIs never grant or change roles;
the admin then logs in with the normal login API (API-04).
"""
import argparse
import getpass
import os
import sys

from .contracts import APIError, email, now, text, uid
from .security import password_hash, valid_password


def create_admin(db, address, display_name, password, clock=now):
    address, display_name = email(address), text(display_name, 50)
    if not valid_password(password):
        raise APIError(422, 'VALIDATION_ERROR')
    with db.transaction() as tx:
        tx.lock('email:' + address)
        if tx.one('SELECT user_id FROM users WHERE email=%s', (address,)):
            raise APIError(409, 'EMAIL_ALREADY_EXISTS')   # existing accounts are never promoted
        user_id, moment = uid(), clock()
        tx.execute("""INSERT INTO users(user_id,email,password_hash,display_name,role,status,email_verified_at,created_at,updated_at)
            VALUES(%s,%s,%s,%s,'ADMIN','ACTIVE',%s,%s,%s)""", (user_id, address, password_hash(password), display_name, moment, moment, moment))
    return user_id


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m easyq.admin')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create', help='create an ADMIN account')
    create.add_argument('--email', required=True)
    create.add_argument('--display-name', required=True)
    args = parser.parse_args(argv)
    password = os.environ.get('EASYQ_ADMIN_PASSWORD') or getpass.getpass('ADMIN password (8~20, letters and digits): ')
    from .config import Settings
    from .db import Database
    settings = Settings.from_env()
    try:
        user_id = create_admin(Database(settings.database_url), args.email, args.display_name, password)
    except APIError as exc:
        print('failed: ' + exc.code, file=sys.stderr)
        return 1
    print('created ADMIN ' + user_id)
    return 0


if __name__ == '__main__':
    sys.exit(main())
