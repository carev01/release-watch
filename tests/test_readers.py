from pathlib import Path

import httpx
import respx

from release_watch.models import WatchedSource
from release_watch.watch.readers import read_source
from tests.fakes import fast_fetcher

PAGES = Path(__file__).parent / "fixtures" / "pages"


def src(url, kind="entry_page", **kw):
    return WatchedSource(vendor_name="V", url=url, url_key=url, kind=kind, state="active", origin="seed", **kw)


@respx.mock
async def test_entry_page_reader_splits():
    respx.get("https://x.com/rn").mock(return_value=httpx.Response(
        200, text=(PAGES / "changelog.html").read_text(), headers={"content-type": "text/html"}))
    async with fast_fetcher() as f:
        r = await read_source(src("https://x.com/rn"), f)
    assert r.outcome == "ok"
    assert [e.key for e in r.entries] == ["october 28, 2026", "october 14, 2026", "september 30, 2026"]
    assert r.size > 0 and "October 28, 2026" in r.markdown


@respx.mock
async def test_feed_reader_items():
    respx.get("https://github.com/v/releases.atom").mock(return_value=httpx.Response(
        200, text=(PAGES / "feed.xml").read_text(), headers={"content-type": "application/atom+xml"}))
    async with fast_fetcher() as f:
        r = await read_source(src("https://github.com/v/releases.atom", kind="feed"), f)
    assert [e.heading for e in r.entries] == ["v1.18.0", "v1.17.2", "v1.17.1"]
    assert r.entries[0].key == "1"
    assert r.entries[0].body.startswith("## v1.18.0") and "Highlights" in r.entries[0].body


@respx.mock
async def test_json_split_rule_reader():
    respx.get("https://h.com/api/articles.json").mock(return_value=httpx.Response(200, json={"articles": [
        {"id": 5, "title": "Oct 2026", "body": "<p>New</p>", "html_url": "https://h.com/5"}]}))
    rule = {"json": {"items": "articles", "key": "id", "title": "title", "body": "body", "url": "html_url"}}
    async with fast_fetcher() as f:
        r = await read_source(src("https://h.com/api/articles.json", split_rule=rule), f)
    assert r.outcome == "ok" and [e.key for e in r.entries] == ["5"]


@respx.mock
async def test_index_reader_children_keys_are_normalised_urls():
    respx.get("https://x.com/rn/").mock(return_value=httpx.Response(
        200, text='<a href="/rn/v2/?utm_source=a">v2</a><a href="/rn/v1/">v1</a>' + " " * 2000))
    follow = {"pick": {"type": "links", "href_regex": r"/rn/v\d"}}
    async with fast_fetcher() as f:
        r = await read_source(src("https://x.com/rn/", kind="index_page", follow=follow), f)
    assert [e.key for e in r.entries] == ["https://x.com/rn/v2", "https://x.com/rn/v1"]
    assert r.entries[0].heading == "v2" and r.entries[0].body == "[v2](https://x.com/rn/v2/?utm_source=a)"


@respx.mock
async def test_version_page_reader():
    respx.get("https://ibm.com/api/p").mock(return_value=httpx.Response(200, json={"taxonomy": {"versions": ["8.2.1", "8.2.2"]}}))
    follow = {"pick": {"type": "versions", "json_path": "taxonomy.versions"}}
    async with fast_fetcher() as f:
        r = await read_source(src("https://ibm.com/api/p", kind="index_page", follow=follow), f)
    assert [e.key for e in r.entries] == ["8.2.2", "8.2.1"]


@respx.mock
async def test_blocked_fetch_is_suspect():
    respx.get("https://x.com/rn").mock(return_value=httpx.Response(403))
    async with fast_fetcher() as f:
        r = await read_source(src("https://x.com/rn"), f)
    assert (r.outcome, r.reason) == ("suspect", "forbidden")


@respx.mock
async def test_unreadable_pdf_is_suspect():
    respx.get("https://x.com/rn.pdf").mock(return_value=httpx.Response(
        200, content=b"%PDF-1.7" + b"x" * 3000, headers={"content-type": "application/pdf"}))
    async with fast_fetcher() as f:
        r = await read_source(src("https://x.com/rn.pdf"), f)
    assert (r.outcome, r.reason) == ("suspect", "pdf_unparsed")


@respx.mock
async def test_pdf_text_is_one_entry():
    from tests.fakes import tiny_pdf
    respx.get("https://x.com/rn.pdf").mock(return_value=httpx.Response(
        200, content=tiny_pdf(["NetWorker 19.14 Release Notes", "Fixed issue NW-123 in backups."]),
        headers={"content-type": "application/pdf"}))
    async with fast_fetcher() as f:
        r = await read_source(src("https://x.com/rn.pdf", title="NetWorker 19.14 release notes (PDF)"), f)
    assert r.outcome == "ok"
    [e] = r.entries
    assert (e.key, e.heading) == ("pdf", "NetWorker 19.14 release notes (PDF)")
    assert "Fixed issue NW-123 in backups." in e.body and e.body == r.markdown


@respx.mock
async def test_gone_and_error():
    respx.get("https://x.com/a").mock(return_value=httpx.Response(404))
    respx.get("https://x.com/b").mock(return_value=httpx.Response(500))
    async with fast_fetcher() as f:
        assert (await read_source(src("https://x.com/a"), f)).outcome == "gone"
        assert (await read_source(src("https://x.com/b"), f)).outcome == "error"


@respx.mock
async def test_duplicate_feed_keys_get_suffixes():
    feed = ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            '<item><guid>https://a/#x</guid><title>A</title><description>one</description></item>'
            '<item><guid>https://a/#x</guid><title>B</title><description>two</description></item>'
            '</channel></rss>')
    respx.get("https://a.com/rss").mock(return_value=httpx.Response(200, text=feed,
                                                                    headers={"content-type": "application/rss+xml"}))
    async with fast_fetcher() as f:
        r = await read_source(src("https://a.com/rss", kind="feed"), f)
    assert [e.key for e in r.entries] == ["https://a/#x", "https://a/#x (2)"]


def test_prerelease_detection():
    from release_watch.watch.readers import is_prerelease
    for pre in ("v1.18.4-rc.1", "v1.18.3-rc2", "2.0.0-beta.3", "Velero 1.19 Beta", "3.1.0-alpha",
                "12.0 Preview", "4.2.0.dev1", "1.0.0-SNAPSHOT", "v2.1-pre", "10.1 Early Access", "9.0 nightly"):
        assert is_prerelease(pre), pre
    for ga in ("v1.18.4", "October 28, 2026", "What's new in 12.1", "Beta features now GA",
               "Release 8.0 - previews of reports removed", "Version 9.2 Patch 1", "Velero 1.18.4 released"):
        assert not is_prerelease(ga), ga


@respx.mock
async def test_prerelease_entries_are_dropped():
    feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>3</id><title>v1.19.0-rc.1</title><link href="https://g.com/r/v1.19.0-rc.1"/><content>c</content></entry>
      <entry><id>2</id><title>v1.18.4</title><link href="https://g.com/r/v1.18.4"/><content>c</content></entry>
    </feed>"""
    respx.get("https://g.com/r.atom").mock(return_value=httpx.Response(
        200, text=feed, headers={"content-type": "application/atom+xml"}))
    async with fast_fetcher() as f:
        r = await read_source(src("https://g.com/r.atom", kind="feed"), f)
    assert [e.heading for e in r.entries] == ["v1.18.4"]
