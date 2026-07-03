import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.models.job import Job, JobStatus


class AppliedJobsTracker:
    """Persists the set of attempted jobs to disk so runs are idempotent.

    Keys are namespaced by source portal: "linkedin:JOB_ID" / "indeed:JOB_ID"
    so the same numeric ID from two portals never collides.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "applied_jobs.json"
        self._records: dict[str, dict] = self._load()

    @staticmethod
    def make_key(source: str, job_id: str) -> str:
        """Return the namespaced tracker key for a job."""
        return f"{source}:{job_id}"

    def already_processed(self, job_id: str, source: str = "linkedin") -> bool:
        """Return True only for jobs we should never retry.

        - applied / skipped(already_applied) → permanent, never re-attempt
        - failed / skipped(dry_run) → allow retry on next run
        """
        key = self.make_key(source, job_id)
        rec = self._records.get(key)
        if rec is None:
            return False
        status = rec.get("status", "")
        if status == JobStatus.APPLIED.value:
            return True
        if status == JobStatus.SKIPPED.value and rec.get("error") == "already_applied":
            return True
        return False

    def record(self, job: Job, source: str = "linkedin") -> None:
        key = self.make_key(source, job.job_id)
        self._records[key] = {
            "key": key,
            "source": source,
            "job_id": job.job_id,
            "title": job.title,
            "company": job.company,
            "status": job.status.value,
            "applied_at": job.applied_at.isoformat() if job.applied_at else None,
            "error": job.error,
            "url": job.url,
            "updated_at": datetime.now().isoformat(),
        }
        self._save()
        logger.debug(f"Tracked [{source}] job {job.job_id} as {job.status.value}")

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self._records.values():
            status = rec.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _load(self) -> dict[str, dict]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(f"Corrupt tracker file {self._path} — starting fresh")
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
