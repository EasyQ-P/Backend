"""The backend container's service object."""
from easyq_common.core import ServiceCore

from .consents import ConsentsMixin
from .documents import DocumentsMixin
from .lectures import LecturesMixin
from .participation import ParticipationMixin
from .routes import RoutesMixin


class BackendService(ParticipationMixin, LecturesMixin, ConsentsMixin, DocumentsMixin, RoutesMixin, ServiceCore):
    pass
