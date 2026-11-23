"""Password hashing, opaque tokens and one-time credential protection."""
import base64
import hashlib
import hmac
import re
import secrets


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def credential_hash(key, purpose, subject, value):
    return hmac.new(key.encode(), f"{purpose}:{subject}:{value}".encode(), hashlib.sha256).hexdigest()


def password_hash(password):
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(value).decode()


def password_matches(password, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (16384, 8, 1):
            return False
        result = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(result, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def valid_password(value):
    return isinstance(value, str) and 8 <= len(value) <= 20 and bool(re.search('[A-Za-z]', value)) and bool(re.search('[0-9]', value))


def token():
    return secrets.token_urlsafe(32)


def encrypt(key, value):
    from cryptography.fernet import Fernet
    return Fernet(key.encode()).encrypt(value.encode())


def decrypt(key, value):
    from cryptography.fernet import Fernet
    return Fernet(key.encode()).decrypt(bytes(value)).decode()
