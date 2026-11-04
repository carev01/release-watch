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
    assert events(session_factory) == []  # the moved line waits for the successor's baseline
    assert old_id


async def test_successor_rejects_thin_or_missing_version(session_factory):
    setup(session_factory)
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, "<p>Page not found</p>")}), NOW)
    assert NEW not in sources(session_factory)
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.reason, ev.url) == ("moved", "needs_look", "successor_not_found", NEW)
    assert "Page not found" in ev.body  # the owner sees what the new URL shows


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
    old_page = "<h2>8.2.1 (October 2026)</h2>" + "<p>Old feature for the 8.2.1 release.</p>" * 10
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
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.url, ev.version) == ("moved", "queued", NEW, "8.2.2")
    assert ev.heading == "Release-notes URL for IBM (Storage Protect 8.2.1 what's new) moved to 8.2.2"


async def test_no_moved_line_when_successor_baseline_fails(session_factory):
    import json

    from release_watch.fetch import FetchResult
    setup(session_factory)
    sig_body = json.dumps({"v": ["8.2.2", "8.2.1"]})
    sig = FetchResult(SIGNAL, SIGNAL, 200, "application/json", sig_body, len(sig_body), "ok", "")
    fetcher = ScriptedFetcher({SIGNAL: sig, OLD: html_page(OLD, "<h2>8.2.1 (October 2026)</h2>" + "<p>Old feature text.</p>" * 20),
                               NEW: [html_page(NEW, GOOD), html_page(NEW, "", status=403, outcome="blocked",
                                                                     reason="forbidden")]})
    await run_watch(session_factory, fetcher, SummaryLLM(), confirm_delay=0)
    await run_watch(session_factory, fetcher, SummaryLLM(), confirm_delay=0)
    assert events(session_factory) == []
    assert sources(session_factory)[OLD].state == "active"


async def test_successor_rejects_redirect_elsewhere(session_factory):
    from release_watch.fetch import FetchResult
    setup(session_factory)
    text = f"<html><body><main>{GOOD}</main></body></html>"
    moved = FetchResult(NEW, "https://ibm.com/docs/sp/landing", 200, "text/html", text, len(text), "ok", "")
    await propose_successors(session_factory, ScriptedFetcher({NEW: moved}), NOW)
    assert NEW not in sources(session_factory)
    [ev] = events(session_factory)
    assert ev.reason == "successor_redirected"
    assert ev.body.startswith("Redirected to https://ibm.com/docs/sp/landing") and "New feature" in ev.body


async def test_successor_rejects_not_found_wording(session_factory):
    setup(session_factory)
    body = "<h1>Page not found</h1><p>The topic for 8.2.2 does not exist.</p>" + "<p>Browse the docs.</p>" * 20
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, body)}), NOW)
    assert NEW not in sources(session_factory)


async def test_successor_accepts_a_much_shorter_page(session_factory):
    # A new version's what's-new page lists only that version's changes; the old one may be cumulative.
    from release_watch.models import Snapshot
    setup(session_factory)
    with session_factory() as s:
        old = s.scalar(select(WatchedSource).where(WatchedSource.url == OLD))
        s.add(Snapshot(source_id=old.id, markdown="x" * 20_000, hash="h"))
        s.commit()
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, GOOD)}), NOW)
    assert NEW in sources(session_factory)


async def test_moved_heading_names_the_product(session_factory):
    import uuid as _uuid

    from release_watch.models import CatalogProduct
    setup(session_factory)
    pid = _uuid.uuid4()
    with session_factory() as s:
        s.add(CatalogProduct(de_id=pid, vendor_de_id=_uuid.uuid4(), name="Storage Protect", synced_at=NOW))
        s.query(WatchedSource).filter(WatchedSource.url == OLD).update({"product_de_id": pid})
        s.commit()
    from release_watch.watch.successor import moved_heading
    with session_factory() as s:
        old = s.scalar(select(WatchedSource).where(WatchedSource.url == OLD))
        assert moved_heading(s, old, "8.2.2") == \
            "Release-notes URL for IBM Storage Protect (Storage Protect 8.2.1 what's new) moved to 8.2.2"


async def test_successor_reasons_for_short_page_and_missing_version(session_factory):
    setup(session_factory)
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, "<p>8.2.2 stub</p>")}), NOW)
    assert events(session_factory)[0].reason == "successor_too_short"


async def test_successor_reason_when_the_page_fails(session_factory):
    setup(session_factory)
    blocked = html_page(NEW, "", status=403, outcome="blocked", reason="forbidden")
    await propose_successors(session_factory, ScriptedFetcher({NEW: blocked}), NOW)
    [ev] = events(session_factory)
    assert ev.reason == "successor_fetch_failed" and "403" in ev.body


async def test_owner_chosen_successor_gets_the_moved_line_at_baseline(session_factory):
    from release_watch.watch.successor import watch_successor
    setup(session_factory)
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, "<p>Page not found</p>")}), NOW)
    other = "https://ibm.com/docs/sp/8.2.2?topic=whats-new-in-sp"
    with session_factory() as s:
        [ev] = s.scalars(select(EntryEvent)).all()
        new = watch_successor(s, ev, other, NOW)
        s.commit()
        assert (new.url, new.origin, new.follow["pin"]["version"]) == (other, "successor", "8.2.2")
    [ev] = events(session_factory)
    assert ev.status == "dismissed" and ev.dedupe_key is None
    assert sources(session_factory)[OLD].superseded_by == sources(session_factory)[other].id
    from release_watch.watch.successor import on_successor_baseline
    with session_factory() as s:
        on_successor_baseline(s, s.scalar(select(WatchedSource).where(WatchedSource.url == other)))
        s.commit()
    assert [e.status for e in events(session_factory) if e.status == "queued"] == ["queued"]


async def test_successor_already_watched_is_linked_not_duplicated(session_factory):
    setup(session_factory)
    with session_factory() as s:  # e.g. discovery already follows DocExtractor's new URL
        s.add(WatchedSource(vendor_name="IBM", url=NEW, url_key=NEW, kind="entry_page", state="candidate",
                            origin="heuristic"))
        s.commit()
    counts = await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, GOOD)}), NOW)
    assert counts == {"proposed": 1, "unverified": 0} and events(session_factory) == []
    src = sources(session_factory)
    assert src[NEW].state == "active" and src[OLD].superseded_by == src[NEW].id
    # it is read like the page it replaces, and keeps following new versions
    assert src[NEW].selector == "main" and src[NEW].follow["pin"]["version"] == "8.2.2"


async def test_successor_already_baselined_retires_the_old_page_now(session_factory):
    setup(session_factory)
    with session_factory() as s:
        w = WatchedSource(vendor_name="IBM", url=NEW, url_key=NEW, kind="entry_page", state="active", origin="heuristic")
        s.add(w)
        s.flush()
        s.add(Entry(source_id=w.id, key="k", heading="k", body="b", body_hash="h"))
        s.commit()
    await propose_successors(session_factory, ScriptedFetcher({NEW: html_page(NEW, GOOD)}), NOW)
    assert sources(session_factory)[OLD].state == "superseded"
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.version) == ("moved", "queued", "8.2.2")
