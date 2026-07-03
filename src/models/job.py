from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    FOUND = "found"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"


class Job(BaseModel):
    """Represents a single LinkedIn job posting."""

    job_id: str
    title: str
    company: str
    location: str
    description: str = ""
    is_easy_apply: bool = False
    url: str = ""
    status: JobStatus = JobStatus.FOUND
    applied_at: datetime | None = None
    tailored_resume_path: str | None = None
    error: str | None = None
    keywords_extracted: list[str] = Field(default_factory=list)

    def mark_applied(self, resume_path: str) -> None:
        self.status = JobStatus.APPLIED
        self.applied_at = datetime.now()
        self.tailored_resume_path = resume_path

    def mark_failed(self, reason: str) -> None:
        self.status = JobStatus.FAILED
        self.error = reason

    def mark_skipped(self, reason: str) -> None:
        self.status = JobStatus.SKIPPED
        self.error = reason
