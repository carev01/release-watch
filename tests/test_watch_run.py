import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from release_watch.llm import LLMError
from release_watch.models import CatalogProduct, Entry, EntryEvent, FetchAttempt, WatchedSource
from release_watch.watch.run import run_watch
from tests.fakes import ScriptedFetcher, SummaryLLM, html_page

URL = "https://x.com/rn"
DAY1 = "<h2>October 14, 2026</h2><ul><li>SAML sign-on.</li></ul><h2>September 30, 2026</h2><ul><li>Retention.</li></ul>"
NEW = "<h2>October 28, 2026</h2><ul><li>Immutable Azure Blob.</li></ul>"
DAY2 = NEW + DAY1


def add_source(sf, url=URL, kind="entry_page", **kw):
    with sf() as s:
        src = WatchedSource(vendor_name="Example", url=url, url_key=url, title="Updates", kind=kind,
                            state="active", origin="seed", selector=kw.pop("selector", "main"), **kw)
        s.add(src)
        s.commit()
        return src.id


async def run(sf, fetcher, llm=None, **kw):
    return await run_watch(sf, fetcher, llm or SummaryLLM(), confirm_delay=0, **kw)


def events(sf):
    with sf() as s:
        return list(s.scalars(select(EntryEvent).order_by(EntryEvent.created_at)))


async def test_first_run_is_baseline_no_events(session_factory):
    add_source(session_factory)
    counts = await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}))
    assert counts["baseline"] == 1 and counts["events_queued"] == 0
    with session_factory() as s:
        assert len(list(s.scalars(select(Entry)))) == 2
        assert s.scalar(select(FetchAttempt.outcome)) == "baseline"
    assert events(session_factory) == []


async def test_new_entry_creates_queued_event_with_verbatim_body_and_summary(session_factory):
    add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}))
    counts = await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY2)}))
    assert counts["changed"] == 1 and counts["events_queued"] == 1
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.heading) == ("new", "queued", "October 28, 2026")
    assert ev.body == "## October 28, 2026\n\n* Immutable Azure Blob."
    assert ev.summary == "Adds X." and ev.url == URL


async def test_confirmation_mismatch_drops_event(session_factory):
    add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}))
    other = "<h2>October 28, 2026</h2><ul><li>Something else.</li></ul>" + DAY1
    await run(session_factory, ScriptedFetcher({URL: [html_page(URL, DAY2), html_page(URL, other)]}))
    assert events(session_factory) == []


async def test_confirmation_fetch_failure_defers(session_factory):
    add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}))
    failing = ScriptedFetcher({URL: [html_page(URL, DAY2), html_page(URL, "", status=503, outcome="error",
                                                                    reason="http_503")]})
    await run(session_factory, failing)
    assert failing.calls == [URL, URL]
    assert events(session_factory) == []
    with session_factory() as s:
        assert "october 28, 2026" not in set(s.scalars(select(Entry.key)))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY2)}))
    assert [e.heading for e in events(session_factory)] == ["October 28, 2026"]


async def test_shrunk_page_is_suspect_and_streak_counts(session_factory):
    sid = add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1 * 20)}))
    for _ in range(2):
        counts = await run(session_factory, ScriptedFetcher({URL: html_page(URL, "<p>tiny</p>")}))
    assert counts["suspect"] == 1
    with session_factory() as s:
        assert s.get(WatchedSource, sid).suspect_streak == 2
    assert events(session_factory) == []


async def test_gone_marks_source_gone(session_factory):
    sid = add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, "", status=404, outcome="gone",
                                                                reason="http_404")}))
    with session_factory() as s:
        assert s.get(WatchedSource, sid).state == "gone"


async def test_login_walled_sources_skipped(session_factory):
    add_source(session_factory, login_walled=True)
    fetcher = ScriptedFetcher({})
    counts = await run(session_factory, fetcher)
    assert fetcher.calls == [] and counts["sources"] == 0


async def test_index_entry_mode_sends_child_verbatim(session_factory):
    follow = {"mode": "entry", "pick": {"type": "links", "href_regex": r"/rn/\d{4}-"}}
    add_source(session_factory, kind="index_page", follow=follow)
    day1 = '<a href="/rn/2026-07">July 2026</a>'
    day2 = '<a href="/rn/2026-10">October 2026</a>' + day1
    child = "https://x.com/rn/2026-10"
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, day1)}))
    await run(session_factory, ScriptedFetcher({
        URL: html_page(URL, day2), child: html_page(child, "<h1>October 2026</h1><p>New: thing.</p>")}))
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.heading, ev.url) == ("new", "queued", "October 2026", child)
    assert "New: thing." in ev.body and ev.summary == "Adds X."


