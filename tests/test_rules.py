import json

import httpx
import respx

from release_watch.watch.rules import Child, children, json_entries, newest, pick_children, version_tuple
from tests.fakes import fast_fetcher


def test_links_pick_with_regex():
    html = ('<main><a href="/rn/2026-oct">Oct 2026</a><a href="https://x.com/rn/2026-sep">Sep 2026</a>'
            '<a href="/pricing">Pricing</a></main>')
    kids = pick_children(html, "text/html", "https://x.com/rn/", {"type": "links", "href_regex": r"/rn/\d{4}"})
    assert [(c.url, c.title) for c in kids] == [("https://x.com/rn/2026-oct", "Oct 2026"),
                                                ("https://x.com/rn/2026-sep", "Sep 2026")]


def test_links_pick_dedupes_urls():
    html = '<a href="/a">A</a><a href="/a">A again</a>'
    assert len(pick_children(html, "text/html", "https://x.com/", {"type": "links"})) == 1


def test_sitemap_pick():
    xml = ("<urlset><url><loc>https://afi.ai/docs/whats-new/afi-oct-2026/release-notes/</loc></url>"
           "<url><loc>https://afi.ai/docs/de/whats-new/afi-oct-2026/release-notes/</loc></url>"
           "<url><loc>https://afi.ai/docs/whats-new/afi-jul-2026/release-notes/</loc></url>"
           "<url><loc>https://afi.ai/docs/setup/</loc></url></urlset>")
    kids = pick_children(xml, "application/xml", "https://afi.ai/docs/sitemap.xml",
                         {"type": "sitemap", "loc_regex": r"/docs/whats-new/afi-[a-z]{3}-\d{4}/release-notes/$"})
    assert [c.url for c in kids] == ["https://afi.ai/docs/whats-new/afi-oct-2026/release-notes/",
                                     "https://afi.ai/docs/whats-new/afi-jul-2026/release-notes/"]


def test_json_pick_orders():
    body = json.dumps({"categories": [{"id": "ReleaseNotes", "subLinks": [
        {"title": "10.8", "url": "/bundle/RN.HTML.10.8", "position": 2},
        {"title": "10.9", "url": "/bundle/RN.HTML.10.9", "position": 1}]}]})
    kids = pick_children(body, "application/json", "https://help-be.zerto.com/api/categories",
                         {"type": "json", "items": "categories.*.subLinks", "url": "url", "title": "title",
                          "order_by": "position", "order": "asc"})
    assert [c.title for c in kids] == ["10.9", "10.8"]
    assert kids[0].url == "https://help-be.zerto.com/bundle/RN.HTML.10.9"


def test_json_pick_url_template():
    body = json.dumps({"items": [{"id": 7, "name": "A"}]})
    kids = pick_children(body, "application/json", "https://x.com/api",
                         {"type": "json", "items": "items", "title": "name", "url_template": "https://x.com/p/{id}"})
    assert kids == [Child(url="https://x.com/p/7", title="A", number=None)]


def test_versions_pick_newest_first():
    body = json.dumps({"taxonomy": {"versions": ["8.2.1", "8.2.2", "8.1.0", "8.2.2"]}})
    kids = pick_children(body, "application/json", "https://www.ibm.com/docs/api/v1/product/x",
                         {"type": "versions", "json_path": "taxonomy.versions"})
    assert [c.title for c in kids] == ["8.2.2", "8.2.1", "8.1.0"]


def test_versions_pick_from_text_with_regex():
    text = "<table><tr><td>Lina 7.4</td></tr><tr><td>Lina 7.3</td></tr><tr><td>Lina 7.10</td></tr></table>"
    kids = pick_children(text, "text/html", "https://x.com/",
                         {"type": "versions", "regex": r"Lina (\d+\.\d+)"})
    assert [c.title for c in kids] == ["7.10", "7.4", "7.3"]


def test_newest_max_number():
    kids = [Child("https://x/_2025", "2025", 2025.0), Child("https://x/_2026", "2026", 2026.0)]
    assert newest(kids, "max_number").title == "2026"
    assert newest(kids, "first").title == "2025"


def test_version_tuple():
    assert version_tuple("11.48") > version_tuple("11.46")
    assert version_tuple("v10.9.10") > version_tuple("10.9.9")
    assert version_tuple("25-1") > version_tuple("24-2")


