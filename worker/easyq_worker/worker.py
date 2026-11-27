"""The worker object: runner plus one mixin per job type (JOB_HANDLERS in each mixin, see runner.WorkerBase)."""
from .cleanup import CleanupJob
from .finalize import FinalizeJob
from .ingest import IngestJob
from .judge_model import JudgeModelKeepalive
from .mail import MailJob
from .purge import PurgeJob
from .qa import QAJob
from .runner import WorkerBase
from .sync import SyncJob
from .transcript import TranscriptJob


class Worker(QAJob, FinalizeJob, PurgeJob, SyncJob, IngestJob, TranscriptJob, MailJob, CleanupJob, JudgeModelKeepalive,
             WorkerBase):
    pass
