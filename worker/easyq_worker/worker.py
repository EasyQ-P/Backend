"""The worker object: runner plus one mixin per job type."""
from .cleanup import CleanupJob
from .ingest import IngestJob
from .mail import MailJob
from .runner import WorkerBase
from .transcript import TranscriptJob


class Worker(IngestJob, TranscriptJob, MailJob, CleanupJob, WorkerBase):
    pass
