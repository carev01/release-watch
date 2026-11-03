from pathlib import Path

import pytest
from sqlalchemy import select

from release_watch.discovery.classify import extract_text, label_candidate, label_pending, looks_like_feed
from release_watch.fetch import FetchResult
from release_watch.llm import LLMError
from release_watch.models import WatchedSource

PAGES = Path(__file__).parent / "fixtures" / "pages"


def page(name, url="https://x.com/rn", content_type="text/html", outcome="ok", reason=""):
    text = (PAGES / name).read_text() if name else ""
    return FetchResult(url, url, 200, content_type, text, len(text), outcome, reason)


class FakeFetcher:
    def __init__(self, results):
        self.results = results

    async def fetch(self, url):
        result = self.results[url]
        if isinstance(result, Exception):
            raise result
        return result


class FakeLLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    async def complete_json(self, system, user, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


def source(url="https://x.com/rn", kind="entry_page"):
    return WatchedSource(vendor_name="Example", url=url, url_key=url, title="Product updates", kind=kind,
                         state="candidate", origin="heuristic")


def test_extract_text_drops_chrome():
    text = extract_text(page("changelog.html").text, "https://x.com/rn")
    assert "October 28, 2026" in text
    assert "Pricing" not in text


def test_looks_like_feed():
    assert looks_like_feed(page("feed.xml", content_type="application/atom+xml"))
    assert looks_like_feed(page("feed.xml", content_type="text/plain"))
    assert not looks_like_feed(page("changelog.html"))


async def test_blocked_skips_llm():
    llm = FakeLLM(reply={})
    fetcher = FakeFetcher({"https://x.com/rn": page(None, outcome="blocked", reason="login_redirect")})
    label = await label_candidate(source(), fetcher, llm)
    assert (label.label, label.reason) == ("blocked", "login_redirect")
    assert llm.calls == 0


async def test_feed_detected_without_llm():
    llm = FakeLLM(reply={})
    fetcher = FakeFetcher({"https://x.com/rn": page("feed.xml", content_type="application/atom+xml")})
    label = await label_candidate(source(), fetcher, llm)
    assert (label.label, label.kind, label.reason, label.evidence) == ("release_notes", "feed", "feed", "v1.18.0")
    assert llm.calls == 0


async def test_llm_label_accepted_when_evidence_present():
    llm = FakeLLM(reply={"label": "release_notes", "kind": "entry_page", "evidence": "October 28, 2026"})
    label = await label_candidate(source(), FakeFetcher({"https://x.com/rn": page("changelog.html")}), llm)
    assert (label.label, label.kind, label.evidence) == ("release_notes", "entry_page", "October 28, 2026")


async def test_hallucinated_evidence_downgraded():
    llm = FakeLLM(reply={"label": "release_notes", "kind": "entry_page", "evidence": "Version 99.9"})
    label = await label_candidate(source(), FakeFetcher({"https://x.com/rn": page("changelog.html")}), llm)
    assert (label.label, label.reason) == ("unsure", "evidence_not_found")


async def test_llm_failure_is_unsure():
    llm = FakeLLM(error=LLMError("both down"))
    label = await label_candidate(source(), FakeFetcher({"https://x.com/rn": page("changelog.html")}), llm)
    assert (label.label, label.reason) == ("unsure", "llm_failed")


async def test_gone_and_error_are_unsure():
    fetcher = FakeFetcher({"https://x.com/a": page(None, "https://x.com/a", outcome="gone", reason="soft_404"),
                           "https://x.com/b": page(None, "https://x.com/b", outcome="error", reason="http_500")})
    gone = await label_candidate(source("https://x.com/a"), fetcher, FakeLLM())
    err = await label_candidate(source("https://x.com/b"), fetcher, FakeLLM())
    assert (gone.label, gone.reason) == ("unsure", "gone")
    assert (err.label, err.reason) == ("unsure", "fetch_failed")


async def test_label_pending_persists_and_resumes(session_factory):
    urls = ["https://x.com/1", "https://x.com/2", "https://x.com/3"]
    with session_factory() as s:
        s.add_all(source(u) for u in urls)
        s.commit()
    fetcher = FakeFetcher({urls[0]: page("feed.xml", urls[0], "application/atom+xml"),
                           urls[1]: page("feed.xml", urls[1], "application/atom+xml"),
                           urls[2]: RuntimeError("fetcher crashed")})
    with pytest.raises(RuntimeError):
        await label_pending(session_factory, fetcher, FakeLLM())
    with session_factory() as s:
        labels = {r.url: r.label for r in s.scalars(select(WatchedSource))}
    assert labels == {urls[0]: "release_notes", urls[1]: "release_notes", urls[2]: None}


async def test_label_pending_skips_labelled_unless_relabel(session_factory):
    with session_factory() as s:
        row = source()
        row.label = "unsure"
        s.add(row)
        s.commit()
    fetcher = FakeFetcher({"https://x.com/rn": page("feed.xml", content_type="application/atom+xml")})
    assert (await label_pending(session_factory, fetcher, FakeLLM())) == {}
    assert (await label_pending(session_factory, fetcher, FakeLLM(), relabel=True)) == {"release_notes": 1}


async def test_label_pending_keeps_seed_kind(session_factory):
    with session_factory() as s:
        row = source("https://keepit.com/updates", kind="section")
        row.origin, row.selector = "seed", "#updates"
        s.add(row)
        s.commit()
    llm = FakeLLM(reply={"label": "release_notes", "kind": "entry_page", "evidence": "October 28, 2026"})
    fetcher = FakeFetcher({"https://keepit.com/updates": page("changelog.html", "https://keepit.com/updates")})
    await label_pending(session_factory, fetcher, llm)
    with session_factory() as s:
        row = s.scalars(select(WatchedSource)).one()
    assert (row.label, row.kind) == ("release_notes", "section")


async def test_label_pending_refines_heuristic_kind(session_factory):
    with session_factory() as s:
        s.add(source(kind="entry_page"))
        s.commit()
    llm = FakeLLM(reply={"label": "release_notes", "kind": "index_page", "evidence": "October 28, 2026"})
    await label_pending(session_factory, FakeFetcher({"https://x.com/rn": page("changelog.html")}), llm)
    with session_factory() as s:
        assert s.scalars(select(WatchedSource)).one().kind == "index_page"


async def test_pdf_is_unsure_without_llm():
    llm = FakeLLM(reply={"label": "release_notes", "kind": "entry_page", "evidence": "Release Notes"})
    pdf = FetchResult("https://x.com/rn.pdf", "https://x.com/rn.pdf", 200, "application/pdf",
                      "%PDF-1.7 ... Release Notes ...", 40000, "ok", "")
    label = await label_candidate(source("https://x.com/rn.pdf"), FakeFetcher({"https://x.com/rn.pdf": pdf}), llm)
    assert (label.label, label.reason) == ("unsure", "pdf_unparsed")
    assert llm.calls == 0


async def test_thin_page_is_unsure_without_llm():
    llm = FakeLLM(reply={"label": "not_release_notes", "kind": "unknown", "evidence": ""})
    html = "<html><body><main>Your search for returned result(s).</main></body></html>" + " " * 5000
    shell = FetchResult("https://x.com/rn", "https://x.com/rn", 200, "text/html", html, len(html), "ok", "")
    label = await label_candidate(source(), FakeFetcher({"https://x.com/rn": shell}), llm)
    assert (label.label, label.reason) == ("unsure", "thin_text")
    assert llm.calls == 0
