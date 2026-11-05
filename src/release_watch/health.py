"""The health block of the daily digest (spec 9): a failure never looks like "nothing changed"."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from release_watch.models import EntryEvent, FetchAttempt, JobRun, WatchedSource

PUBLIC_ONLY = ["Rubrik", "Cohesity"]
BROKEN_STREAK = 3
NEEDS_LOOK_STALE = timedelta(days=3)
STALE_JOB = timedelta(hours=6)


@dataclass
class Health:
    checked: int = 0
    total: int = 0
    failed: list[str] = field(default_factory=list)
    suspect: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    gone_new: list[str] = field(default_factory=list)
    waiting_login: dict[str, int] = field(default_factory=dict)
    public_only: list[str] = field(default_factory=lambda: list(PUBLIC_ONLY))
    needs_look: int = 0
    needs_look_stale: int = 0
    sync_error: str | None = None
    last_watch_ok: datetime | None = None
    stale_jobs: list[str] = field(default_factory=list)


def _label(src: WatchedSource, detail: str | None = None) -> str:
    text = f"{src.vendor_name}: {src.title or src.url}"
    return f"{text} ({detail})" if detail else text


def _mark_stale_jobs(s, now) -> list[str]:
    stale = list(s.scalars(select(JobRun).where(JobRun.status == "running", JobRun.started_at < now - STALE_JOB)))
    for run in stale:
        run.status, run.error, run.finished_at = "failed", "stale: never finished", now
    s.commit()
    return [run.job for run in stale]


def compute_health(s, now: datetime) -> Health:
    h = Health(stale_jobs=_mark_stale_jobs(s, now))
    active = select(WatchedSource).where(WatchedSource.state == "active")
    watched = list(s.scalars(active.where(WatchedSource.login_walled.is_(False))
                             .order_by(WatchedSource.vendor_name, WatchedSource.title)))
    by_id = {w.id: w for w in watched}
    last_watch = s.scalar(select(JobRun).where(JobRun.job == "watch").order_by(JobRun.started_at.desc()).limit(1))
    # Sources added or accepted during or after the run are checked from the next run on.
    h.total = sum(1 for w in watched
                  if last_watch is None or max(w.created_at, w.decided_at or w.created_at) <= last_watch.started_at)
    h.broken = [_label(w) for w in watched if w.suspect_streak >= BROKEN_STREAK]
    for vendor, n in s.execute(select(WatchedSource.vendor_name, func.count()).where(
            WatchedSource.state == "active", WatchedSource.login_walled.is_(True))
            .group_by(WatchedSource.vendor_name).order_by(WatchedSource.vendor_name)):
        h.waiting_login[vendor] = n

    if last_watch is not None:
        window_end = last_watch.finished_at or now
        attempts = s.scalars(select(FetchAttempt).where(FetchAttempt.fetched_at >= last_watch.started_at,
                                                        FetchAttempt.fetched_at <= window_end))
        latest: dict = {}
        for a in attempts:  # a source may have a first read and a confirmation read; the later one counts
            if a.source_id in by_id and (a.source_id not in latest or a.fetched_at >= latest[a.source_id].fetched_at):
                latest[a.source_id] = a
        h.checked = sum(1 for a in latest.values() if a.outcome != "error")
        ordered = sorted(latest.values(), key=lambda a: _label(by_id[a.source_id]))
        h.failed = [_label(by_id[a.source_id], a.detail) for a in ordered if a.outcome == "error"]
        h.suspect = [_label(by_id[a.source_id], a.detail) for a in ordered if a.outcome == "suspect"]
    h.last_watch_ok = s.scalar(select(func.max(JobRun.finished_at)).where(JobRun.job == "watch", JobRun.status == "ok"))

    last_digest = s.scalar(select(func.max(JobRun.started_at)).where(JobRun.job == "digest", JobRun.status == "ok"))
    gone_q = (select(WatchedSource).join(FetchAttempt, FetchAttempt.source_id == WatchedSource.id)
              .where(WatchedSource.state == "gone", FetchAttempt.outcome == "gone"))
    if last_digest:
        gone_q = gone_q.where(FetchAttempt.fetched_at > last_digest)
    h.gone_new = sorted({_label(w) for w in s.scalars(gone_q)})

    h.needs_look = s.scalar(select(func.count()).select_from(EntryEvent).where(EntryEvent.status == "needs_look"))
    h.needs_look_stale = s.scalar(select(func.count()).select_from(EntryEvent).where(
        EntryEvent.status == "needs_look", EntryEvent.created_at < now - NEEDS_LOOK_STALE))
    last_sync = s.scalar(select(JobRun).where(JobRun.job == "sync").order_by(JobRun.started_at.desc()).limit(1))
    if last_sync is not None and last_sync.status == "failed":
        h.sync_error = last_sync.error
    return h
