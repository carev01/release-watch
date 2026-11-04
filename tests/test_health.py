from datetime import UTC, datetime, timedelta

from release_watch.health import compute_health
from release_watch.models import EntryEvent, FetchAttempt, JobRun, WatchedSource

NOW = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


def src(s, url, vendor="V", **kw):
    w = WatchedSource(vendor_name=vendor, url=url, url_key=url, title=url.rsplit("/", 1)[-1], kind="entry_page",
                      state=kw.pop("state", "active"), origin="seed",
                      created_at=kw.pop("created_at", NOW - timedelta(days=7)), **kw)
    s.add(w)
    s.flush()
    return w


def watch_run(s, started, status="ok"):
    s.add(JobRun(job="watch", started_at=started, finished_at=started + timedelta(minutes=20), status=status))


def test_health_counts_checked_of_total(session_factory):
    with session_factory() as s:
        a, b, c = (src(s, f"https://x.com/{n}") for n in "abc")
        watch_run(s, NOW - timedelta(hours=2))
        s.add(FetchAttempt(source_id=a.id, outcome="same", fetched_at=NOW - timedelta(hours=1, minutes=50)))
        s.add(FetchAttempt(source_id=b.id, outcome="error", detail="http_500",
                           fetched_at=NOW - timedelta(hours=1, minutes=50)))
        s.add(FetchAttempt(source_id=c.id, outcome="same", fetched_at=NOW - timedelta(days=1, hours=2)))
        s.commit()
        h = compute_health(s, NOW)
    assert (h.checked, h.total) == (1, 3)
    assert h.failed == ["V: b (http_500)"]


def test_broken_after_three_suspect_days(session_factory):
    with session_factory() as s:
        src(s, "https://x.com/a", suspect_streak=3)
        src(s, "https://x.com/b", suspect_streak=2)
        s.commit()
        assert compute_health(s, NOW).broken == ["V: a"]


def test_waiting_login_grouped_by_vendor(session_factory):
    with session_factory() as s:
        for i in range(3):
            src(s, f"https://ave.com/{i}", vendor="AvePoint", login_walled=True)
        src(s, "https://eon.io/a", vendor="Eon", login_walled=True)
        s.commit()
        h = compute_health(s, NOW)
    assert h.waiting_login == {"AvePoint": 3, "Eon": 1} and h.total == 0
    assert h.public_only == ["Rubrik", "Cohesity"]


def test_needs_look_stale_after_three_days(session_factory):
    with session_factory() as s:
        a = src(s, "https://x.com/a")
        s.add(EntryEvent(source_id=a.id, kind="new", heading="h", status="needs_look",
                         created_at=NOW - timedelta(days=4)))
        s.add(EntryEvent(source_id=a.id, kind="new", heading="h2", status="needs_look", created_at=NOW))
        s.commit()
        h = compute_health(s, NOW)
    assert (h.needs_look, h.needs_look_stale) == (2, 1)


def test_stale_running_job_marked_failed(session_factory):
    with session_factory() as s:
        s.add(JobRun(job="watch", started_at=NOW - timedelta(hours=7), status="running"))
        s.add(JobRun(job="sync", started_at=NOW - timedelta(hours=1), status="running"))
        s.commit()
        h = compute_health(s, NOW)
        statuses = {j.job: (j.status, j.error) for j in s.query(JobRun)}
    assert h.stale_jobs == ["watch"]
    assert statuses["watch"] == ("failed", "stale: never finished")
    assert statuses["sync"][0] == "running"


def test_last_watch_ok_and_sync_error(session_factory):
    with session_factory() as s:
        watch_run(s, NOW - timedelta(days=2))
        watch_run(s, NOW - timedelta(hours=2), status="failed")
        s.add(JobRun(job="sync", started_at=NOW - timedelta(hours=3), finished_at=NOW - timedelta(hours=3),
                     status="failed", error="DocExtractorError: boom"))
        s.commit()
        h = compute_health(s, NOW)
    assert h.last_watch_ok == NOW - timedelta(days=2) + timedelta(minutes=20)
    assert h.sync_error == "DocExtractorError: boom"


def test_gone_since_last_digest(session_factory):
    with session_factory() as s:
        s.add(JobRun(job="digest", started_at=NOW - timedelta(days=1), finished_at=NOW - timedelta(days=1),
                     status="ok"))
        g = src(s, "https://x.com/g", state="gone")
        s.add(FetchAttempt(source_id=g.id, outcome="gone", fetched_at=NOW - timedelta(hours=2)))
        old = src(s, "https://x.com/old", state="gone")
        s.add(FetchAttempt(source_id=old.id, outcome="gone", fetched_at=NOW - timedelta(days=3)))
        s.commit()
        assert compute_health(s, NOW).gone_new == ["V: g"]


def test_total_excludes_sources_created_after_the_watch_started(session_factory):
    with session_factory() as s:
        a = src(s, "https://x.com/a")
        a.created_at = NOW - timedelta(days=5)
        new = src(s, "https://x.com/successor")
        new.created_at = NOW - timedelta(hours=1)
        watch_run(s, NOW - timedelta(hours=2))
        s.add(FetchAttempt(source_id=a.id, outcome="same", fetched_at=NOW - timedelta(hours=1, minutes=50)))
        s.commit()
        h = compute_health(s, NOW)
    assert (h.checked, h.total) == (1, 1)
