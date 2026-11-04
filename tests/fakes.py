"""Fakes shared by tests."""

import copy
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


class FakeDocExtractor:
    """Duck-typed stand-in for DocExtractorClient, backed by a fixture dict."""

    def __init__(self, catalog: dict | None = None, *, tocs=None, fail_on=None, toc_fail=()):
        self.catalog = copy.deepcopy(catalog or json.loads((FIXTURES / "catalog_small.json").read_text()))
        self.tocs = tocs or {}
        self.toc_fail = set(toc_fail)
        self.fail_on = fail_on

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _check(self, name):
        if self.fail_on == name:
            from release_watch.docextractor import DocExtractorError
            raise DocExtractorError(f"GET /{name}: HTTP 503")

    async def list_vendors(self):
        self._check("vendors")
        return self.catalog["vendors"]

    async def list_products(self):
        self._check("products")
        return self.catalog["products"]

    async def list_sources(self):
        self._check("sources")
        return self.catalog["sources"]

    async def get_toc(self, source_id):
        if str(source_id) in self.toc_fail:
            from release_watch.docextractor import DocExtractorError
            raise DocExtractorError(f"GET /articles/toc/{source_id}: ReadTimeout")
        return self.tocs.get(str(source_id), [])


def fast_fetcher(transport=None):
    """A Fetcher with no per-host delay, for respx-backed tests."""
    from release_watch.config import Settings
    from release_watch.fetch import Fetcher, HostPolicy

    settings = Settings(database_url="postgresql+psycopg://x@y/z", docextractor_api_key="k")
    return Fetcher(settings, policies={}, transport=transport, default_policy=HostPolicy(min_delay_ms=0))


class ScriptedFetcher:
    """Returns FetchResults per URL in order; the last one repeats. Records every URL fetched."""

    def __init__(self, script: dict):
        self.script = {url: list(v) if isinstance(v, list) else [v] for url, v in script.items()}
        self.calls: list[str] = []

    async def fetch(self, url):
        self.calls.append(url)
        queue = self.script[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def html_page(url, body, status=200, outcome="ok", reason="", content_type="text/html"):
    from release_watch.fetch import FetchResult

    text = f"<html><body><main>{body}</main></body></html>"
    return FetchResult(url, url, status, content_type, text, len(text), outcome, reason)


class SummaryLLM:
    def __init__(self, reply=None, error=None):
        self.reply = {"summary": "Adds X."} if reply is None else reply
        self.error, self.calls = error, 0

    async def complete_json(self, system, user, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


def tiny_pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text layer holds these lines (enough for pypdf's text extraction)."""
    ops = "BT /F1 11 Tf 50 750 Td 14 TL " + " ".join(f"({ln}) Tj T*" for ln in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
