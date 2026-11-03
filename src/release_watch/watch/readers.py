"""Read one watched source into entries, whatever its kind."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import feedparser

from release_watch.models import WatchedSource
from release_watch.urls import normalize_url
from release_watch.watch.extract import extract_markdown, html_to_markdown
from release_watch.watch.rules import children, json_entries
from release_watch.watch.split import Entry, split_entries

ReadOutcome = Literal["ok", "suspect", "error", "gone"]


@dataclass
class ReadResult:
    outcome: ReadOutcome
    reason: str
    entries: list[Entry] = field(default_factory=list)
    size: int = 0
    markdown: str = ""
    final_url: str = ""
    http_status: int | None = None


def _failed(result) -> ReadResult | None:
    mapped = {"blocked": "suspect", "gone": "gone", "error": "error"}.get(result.outcome)
    if mapped:
        return ReadResult(mapped, result.reason, size=result.bytes, final_url=result.final_url,
                          http_status=result.status)
    return None


def _feed_entries(text: str) -> list[Entry]:
    entries = []
    for item in feedparser.parse(text).entries:
        title = item.get("title", "").strip()
        content = item.get("content")
        html = content[0].get("value", "") if content else item.get("summary", "")
        published = item.get("published_parsed") or item.get("updated_parsed")
        entries.append(Entry(
            key=str(item.get("id") or item.get("link") or title), heading=title,
            body=f"## {title}\n\n{html_to_markdown(html)}".strip(), url=item.get("link"),
            published_at=datetime(*published[:6], tzinfo=UTC) if published else None,
        ))
    return entries


async def read_source(source: WatchedSource, fetcher) -> ReadResult:
    if source.kind == "index_page":
        follow = source.follow or {"pick": {"type": "links"}}
        result, kids = await children(fetcher, source.url, follow)
        if failed := _failed(result):
            return failed
        if follow.get("pick", {}).get("type") == "versions":
            entries = [Entry(key=k.title, heading=k.title, body=k.title) for k in kids]
        else:
            # The body must differ per child: compare() treats a known body under a new key as a rename.
            entries = [Entry(key=normalize_url(k.url), heading=k.title, body=f"[{k.title}]({k.url})", url=k.url)
                       for k in kids]
        return ReadResult("ok", "", entries, result.bytes, "", result.final_url, result.status)

    result = await fetcher.fetch(source.url)
    if failed := _failed(result):
        return failed
    done = dict(size=result.bytes, final_url=result.final_url, http_status=result.status)
    if "pdf" in result.content_type.lower() or result.text.startswith("%PDF"):
        return ReadResult("suspect", "pdf_unparsed", **done)
    if source.kind == "feed":
        return ReadResult("ok", "", _feed_entries(result.text), markdown="", **done)
    rule = source.split_rule or {}
    if "json" in rule:
        return ReadResult("ok", "", json_entries(result.text, rule["json"]), **done)
    md = extract_markdown(result.text, source.url, source.selector)
    return ReadResult("ok", "", split_entries(md, rule), markdown=md, **done)
