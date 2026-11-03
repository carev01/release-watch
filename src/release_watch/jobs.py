"""Every scheduled job records a job_run row, so a run that never happened is visible."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from release_watch.models import JobRun


@contextmanager
def job_run(session_factory: sessionmaker[Session], job: str) -> Iterator[dict]:
    with session_factory() as s:
        run = JobRun(job=job, status="running", counts={})
        s.add(run)
        s.commit()
        run_id = run.id
    counts: dict = {}
    try:
        yield counts
    except BaseException as exc:
        _finish(session_factory, run_id, "failed", counts, f"{type(exc).__name__}: {exc}"[:2000])
        raise
    _finish(session_factory, run_id, "ok", counts, None)


def _finish(session_factory, run_id, status: str, counts: dict, error: str | None) -> None:
    with session_factory() as s:
        run = s.get(JobRun, run_id)
        run.status = status
        run.counts = dict(counts)
        run.error = error
        run.finished_at = datetime.now(UTC)
        s.commit()
