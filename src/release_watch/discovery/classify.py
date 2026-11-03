"""Fetch each candidate live and label it for the owner's review. Labels only inform; the owner decides."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import trafilatura
from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import select

from release_watch.fetch import FetchResult
from release_watch.llm import LLMError
from release_watch.models import WatchedSource

LLM_TEXT_CHARS = 12_000
MAX_PARALLEL = 8
THIN_TEXT_CHARS = 200

SYSTEM_PROMPT = """You classify vendor documentation pages for a service that watches release notes.
A page counts as release notes when its main purpose is to list product releases or changes over time:
release notes, a changelog, "what's new", product updates, revision history. A page that only mentions
release notes, or documents how to use the product, does not count.

Reply with a JSON object:
{"label": "release_notes" | "not_release_notes" | "unsure",
 "kind": "entry_page" | "index_page" | "unknown",
 "evidence": "<a heading or sentence copied exactly from the page that supports the label>"}

kind: "entry_page" when the page itself holds dated or versioned entries; "index_page" when it mainly
links to one page or document per release. Copy the evidence verbatim; do not paraphrase."""


@dataclass
class Label:
    label: str
    kind: str
    reason: str
    evidence: str


def extract_text(html: str, url: str) -> str:
    text = trafilatura.extract(html, url=url, output_format="markdown", include_links=True)
    if text:
        return text
    body = LexborHTMLParser(html).body
    return body.text(separator="\n", strip=True) if body else ""


def looks_like_feed(result: FetchResult) -> bool:
    ctype = result.content_type.lower()
    if any(t in ctype for t in ("rss", "atom")) or ("xml" in ctype and "html" not in ctype):
        return True
    head = result.text.lstrip()[:500]
    return head.startswith("<?xml") and ("<rss" in head or "<feed" in head)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


async def label_candidate(source: WatchedSource, fetcher, llm) -> Label:
    result = await fetcher.fetch(source.url)
    if result.outcome == "blocked":
        return Label("blocked", source.kind, result.reason, "")
    if result.outcome == "gone":
        return Label("unsure", source.kind, "gone", "")
    if result.outcome == "error":
        return Label("unsure", source.kind, "fetch_failed", "")

    if "pdf" in result.content_type.lower() or result.text.startswith("%PDF"):
        return Label("unsure", source.kind, "pdf_unparsed", "")

    if looks_like_feed(result):
        feed = feedparser.parse(result.text)
        if feed.entries:
            return Label("release_notes", "feed", "feed", feed.entries[0].get("title", ""))
        return Label("unsure", "feed", "empty_feed", "")

    text = extract_text(result.text, source.url)
    if len(text) < THIN_TEXT_CHARS:  # a JS shell or search widget, not something the LLM can judge
        return Label("unsure", source.kind, "thin_text", text[:200])
    user = json.dumps({"vendor": source.vendor_name, "title": source.title, "url": source.url,
                       "text": text[:LLM_TEXT_CHARS]}, ensure_ascii=False)
    try:
        reply = await llm.complete_json(SYSTEM_PROMPT, user)
    except LLMError:
        return Label("unsure", source.kind, "llm_failed", "")

    label = reply.get("label")
    kind = reply.get("kind") if reply.get("kind") in ("entry_page", "index_page") else source.kind
    evidence = str(reply.get("evidence") or "")
    if label not in ("release_notes", "not_release_notes", "unsure"):
        return Label("unsure", source.kind, "llm_bad_reply", evidence)
    # A release-notes label must point at text that is really on the page.
    if label == "release_notes" and (not evidence or _squash(evidence) not in _squash(text)):
        return Label("unsure", kind, "evidence_not_found", evidence)
    return Label(label, kind, "llm", evidence)


async def label_pending(session_factory, fetcher, llm, *, relabel: bool = False) -> dict[str, int]:
    with session_factory() as s:
        query = select(WatchedSource).where(WatchedSource.state == "candidate")
        if not relabel:
            query = query.where(WatchedSource.label.is_(None))
        sources = list(s.scalars(query))

    gate = asyncio.Semaphore(MAX_PARALLEL)

    async def one(src: WatchedSource):
        async with gate:
            return src.id, await label_candidate(src, fetcher, llm)

    counts: dict[str, int] = {}
    first_error: BaseException | None = None
    for next_done in asyncio.as_completed([one(src) for src in sources]):
        try:
            source_id, label = await next_done
        except Exception as exc:  # keep labelling the rest; progress is committed per row
            first_error = first_error or exc
            continue
        with session_factory() as s:
            row = s.get(WatchedSource, source_id)
            row.label, row.label_reason = label.label, label.reason
            if row.origin != "seed":  # a hand-set kind (e.g. section) is not the LLM's to change
                row.kind = label.kind
            row.label_evidence, row.labelled_at = label.evidence[:2000], datetime.now(UTC)
            s.commit()
        counts[label.label] = counts.get(label.label, 0) + 1
    if first_error:
        raise first_error
    return counts