@respx.mock
async def test_two_level_then_follows_newest():
    respx.get("https://docs.x.com/RN").mock(return_value=httpx.Response(200, text=(
        '<a href="/RN/RN_2025">RN 2025</a><a href="/RN/RN_2026">RN 2026</a>' + " " * 2000)))
    respx.get("https://docs.x.com/RN/RN_2026").mock(return_value=httpx.Response(200, text=(
        '<a href="/RN/RN_2026/035">September 2026 - v17.7.0</a><a href="/RN/RN_2026/040">July 2026</a>'
        + " " * 2000)))
    follow = {"pick": {"type": "links", "href_regex": r"/RN/RN_\d{4}$", "number_regex": r"(\d{4})"},
              "newest": "max_number",
              "then": {"pick": {"type": "links", "href_regex": r"/RN/RN_\d{4}/\d+$"}}}
    async with fast_fetcher() as f:
        result, kids = await children(f, "https://docs.x.com/RN", follow)
    assert result.outcome == "ok"
    assert [c.url for c in kids] == ["https://docs.x.com/RN/RN_2026/035", "https://docs.x.com/RN/RN_2026/040"]


@respx.mock
async def test_url_rewrite():
    respx.get("https://help-be.x.com/api/c").mock(return_value=httpx.Response(
        200, json={"items": [{"t": "A", "u": "https://help-be.x.com/bundle/A"}]}))
    follow = {"pick": {"type": "json", "items": "items", "title": "t", "url": "u"},
              "url_rewrite": [["help-be.x.com", "help.x.com"]]}
    async with fast_fetcher() as f:
        _, kids = await children(f, "https://help-be.x.com/api/c", follow)
    assert kids[0].url == "https://help.x.com/bundle/A"


def test_json_entries_zendesk():
    body = json.dumps({"articles": [
        {"id": 2, "title": "September 2026", "body": "<ul><li>New A</li></ul>", "html_url": "https://h/2",
         "created_at": "2026-10-06T10:00:00Z"},
        {"id": 1, "title": "August 2026", "body": "<p>B</p>", "html_url": "https://h/1",
         "created_at": "2026-09-01T10:00:00Z"}]})
    rule = {"items": "articles", "key": "id", "title": "title", "body": "body", "url": "html_url",
            "date": "created_at", "body_format": "html"}
    entries = json_entries(body, rule)
    assert [e.key for e in entries] == ["2", "1"]
    assert entries[0].heading == "September 2026"
    assert entries[0].body.startswith("## September 2026") and "New A" in entries[0].body
    assert entries[0].url == "https://h/2" and entries[0].published_at.year == 2026


def test_json_entries_join_list_bodies():
    body = json.dumps({"items": [{"id": 1, "title": "Nov 3", "changes": ["Added A.", "Fixed B."]}]})
    [entry] = json_entries(body, {"items": "items", "body": "changes", "body_format": "text"})
    assert entry.body == "## Nov 3\n\n* Added A.\n* Fixed B."


def test_json_pick_capture_feeds_url_template():
    body = json.dumps({"subLinks": [{"title": "10.9", "route": "/bundle/RN.HTML.10.9/page/x"}]})
    pick = {"type": "json", "items": "subLinks", "title": "title",
            "capture": {"field": "route", "regex": r"bundle/([^/]+)"},
            "url_template": "https://help-be.x.com/api/bundle/{_1}/toc"}
    [kid] = pick_children(body, "application/json", "https://help-be.x.com/api/categories", pick)
    assert kid.url == "https://help-be.x.com/api/bundle/RN.HTML.10.9/toc"


def test_versions_pick_raw_reads_attributes():
    html = ('<div data-items="[{&quot;path&quot;:&quot;/backup-exec-readme/25-1/x.html&quot;},'
            '{&quot;path&quot;:&quot;/backup-exec-readme/24-2/y.html&quot;}]"></div>')
    kids = pick_children(html, "text/html", "https://d.com/", {"type": "versions", "raw": True,
                                                               "regex": r"backup-exec-readme/(\d+-\d+)/"})
    assert [k.title for k in kids] == ["25-1", "24-2"]


