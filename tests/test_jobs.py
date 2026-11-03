import pytest
from sqlalchemy import select

from release_watch.jobs import job_run
from release_watch.models import JobRun


def test_job_run_ok_records_counts(session_factory):
    with job_run(session_factory, "sync") as counts:
        counts["vendors"] = 43
    with session_factory() as s:
        row = s.scalars(select(JobRun)).one()
    assert row.status == "ok"
    assert row.counts == {"vendors": 43}
    assert row.finished_at is not None


def test_job_run_failure_recorded_and_reraised(session_factory):
    with pytest.raises(RuntimeError):
        with job_run(session_factory, "sync"):
            raise RuntimeError("boom")
    with session_factory() as s:
        row = s.scalars(select(JobRun)).one()
    assert row.status == "failed"
    assert "boom" in row.error
