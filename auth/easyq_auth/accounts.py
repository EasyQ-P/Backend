"""Accounts (API-01~03, 04, 07, 08, 09 data): sign-up, verification, login, password reset."""
from datetime import timedelta
import hmac
import re
import secrets

from easyq_common.contracts import (APIError, Result, consent, dt, email, fields,
                                    text, uid)
from easyq_common.security import (credential_hash, encrypt, password_hash,
                                   password_matches, token, valid_password)


def deliverable_email(value):
    """API-01 address check with the rules fastapi-mail applies when the worker sends (email-validator):
    special-use domains such as .local/.test/.localhost are rejected up front instead of failing every mail job.
    Login and other endpoints keep the lenient email() check so existing accounts still work."""
    from email_validator import EmailNotValidError, validate_email
    address = email(value)
    try:
        validate_email(address, check_deliverability=False)
    except EmailNotValidError:
        raise APIError(422, 'VALIDATION_ERROR') from None
    return address


class AccountsMixin:
    def mail_rate(self, tx, address, ip):
        self.mail_ip_rate(tx, ip)
        self.mail_email_rate(tx, address)

    def mail_ip_rate(self, tx, ip):
        self.rate(tx, 'mail-ip', ip, 20)

    def mail_email_rate(self, tx, address):
        self.rate(tx, 'mail-email', address, 5, gap=60)

    def user_public(self, tx, user):
        data = {k: user[k] for k in ['user_id', 'email', 'display_name', 'role', 'status', 'email_verified_at']}
        data['consents'] = {}
        for purpose in ('SERVICE', 'TRAINING'):
            c = self.effective(tx, purpose, user=str(user['user_id']))
            data['consents'][purpose] = {'policy_version': c['policy_version'], 'granted': bool(c['granted'])} if c else None
        return data

    def queue_mail(self, tx, user, reset=False):
        table, idcol, hashcol, kind = ('password_resets', 'reset_id', 'token_hash', 'PASSWORD_RESET') if reset else ('email_verifications', 'verification_id', 'code_hash', 'EMAIL_VERIFICATION')
        current = self.clock()
        tx.execute(f"UPDATE {table} SET revoked_at=%s,delivery_ciphertext=NULL WHERE user_id=%s AND consumed_at IS NULL AND revoked_at IS NULL", (current, user['user_id']))
        tx.execute("""UPDATE jobs SET status='CANCELLED',finished_at=%s WHERE requested_by=%s
            AND job_type=%s AND status IN ('QUEUED','RUNNING')""", (current, user['user_id'], kind))
        rid = uid()
        raw = token() if reset else f'{secrets.randbelow(1000000):06d}'
        subject = 'reset' if reset else str(user['user_id'])
        hashed = credential_hash(self.settings.hmac_key, kind, subject, raw)
        tx.execute(f"""INSERT INTO {table}({idcol},user_id,{hashcol},key_id,delivery_ciphertext,expires_at)
            VALUES(%s,%s,%s,'v1',%s,%s)""", (rid, user['user_id'], hashed,
            encrypt(self.settings.encryption_key, raw), current + timedelta(minutes=30 if reset else 10)))
        self.job(tx, kind, rid, f'{kind}:{rid}', {}, user=str(user['user_id']))

    def register(self, tx, req):
        b = req.body
        fields(b, ['email', 'password', 'display_name', 'consents'], ['email', 'password', 'display_name', 'consents'])
        address, name = deliverable_email(b['email']), text(b['display_name'], 50)
        if not valid_password(b['password']):
            raise APIError(422, 'VALIDATION_ERROR')
        fields(b['consents'], ['SERVICE', 'TRAINING'], ['SERVICE'])
        service = consent(b['consents']['SERVICE'])
        training = consent(b['consents'].get('TRAINING', {'policy_version': 'training-v1', 'granted': False}))
        if not service['granted']:
            raise APIError(422, 'CONSENT_REQUIRED')
        # IP limit first (an existing-address probe still counts), then the duplicate check, then the
        # per-address 60-second/hourly limit, so a repeated sign-up gets EMAIL_ALREADY_EXISTS, not RATE_LIMITED.
        self.mail_ip_rate(tx, req.ip)
        tx.lock('email:' + address)
        if tx.one('SELECT user_id FROM users WHERE email=%s', (address,)):
            raise APIError(409, 'EMAIL_ALREADY_EXISTS', commit=True)
        self.mail_email_rate(tx, address)
        user_id = uid()
        tx.execute("INSERT INTO users(user_id,email,password_hash,display_name) VALUES(%s,%s,%s,%s)",
            (user_id, address, password_hash(b['password']), name))
        self.save_consent(tx, 'SERVICE', service, user=user_id)
        self.save_consent(tx, 'TRAINING', training, user=user_id)
        self.queue_mail(tx, {'user_id': user_id})
        return Result(201, {'user_id': user_id, 'status': 'PENDING_VERIFICATION', 'resend_available_at': self.clock() + timedelta(seconds=60)})

    def confirm(self, tx, req):
        fields(req.body, ['email', 'code'], ['email', 'code'])
        address, code = email(req.body['email']), req.body['code']
        if not isinstance(code, str) or not re.fullmatch('[0-9]{6}', code):
            raise APIError(422, 'VALIDATION_ERROR')
        self.rate(tx, 'confirm-ip', req.ip, 100, 900)
        user = tx.one('SELECT * FROM users WHERE email=%s FOR UPDATE', (address,))
        record = tx.one("""SELECT * FROM email_verifications WHERE user_id=%s
            AND consumed_at IS NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (user['user_id'],)) if user else None
        if not record or user['status'] != 'PENDING_VERIFICATION' or dt(record['expires_at']) <= self.clock():
            raise APIError(422, 'EMAIL_VERIFICATION_INVALID', commit=True)
        if record['attempt_count'] >= 5:
            raise APIError(429, 'EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED', commit=True)
        hashed = credential_hash(self.settings.hmac_key, 'EMAIL_VERIFICATION', str(user['user_id']), code)
        if not hmac.compare_digest(hashed, record['code_hash']):
            attempts = record['attempt_count'] + 1
            tx.execute('UPDATE email_verifications SET attempt_count=%s WHERE verification_id=%s', (attempts, record['verification_id']))
            if attempts == 5:
                tx.execute('UPDATE email_verifications SET delivery_ciphertext=NULL WHERE verification_id=%s', (record['verification_id'],))
            raise APIError(429 if attempts == 5 else 422, 'EMAIL_VERIFICATION_ATTEMPTS_EXCEEDED' if attempts == 5 else 'EMAIL_VERIFICATION_INVALID', commit=True)
        tx.execute('UPDATE email_verifications SET consumed_at=%s,delivery_ciphertext=NULL WHERE verification_id=%s', (self.clock(), record['verification_id']))
        tx.execute("UPDATE users SET status='ACTIVE',email_verified_at=%s,updated_at=%s WHERE user_id=%s", (self.clock(), self.clock(), user['user_id']))
        return Result(200, {'email_verified': True, 'next_action': 'LOGIN'})

    def login(self, tx, req):
        fields(req.body, ['email', 'password'], ['email', 'password'])
        address = email(req.body['email'])
        password = req.body['password']
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise APIError(422, 'VALIDATION_ERROR')
        self.rate(tx, 'login-ip', req.ip, 100, 900, count=False)
        self.rate(tx, 'login-email', address, 10, 900, count=False)
        user = tx.one('SELECT * FROM users WHERE email=%s FOR UPDATE', (address,))
        matched = password_matches(password, user['password_hash'] if user else self.dummy_password)
        if not user or not matched:
            self.rate(tx, 'login-ip', req.ip, 100, 900)
            self.rate(tx, 'login-email', address, 10, 900)
            raise APIError(401, 'AUTH_INVALID_CREDENTIALS', commit=True)
        if user['status'] == 'SUSPENDED':
            raise APIError(403, 'ACCOUNT_SUSPENDED', {'suspension_reason': user['suspension_reason']}, commit=True)
        if not user['email_verified_at']:
            raise APIError(403, 'EMAIL_NOT_VERIFIED', commit=True)
        if user['status'] != 'ACTIVE':
            raise APIError(401, 'AUTH_INVALID_CREDENTIALS', commit=True)
        data, cookie = self.issue(tx, user=str(user['user_id']))
        return Result(201, {'user': self.user_public(tx, user), **data}, {'Set-Cookie': cookie})

    def reset_password(self, tx, req):
        fields(req.body, ['token', 'new_password'], ['token', 'new_password'])
        raw = text(req.body['token'], 200)
        if not valid_password(req.body['new_password']):
            raise APIError(422, 'VALIDATION_ERROR')
        self.rate(tx, 'reset-confirm-ip', req.ip, 30, 900)
        hashed = credential_hash(self.settings.hmac_key, 'PASSWORD_RESET', 'reset', raw)
        record = tx.one('SELECT * FROM password_resets WHERE token_hash=%s', (hashed,))
        if not record or record['consumed_at'] or record['revoked_at'] or dt(record['expires_at']) <= self.clock():
            raise APIError(422, 'RESET_TOKEN_INVALID', commit=True)
        user = tx.one('SELECT * FROM users WHERE user_id=%s FOR UPDATE', (record['user_id'],))
        # Same user -> credential lock order as resend, avoiding reset/resend deadlocks.
        record = tx.one('SELECT * FROM password_resets WHERE token_hash=%s FOR UPDATE', (hashed,))
        if record['consumed_at'] or record['revoked_at'] or dt(record['expires_at']) <= self.clock():
            raise APIError(422, 'RESET_TOKEN_INVALID', commit=True)
        if user['status'] != 'ACTIVE' or not user['email_verified_at']:
            raise APIError(422, 'RESET_TOKEN_INVALID', commit=True)
        tx.execute('UPDATE users SET password_hash=%s,updated_at=%s WHERE user_id=%s', (password_hash(req.body['new_password']), self.clock(), user['user_id']))
        tx.execute('UPDATE password_resets SET consumed_at=%s,delivery_ciphertext=NULL WHERE reset_id=%s', (self.clock(), record['reset_id']))
        self.revoke(tx, user=str(user['user_id']))
        return Result(204)
