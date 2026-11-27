"""The backend container's service object."""
from easyq_common.core import ServiceCore

from .admin import AdminMixin
from .consents import ConsentsMixin
from .deletion import DeletionMixin
from .documents import DocumentsMixin
from .lectures import LecturesMixin
from .notices import NoticesMixin
from .participation import ParticipationMixin
from .questions import QuestionsMixin
from .reports import ReportsMixin
from .routes import RoutesMixin
from .training import TrainingMixin


class BackendService(QuestionsMixin, ReportsMixin, DeletionMixin, NoticesMixin, AdminMixin, TrainingMixin,
                     ParticipationMixin, LecturesMixin, ConsentsMixin, DocumentsMixin, RoutesMixin, ServiceCore):
    pass
