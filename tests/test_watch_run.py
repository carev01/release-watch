import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from release_watch.llm import LLMError
from release_watch.models import CatalogProduct, CatalogSource, Entry, EntryEvent, FetchAttempt, WatchedSource
from release_watch.watch.run import run_watch
from tests.fakes import ScriptedFetcher, SummaryLLM, html_page

URL = "https://x.com/rn"
NOW_DT = datetime(2026, 11, 4, tzinfo=UTC)
DAY1 = "<h2>October 14, 2026</h2><ul><li>SAML sign-on.</li></ul><h2>September 30, 2026</h2><ul><li>Retention.</li></ul>"
NEW = "<h2>October 28, 2026</h2><ul><li>Immutable Azure Blob.</li></ul>"
DAY2 = NEW + DAY1


def add_source(sf, url=URL, kind="entry_page", **kw):
    with sf() as s:
        src = WatchedSource(vendor_name="Example", url=url, url_key=url, title="Updates", kind=kind,
                            state="active", origin=kw.pop("origin", "seed"), selector=kw.pop("selector", "main"), **kw)
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
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}), now=later(0))
    edited = DAY1.replace("<li>SAML sign-on.</li>", "<li>SAML sign-on.</li><li>SCIM provisioning.</li>")
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, edited)}), now=later(24))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, edited)}), now=later(48))
    [ev] = events(session_factory)
    assert ev.kind == "edited" and ev.diff == "**Added**\n- SCIM provisioning."


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


async def test_same_heading_added_above_is_sent_once_as_new(session_factory):
    add_source(session_factory)
    day1 = "<h2>October 1, 2026</h2><ul><li>Entry A.</li></ul>"
    day2 = "<h2>October 1, 2026</h2><ul><li>Entry B.</li></ul>" + day1
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, day1)}))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, day2)}))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, day2)}))
    assert [(e.kind, "Entry B." in e.body) for e in events(session_factory)] == [("new", True)]


def later(hours):
    from datetime import timedelta
    base = datetime(2026, 11, 4, 9, tzinfo=UTC)
    return lambda: base + timedelta(hours=hours)


async def test_edit_held_until_seen_again_next_day(session_factory):
    add_source(session_factory)
    edited = DAY1.replace("<li>SAML sign-on.</li>", "<li>SAML sign-on.</li><li>SCIM provisioning.</li>")
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, DAY1)}), now=later(0))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, edited)}), now=later(24))
    assert events(session_factory) == []
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, edited)}), now=later(30))  # same day: held
    assert events(session_factory) == []
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, edited)}), now=later(48))
    [ev] = events(session_factory)
    assert ev.kind == "edited" and ev.diff == "**Added**\n- SCIM provisioning."


async def test_daily_drift_never_becomes_an_edit(session_factory):
    add_source(session_factory)
    pages = [DAY1.replace("Retention.", f"Retention. Build {n}.") for n in range(4)]
    for day, page in enumerate(pages):
        await run(session_factory, ScriptedFetcher({URL: html_page(URL, page)}), now=later(24 * day))
    assert events(session_factory) == []


async def test_index_version_mode_ignores_older_archived_links(session_factory):
    follow = {"pick": {"type": "links", "href_regex": r"\.pdf$"}}
    add_source(session_factory, kind="index_page", follow=follow)
    base = '<a href="/rn_11.40.pdf">Release Notes 11.40</a>'
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, base)}))
    archived = base + '<a href="/rn_11.0.pdf">Release Notes 11.0</a>'
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, archived)}))
    assert events(session_factory) == []


async def test_version_page_reports_highest_new_version_even_if_ascending(session_factory):
    import json

    from release_watch.fetch import FetchResult
    add_source(session_factory, kind="index_page", follow={"pick": {"type": "links", "href_regex": r"rn_"}})

    def page(vs):
        html = "".join(f'<a href="/rn_{v}.pdf">Release Notes {v}</a>' for v in vs)
        return html_page(URL, html)

    await run(session_factory, ScriptedFetcher({URL: page(["11.40"])}))
    await run(session_factory, ScriptedFetcher({URL: page(["11.40", "11.42", "11.44"])}))
    assert [e.version for e in events(session_factory)] == ["11.44"]
    assert json and FetchResult


