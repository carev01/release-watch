from datetime import UTC, datetime

from sqlalchemy import select

from release_watch.models import Entry, EntryEvent, WatchedSource
from release_watch.watch.run import run_watch
from release_watch.watch.successor import propose_successors
from tests.fakes import ScriptedFetcher, SummaryLLM, html_page

SIGNAL = "https://ibm.com/api/product/sp"
OLD = "https://ibm.com/docs/sp/8.2.1?topic=whats-new"
NEW = "https://ibm.com/docs/sp/8.2.2?topic=whats-new"
NOW = datetime(2026, 11, 4, tzinfo=UTC)
GOOD = "<h2>What's new in 8.2.2</h2>" + "<p>New feature for the 8.2.2 release.</p>" * 10


def setup(sf, signal_versions=("8.2.2", "8.2.1")):
    with sf() as s:
        sig = WatchedSource(vendor_name="IBM", url=SIGNAL, url_key=SIGNAL, kind="index_page", state="active",
                            origin="seed", follow={"pick": {"type": "versions", "json_path": "v"}})
        old = WatchedSource(vendor_name="IBM", url=OLD, url_key=OLD, title="Storage Protect 8.2.1 what's new",
                            kind="entry_page", state="active", origin="seed", selector="main",
                            follow={"pin": {"version": "8.2.1", "signal": SIGNAL,
                                            "successor": "https://ibm.com/docs/sp/{version}?topic=whats-new"}})
        s.add_all([sig, old])
        s.flush()
        for v in signal_versions:
            s.add(Entry(source_id=sig.id, key=v, heading=v, body=v, body_hash=v))
        s.commit()
        return old.id


def sources(sf):
    with sf() as s:
        return {w.url: w for w in s.scalars(select(WatchedSource))}


def events(sf):
    with sf() as s:
        return list(s.scalars(select(EntryEvent)))


async def test_successor_created_when_signal_newer(session_factory):
    old_id = setup(session_factory)
    counts = await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, GOOD)}), NOW)
    assert counts == {"proposed": 1, "unverified": 0}
    new = sources(session_factory)[NEW]
    assert (new.state, new.origin, new.follow["pin"]["version"]) == ("active", "successor", "8.2.2")
    assert new.title == "Storage Protect 8.2.2 what's new" and new.selector == "main"
    assert sources(session_factory)[OLD].superseded_by == new.id
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.url) == ("moved", "queued", NEW)
    assert ev.heading == "Release-notes URL for Storage Protect 8.2.1 what's new moved to 8.2.2"
    assert old_id


async def test_successor_rejects_thin_or_missing_version(session_factory):
    setup(session_factory)
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, "<p>Page not found</p>")}), NOW)
    assert NEW not in sources(session_factory)
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.reason, ev.url) == ("moved", "needs_look", "successor_unverified", NEW)


async def test_successor_rejects_page_without_new_version(session_factory):
    setup(session_factory)
    body = "<h2>What's new in 8.2.1</h2>" + "<p>Old feature text.</p>" * 20
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, body)}), NOW)
    assert NEW not in sources(session_factory)


async def test_no_successor_when_signal_not_newer(session_factory):
    setup(session_factory, signal_versions=("8.2.1", "8.1.0"))
    fetcher = ScriptedFetcher({})
    assert await propose_successors(session_factory, fetcher, NOW) == {"proposed": 0, "unverified": 0}
    assert fetcher.calls == []


async def test_moved_event_deduped(session_factory):
    setup(session_factory)
    bad = ScriptedFetcher({NEW: html_page(NEW, "<p>x</p>")})
    await propose_successors(session_factory, bad, NOW)
    await propose_successors(session_factory, bad, NOW)
    assert len(events(session_factory)) == 1 and bad.calls == [NEW]


async def test_old_source_superseded_after_successor_baseline(session_factory):
    setup(session_factory)
    old_page = "<h2>8.2.1 (October 2026)</h2><p>Old.</p>"
    import json

    from release_watch.fetch import FetchResult
    sig_body = json.dumps({"v": ["8.2.2", "8.2.1"]})
    fetcher = ScriptedFetcher({
        SIGNAL: FetchResult(SIGNAL, SIGNAL, 200, "application/json", sig_body, len(sig_body), "ok", ""),
        OLD: html_page(OLD, old_page), NEW: html_page(NEW, GOOD)})
    await run_watch(session_factory, fetcher, SummaryLLM(), confirm_delay=0)  # creates the successor
    assert sources(session_factory)[OLD].state == "active"
    await run_watch(session_factory, fetcher, SummaryLLM(), confirm_delay=0)  # successor baseline
    assert sources(session_factory)[OLD].state == "superseded"
    assert sources(session_factory)[NEW].state == "active"
