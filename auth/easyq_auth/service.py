"""The auth container's service object."""
from easyq_common.core import ServiceCore

from .accounts import AccountsMixin
from .sessions import SessionsMixin


class AuthService(AccountsMixin, SessionsMixin, ServiceCore):
    pass
