"""Transport-independent input checks and safe API responses."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from uuid import UUID, uuid4


class APIError(Exception):
    def __init__(self, status, code, details=None, *, commit=False, headers=None):
        super().__init__(code)
        self.status, self.code, self.details = status, code, details or {}
        self.commit, self.headers = commit, headers or {}


@dataclass
class Request:
    method: str
    path: str
    headers: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)
    ip: str = "unknown"
    file: tuple | None = None  # filename, media type, bytes


@dataclass
class Result:
    status: int = 200
    data: object = None
    headers: dict = field(default_factory=dict)


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc)


def dt(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def json_value(value):
    if isinstance(value, (datetime, UUID)):
        return str(value)
    raise TypeError(type(value).__name__)


def dumps(value):
    return json.dumps(value, default=json_value, ensure_ascii=False, separators=(',', ':'))


def obj(value):
    return json.loads(value) if isinstance(value, str) else value


def fields(body, allowed, required=()):
    if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
        raise APIError(422, "VALIDATION_ERROR")


def text(value, maximum, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise APIError(422, "VALIDATION_ERROR")
    return value.strip()


def email(value):
    value = text(value, 254).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise APIError(422, "VALIDATION_ERROR")
    return value


def uuid_value(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise APIError(422, "VALIDATION_ERROR") from None


def consent(value):
    fields(value, ['policy_version', 'granted'], ['policy_version', 'granted'])
    if type(value['granted']) is not bool:
        raise APIError(422, "VALIDATION_ERROR")
    return {'policy_version': text(value['policy_version'], 30), 'granted': value['granted']}