async def test_version_page_baseline_reports_a_version_newer_than_docextractor(session_factory):
    import json

    from release_watch.fetch import FetchResult
    pid = uuid.uuid4()
    with session_factory() as s:
        s.add(CatalogProduct(de_id=pid, vendor_de_id=uuid.uuid4(), name="Resiliency Platform", de_version="10.3",
                             synced_at=datetime(2026, 11, 3, tzinfo=UTC)))
        s.commit()
    add_source(session_factory, kind="index_page", follow={"pick": {"type": "versions", "json_path": "v"}},
               product_de_id=pid)
    text = json.dumps({"v": ["10.4", "10.3"]})
    page = FetchResult(URL, URL, 200, "application/json", text, len(text), "ok", "")
    await run(session_factory, ScriptedFetcher({URL: page}))
    await run(session_factory, ScriptedFetcher({URL: page}))
    [ev] = events(session_factory)
    assert (ev.kind, ev.version, ev.status) == ("version", "10.4", "queued")
    assert ev.heading == "Example released a new version of Resiliency Platform: 10.4"


async def test_version_page_baseline_quiet_when_docextractor_is_current(session_factory):
    import json

    from release_watch.fetch import FetchResult
    pid = uuid.uuid4()
    with session_factory() as s:
        s.add(CatalogProduct(de_id=pid, vendor_de_id=uuid.uuid4(), name="P", de_version="10.4",
                             synced_at=datetime(2026, 11, 3, tzinfo=UTC)))
        s.commit()
    add_source(session_factory, kind="index_page", follow={"pick": {"type": "versions", "json_path": "v"}},
               product_de_id=pid)
    text = json.dumps({"v": ["10.4", "10.3"]})
    await run(session_factory, ScriptedFetcher({URL: FetchResult(URL, URL, 200, "application/json", text, len(text), "ok", "")}))
    assert events(session_factory) == []


async def test_entry_mode_sends_the_child_with_its_sub_pages(session_factory):
    from tests.test_rules import MC, _raw, madcap_site
    follow = {"mode": "entry", "pick": {"type": "madcap", "title_regex": r"SPX \d", "include_children": True}}
    with session_factory() as s:
        s.add(WatchedSource(vendor_name="Arcserve", url=MC + "default.htm", url_key=MC + "default.htm",
                            kind="index_page", state="active", origin="seed", follow=follow))
        s.commit()
    site = madcap_site()
    old_chunk = site.script[MC + "Data/Tocs/RN_Chunk0.js"][0]
    first = old_chunk.text.replace("'/Content/RN/SPX-8.0.0.htm':{i:[2]", "'/Content/RN/OTHER.htm':{i:[99]")
    site.script[MC + "Data/Tocs/RN_Chunk0.js"] = [_raw(old_chunk.url, first), old_chunk]
    await run(session_factory, site)  # baseline: only 7.5.18 is a version page
    for path, text in (("Content/RN/SPX-8.0.0.htm", "<h1>SPX 8.0.0</h1><p>Intro.</p>"),
                       ("Content/RN/Enhancements-8.0.0.htm", "<h1>Enhancements</h1><p>Faster.</p>"),
                       ("Content/RN/Fixed%20Issues-8.0.0.htm", "<h1>Fixed</h1><p>Bug 12.</p>")):
        site.script[MC + path] = [html_page(MC + path, text)]
    await run(session_factory, site)
    [ev] = events(session_factory)
    assert ev.heading == "ShadowProtect SPX 8.0.0 (June, 2026)"
    assert ev.body.index("Intro.") < ev.body.index("Faster.") < ev.body.index("Bug 12.")


def docextractor_source(sf, url=URL):
    """A page DocExtractor extracts (one of its articles), as discovery adds it."""
    pid, csid = uuid.uuid4(), uuid.uuid4()
    with sf() as s:
        s.add(CatalogProduct(de_id=pid, vendor_de_id=uuid.uuid4(), name="Storage Protect", synced_at=NOW_DT))
        s.add(CatalogSource(de_id=csid, product_de_id=pid, name="Storage Protect for Mail", source_type="web",
                            base_url="https://x.com/", active=True, synced_at=NOW_DT))
        s.commit()
    return add_source(sf, url, product_de_id=pid, de_source_id=csid, de_article_id=uuid.uuid4(), origin="heuristic")