def test_feed_pick_lists_item_links():
    from release_watch.watch.rules import pick_children
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>RN</title>
      <item><title>VM Backup Version 9.21.0.0</title><link>https://h.com/rn/vm-backup-9-21-0-0/</link><description>short</description></item>
      <item><title>VM Backup Version 9.20.0.0</title><link>https://h.com/rn/vm-backup-9-20-0-0/</link><description>short</description></item>
    </channel></rss>"""
    kids = pick_children(rss, "application/rss+xml", "https://h.com/feed/", {"type": "feed"})
    assert [(k.url, k.title) for k in kids] == [("https://h.com/rn/vm-backup-9-21-0-0/", "VM Backup Version 9.21.0.0"),
                                                ("https://h.com/rn/vm-backup-9-20-0-0/", "VM Backup Version 9.20.0.0")]


MC = "https://docs.x.com/SPX_RN/"


def _raw(url, text, ctype="application/javascript"):
    from release_watch.fetch import FetchResult
    return FetchResult(url, url, 200, ctype, text, len(text), "ok", "")


def madcap_site():
    from tests.fakes import ScriptedFetcher
    toc = ("define({numchunks:1,prefix:'RN_Chunk',chunkstart:['/Content/a.htm'],tree:{n:[{i:0,c:0},"
           "{i:1,c:0,n:[{i:2,c:0,n:[{i:3,c:0},{i:4,c:0}]},{i:5,c:0,n:[{i:6,c:0}]}]}]}});")
    chunk = ("define({'/Content/RN/title.htm':{i:[0],t:['SPX Release Notes'],b:['']},"
             "'/Content/RN/by-version.htm':{i:[1],t:['Details by Product Version'],b:['']},"
             "'/Content/RN/SPX-8.0.0.htm':{i:[2],t:['ShadowProtect SPX 8.0.0 (June, 2026)'],b:['']},"
             "'/Content/RN/Enhancements-8.0.0.htm':{i:[3],t:['Enhancements'],b:['']},"
             "'/Content/RN/Fixed Issues-8.0.0.htm':{i:[4],t:['What\\'s fixed'],b:['']},"
             "'/Content/RN/SPX-7.5.18.htm':{i:[5],t:['ShadowProtect SPX 7.5.18 (May, 2026)'],b:['']},"
             "'/Content/RN/Enhancements-7.5.18.htm':{i:[6],t:['Enhancements'],b:['']}});")
    return ScriptedFetcher({
        MC + "default.htm": _raw(MC + "default.htm", "<html><iframe name=topic></iframe></html>", "text/html"),
        MC + "Data/HelpSystem.xml": _raw(MC + "Data/HelpSystem.xml",
                                         '<WebHelpSystem Toc="Data/Tocs/RN.js" SkinName="HTML5"/>', "text/xml"),
        MC + "Data/Tocs/RN.js": _raw(MC + "Data/Tocs/RN.js", toc),
        MC + "Data/Tocs/RN_Chunk0.js": _raw(MC + "Data/Tocs/RN_Chunk0.js", chunk),
    })


async def test_madcap_pick_lists_toc_pages_with_their_subpages():
    from release_watch.watch.rules import children
    result, kids = await children(madcap_site(), MC + "default.htm",
                                  {"pick": {"type": "madcap", "title_regex": r"SPX \d", "include_children": True}})
    assert result.outcome == "ok"
    assert [k.title for k in kids] == ["ShadowProtect SPX 8.0.0 (June, 2026)", "ShadowProtect SPX 7.5.18 (May, 2026)"]
    assert kids[0].url == MC + "Content/RN/SPX-8.0.0.htm"
    assert kids[0].parts == (MC + "Content/RN/Enhancements-8.0.0.htm", MC + "Content/RN/Fixed%20Issues-8.0.0.htm")


async def test_madcap_pick_without_filter_lists_every_page_in_toc_order():
    from release_watch.watch.rules import children
    _, kids = await children(madcap_site(), MC + "default.htm", {"pick": {"type": "madcap"}})
    assert [k.title for k in kids][:3] == ["SPX Release Notes", "Details by Product Version",
                                           "ShadowProtect SPX 8.0.0 (June, 2026)"]
    assert "What's fixed" in [k.title for k in kids]


def test_newer_than_tracked_compares_at_docextractor_precision():
    from release_watch.watch.rules import newer_than_tracked
    assert newer_than_tracked("1.19.0", "1.18") and newer_than_tracked("10.4", "10.3")
    assert not newer_than_tracked("1.18.5", "1.18")  # a patch of the tracked docs version
    assert not newer_than_tracked("8.2.2", "8.2.2") and not newer_than_tracked("25-0", "25-1")
    assert newer_than_tracked("2027.1", "2026.2.1") and newer_than_tracked("26-0", "25-1")


def test_same_version_line_rejects_numbers_from_another_scheme():
    from release_watch.watch.rules import same_version_line
    assert same_version_line("1.19.0", "1.18") and same_version_line("2.0", "1.18")
    assert same_version_line("10.9", "10.9") and same_version_line("9.4", "10.9")
    assert not same_version_line("1.6", "5.0")  # OADP's own versions vs the OpenShift docs version
    assert not same_version_line("1.2645", "11.46")  # a component's build number vs the product release
