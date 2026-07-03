import tempfile
from pathlib import Path

import pytest

from src.models.job import Job, JobStatus
from src.utils.tracker import AppliedJobsTracker


def make_job(job_id: str = "job123") -> Job:
    return Job(job_id=job_id, title="Software Engineer", company="ACME", location="Remote")


def test_not_processed_initially():
    with tempfile.TemporaryDirectory() as d:
        tracker = AppliedJobsTracker(Path(d))
        assert tracker.already_processed("nonexistent") is False


def test_record_applied_then_detected():
    with tempfile.TemporaryDirectory() as d:
        tracker = AppliedJobsTracker(Path(d))
        job = make_job("abc")
        job.mark_applied("data/resumes/abc.pdf")
        tracker.record(job)
        assert tracker.already_processed("abc") is True


def test_record_failed_is_still_tracked():
    with tempfile.TemporaryDirectory() as d:
        tracker = AppliedJobsTracker(Path(d))
        job = make_job("xyz")
        job.mark_failed("some error")
        tracker.record(job)
        assert tracker.already_processed("xyz") is True


def test_summary_counts_correctly():
    with tempfile.TemporaryDirectory() as d:
        tracker = AppliedJobsTracker(Path(d))

        j1 = make_job("1")
        j1.mark_applied("r.pdf")
        tracker.record(j1)

        j2 = make_job("2")
        j2.mark_applied("r2.pdf")
        tracker.record(j2)

        j3 = make_job("3")
        j3.mark_failed("network error")
        tracker.record(j3)

        j4 = make_job("4")
        j4.mark_skipped("not_easy_apply")
        tracker.record(j4)

        summary = tracker.summary()
        assert summary.get("applied") == 2
        assert summary.get("failed") == 1
        assert summary.get("skipped") == 1


def test_persists_across_instances():
    """Verify the JSON file is reloaded correctly by a new tracker instance."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        t1 = AppliedJobsTracker(path)
        job = make_job("persist1")
        job.mark_applied("r.pdf")
        t1.record(job)

        t2 = AppliedJobsTracker(path)
        assert t2.already_processed("persist1") is True