def redirected(url, final, body=DAY1):
    page = html_page(url, body)
    page.final_url = final
    return page


async def test_docextractor_page_that_redirects_queues_one_update_line(session_factory):
    docextractor_source(session_factory)
    for _ in range(2):
        await run(session_factory, ScriptedFetcher({URL: redirected(URL, "https://x.com/new-rn")}))
    [ev] = events(session_factory)
    assert (ev.kind, ev.status, ev.reason, ev.url) == ("moved", "queued", "docextractor_moved", "https://x.com/new-rn")
    assert ev.heading == ("Storage Protect for Mail: the release-notes page [https://x.com/rn](https://x.com/rn) "
                          "now redirects to [https://x.com/new-rn](https://x.com/new-rn)")


async def test_docextractor_page_gone_queues_an_update_line(session_factory):
    docextractor_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, "", status=404, outcome="gone",
                                                                reason="http_404")}))
    [ev] = events(session_factory)
    assert (ev.kind, ev.reason) == ("moved", "docextractor_gone")
    assert ev.heading == ("Storage Protect for Mail: the release-notes page [https://x.com/rn](https://x.com/rn) "
                          "is gone (HTTP 404)")


async def test_redirect_of_a_page_docextractor_does_not_extract_is_silent(session_factory):
    add_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: redirected(URL, "https://x.com/new-rn")}))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, "", status=404, outcome="gone",
                                                                reason="http_404")}))
    assert events(session_factory) == []


async def test_trivial_redirect_is_not_a_move(session_factory):
    docextractor_source(session_factory)
    await run(session_factory, ScriptedFetcher({URL: redirected(URL, "https://x.com/rn/")}))
    assert events(session_factory) == []


async def test_watch_retires_the_older_version_page(session_factory):
    old, new = "https://x.com/docs/4.22/rn", "https://x.com/docs/5.0/rn"
    old_id, new_id = add_source(session_factory, old), add_source(session_factory, new)
    counts = await run(session_factory, ScriptedFetcher({old: html_page(old, DAY1), new: html_page(new, DAY2)}))
    assert counts["retired"] == 1
    with session_factory() as s:
        assert (s.get(WatchedSource, old_id).state, s.get(WatchedSource, old_id).superseded_by) == ("superseded",
                                                                                                    new_id)


def versioned_product(sf, version):
    pid = uuid.uuid4()
    with sf() as s:
        s.add(CatalogProduct(de_id=pid, vendor_de_id=uuid.uuid4(), name="Velero", de_version=version, synced_at=NOW_DT))
        s.commit()
    return pid


async def test_release_note_titled_with_a_newer_version_also_sends_a_version_line(session_factory):
    pid = versioned_product(session_factory, "1.18")
    add_source(session_factory, product_de_id=pid)
    base = "<h2>v1.18.3</h2><p>Fixes.</p>"
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, base)}), now=later(0))
    patch = "<h2>v1.18.4</h2><p>More fixes.</p>" + base
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, patch)}), now=later(24))
    assert [e.kind for e in events(session_factory)] == ["new"]  # a patch of the docs DocExtractor reads
    minor = "<h2>v1.19.0</h2><p>Features.</p>" + patch
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, minor)}), now=later(48))
    version = [e for e in events(session_factory) if e.kind == "version"]
    assert [(e.version, e.status) for e in version] == [("1.19.0", "queued")]


async def test_release_note_numbers_from_another_scheme_send_no_version_line(session_factory):
    pid = versioned_product(session_factory, "11.46")
    add_source(session_factory, product_de_id=pid)
    base = "<h2>Satori v1.2644</h2><p>Fixes.</p>"
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, base)}), now=later(0))
    await run(session_factory, ScriptedFetcher({URL: html_page(URL, "<h2>Satori v1.2645</h2><p>New.</p>" + base)}),
              now=later(24))
    assert [e.kind for e in events(session_factory)] == ["new"]
