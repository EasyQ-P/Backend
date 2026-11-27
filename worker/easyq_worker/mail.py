"""EMAIL_VERIFICATION · PASSWORD_RESET jobs and the SMTP adapter (fastapi-mail, Gmail 587 or local Mailpit)."""

from easyq_common.contracts import APIError, dt
from easyq_common.security import decrypt

import asyncio


class Mailer:
    def __init__(self, settings):
        self.settings = settings

    def send(self, address, subject, body):
        from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
        s = self.settings
        if not s.smtp_host or not s.smtp_from:
            raise APIError(503, 'SMTP_NOT_CONFIGURED')
        config = ConnectionConfig(MAIL_USERNAME=s.smtp_user, MAIL_PASSWORD=s.smtp_password,
            MAIL_FROM=s.smtp_from, MAIL_PORT=s.smtp_port, MAIL_SERVER=s.smtp_host,
            MAIL_STARTTLS=s.smtp_starttls, MAIL_SSL_TLS=s.smtp_ssl,   # Gmail 587: STARTTLS; local Mailpit: neither
            USE_CREDENTIALS=bool(s.smtp_user), VALIDATE_CERTS=True)
        message = MessageSchema(subject=subject, recipients=[address], body=body, subtype=MessageType.plain)
        asyncio.run(FastMail(config).send_message(message))


class MailJob:
    def mail(self, job):
        reset = job['job_type'] == 'PASSWORD_RESET'
        table, idcol = ('password_resets', 'reset_id') if reset else ('email_verifications', 'verification_id')
        with self.db.transaction() as tx:
            self.active(tx, job)
            # Named columns only: easyq_worker may not read the verifier hashes (code_hash/token_hash) or password_hash.
            r = tx.one(f'SELECT user_id,delivery_ciphertext,expires_at,consumed_at,revoked_at FROM {table} WHERE {idcol}=%s FOR UPDATE', (job['target_id'],))
            if not r or r['consumed_at'] or r['revoked_at'] or dt(r['expires_at']) <= self.service.clock() or not r['delivery_ciphertext']:
                self.finish(tx, job, {'skipped': True})
                return
            user = tx.one('SELECT email FROM users WHERE user_id=%s', (r['user_id'],))
            raw = decrypt(self.settings.encryption_key, r['delivery_ciphertext'])
            address = user['email']
        body = self.settings.origin + '/reset-password#token=' + raw if reset else 'EasyQ 가입 인증 코드: ' + raw
        self.mailer.send(address, 'EasyQ 비밀번호 재설정' if reset else 'EasyQ 이메일 인증', body)
        with self.db.transaction() as tx:
            self.active(tx, job)
            if reset:
                tx.execute(f'UPDATE {table} SET delivery_ciphertext=NULL WHERE {idcol}=%s', (job['target_id'],))
            else:
                tx.execute(f'UPDATE {table} SET delivery_ciphertext=NULL,sent_at=%s WHERE {idcol}=%s', (self.service.clock(), job['target_id']))
            self.finish(tx, job)