def add_product(sf, version):
    pid = uuid.uuid4()
    with sf() as s:
        s.add(CatalogProduct(de_id=pid, vendor_de_id=uuid.uuid4(), name="Backup", de_version=version,
                             synced_at=datetime.now(UTC)))
        s.commit()
    return pid


async def test_index_version_mode_dedupes_and_suppresses_known_version(session_factory):
    pid = add_product(session_factory, "11.46")
    follow = {"pick": {"type": "links", "href_regex": r"\.pdf$"}}
    url2 = "https://x.com/rn2"
    for u in (URL, url2):
        add_source(session_factory, url=u, kind="index_page", follow=follow, product_de_id=pid)
    base = '<a href="/rn_11.40.pdf">Release Notes 11.40</a>'
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, base), url2: html_page(url2, base)}))
    old = '<a href="/rn_11.44.pdf">Release Notes 11.44</a>' + base
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, old), url2: html_page(url2, base)}))
    assert events(session_factory) == []
    new = '<a href="/rn_11.48.pdf">Release Notes 11.48</a>' + old
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, new), url2: html_page(url2, new)}))
    [ev] = events(session_factory)
    assert (ev.kind, ev.version, ev.status) == ("version", "11.48", "queued")
    assert ev.heading == "Example released a new version of Backup: 11.48"


async def test_version_page_only_newest_and_only_if_newer(session_factory):
    follow = {"pick": {"type": "versions", "json_path": "v"}}
    add_source(session_factory, kind="index_page", follow=follow)

    def page(vs):
        import json

        from release_watch.fetch import FetchResult
        text = json.dumps({"v": vs})
        return FetchResult(URL, URL, 200, "application/json", text, len(text), "ok", "")

    await run(session_factory, ScriptedFetcher({URL: page(["8.2.1"])}))
    await run(session_factory, ScriptedFetcher({URL: page(["8.2.1", "8.1.9"])}))
    assert events(session_factory) == []
    await run(session_factory, ScriptedFetcher({URL: page(["8.2.3", "8.2.2", "8.2.1"])}))
    assert [(e.kind, e.version) for e in events(session_factory)] == [("version", "8.2.3")]


async def test_summary_failure_keeps_event(session_factory):
    add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY2)}), llm=SummaryLLM(error=LLMError("x")))
    [ev] = events(session_factory)
    assert ev.status == "queued" and ev.summary is None


async def test_edited_entry_event_has_diff(session_factory):
    add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}))
    edited = DAY1.replace("<li>SAML sign-on.</li>", "<li>SAML sign-on.</li><li>SCIM provisioning.</li>")
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, edited)}))
    [ev] = events(session_factory)
    assert ev.kind == "edited" and "+* SCIM provisioning." in ev.diff


async def test_dry_run_writes_nothing(session_factory, capsys):
    add_source(session_factory)
    counts = await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}), dry_run=True)
    assert counts == {"sources": 1, "ok": 1, "suspect": 0, "error": 0, "gone": 0}
    with session_factory() as s:
        assert list(s.scalars(select(Entry))) == [] and list(s.scalars(select(FetchAttempt))) == []
    assert "october 14, 2026" in capsys.readouterr().out


class BrokenFetcher(ScriptedFetcher):
    async def fetch(self, url):
        if url == "https://x.com/boom":
            raise RecursionError("too deep")
        return await super().fetch(url)


async def test_one_source_crash_does_not_stop_the_run(session_factory):
    add_source(session_factory)
    add_source(session_factory, url="https://x.com/boom")
    counts = await run(session_factory, BrokenFetcher({URL: html_page(URL, DAY1)}))
    assert counts["baseline"] == 1 and counts["error"] == 1
    with session_factory() as s:
        details = set(s.scalars(select(FetchAttempt.detail)))
    assert "crash: RecursionError" in details


async def test_storage_failure_on_one_source_does_not_stop_the_run(session_factory, monkeypatch):
    import release_watch.watch.run as run_mod

    add_source(session_factory)
    add_source(session_factory, url="https://x.com/other")
    real = run_mod._settle_first

    def flaky(sf, src, *args):
        if src.url == URL:
            raise RuntimeError("db hiccup")
        return real(sf, src, *args)

    monkeypatch.setattr(run_mod, "_settle_first", flaky)
    counts = await run(session_factory, ScriptedFetcher({
        URL: html_page(URL, DAY1), "https://x.com/other": html_page("https://x.com/other", DAY1)}))
    assert counts["baseline"] == 1 and counts["error"] == 1
