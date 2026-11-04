"""Newest-child rules for index pages and moving targets (spec 7.1), and JSON listings as entries.

A rule is plain JSON stored on the source (`follow`), so a new vendor quirk is a seed entry, not code:
  {"pick": {...}, "newest": "first" | "max_number", "then": {<nested rule>}, "url_rewrite": [[from, to]]}
Pick types: links, sitemap, json, versions (see pick_children).
"""

import html as htmllib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin

import feedparser
from selectolax.lexbor import LexborHTMLParser

from release_watch.urls import normalize_url
from release_watch.watch.extract import html_to_markdown
from release_watch.watch.split import VERSION_RE, Entry

PICK_TYPES = {"links", "sitemap", "json", "versions", "feed"}
_SEGMENT = re.compile(r"^([^\[\]]+)(?:\[([^=\]]+)=([^\]]*)\])?$")


@dataclass
class Child:
    url: str
    title: str
    number: float | None = None


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v))


def json_path(data, path: str) -> list:
    """Dotted path; `*` flattens a list; `name[key=value]` keeps list items whose key equals value."""
    current = [data]
    for seg in path.split(".") if path else []:
        if seg == "*":
            current = [x for c in current if isinstance(c, list) for x in c]
            continue
        m = _SEGMENT.match(seg)
        name, key, value = m.group(1), m.group(2), m.group(3)
        current = [c[name] for c in current if isinstance(c, dict) and name in c]
        if key is not None:
            current = [x for c in current for x in (c if isinstance(c, list) else [c])
                       if isinstance(x, dict) and str(x.get(key)) == value]
    out = []
    for c in current:
        out.extend(c if isinstance(c, list) else [c])
    return out


def _number(pick: dict, title: str, url: str) -> float | None:
    if not pick.get("number_regex"):
        return None
    for text in (title, url):
        if m := re.search(pick["number_regex"], text):
            return float(m.group(1))
    return None


def _dedupe(kids: list[Child]) -> list[Child]:
    seen, out = set(), []
    for k in kids:
        key = normalize_url(k.url) if k.url.startswith("http") else k.url
        if key not in seen:
            seen.add(key)
            out.append(k)
    return out


def _links(body: str, base_url: str, pick: dict) -> list[Child]:
    tree = LexborHTMLParser(body)
    anchors = []
    for node in tree.css(pick.get("selector", "a")):
        anchors.extend([node] if node.tag == "a" else node.css("a"))
    href_re = re.compile(pick["href_regex"]) if pick.get("href_regex") else None
    text_re = re.compile(pick["text_regex"], re.I) if pick.get("text_regex") else None
    kids = []
    for a in anchors:
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base_url, href)
        title = re.sub(r"\s+", " ", a.text(strip=True))
        if (href_re and not href_re.search(url)) or (text_re and not text_re.search(title)):
            continue
        kids.append(Child(url, title, _number(pick, title, url)))
    return _dedupe(kids)


def _versions(body: str, content_type: str, base_url: str, pick: dict) -> list[Child]:
    if pick.get("json_path"):
        texts = [str(v) for v in json_path(json.loads(body), pick["json_path"])]
    elif pick.get("raw"):  # match the markup itself, e.g. a JSON version list in a data-items attribute
        texts = [htmllib.unescape(body)]
    elif "html" in content_type.lower() or body.lstrip().startswith("<"):
        root = LexborHTMLParser(body).body
        texts = [root.text(separator="\n") if root else body]
    else:
        texts = [body]
    regex = re.compile(pick["regex"]) if pick.get("regex") else None
    found = []
    for text in texts:
        if regex:
            found.extend(m.group(1) for m in regex.finditer(text))
        else:
            found.extend(m.group(0) for m in VERSION_RE.finditer(text))
    unique = list(dict.fromkeys(v.strip() for v in found if v.strip()))
    unique.sort(key=version_tuple, reverse=True)
    return [Child(base_url, v) for v in unique]


def pick_children(body: str, content_type: str, base_url: str, pick: dict) -> list[Child]:
    kind = pick.get("type", "links")
    if kind == "links":
        return _links(body, base_url, pick)
    if kind == "sitemap":
        regex = re.compile(pick.get("loc_regex", "."))
        locs = [htmllib.unescape(m.strip()) for m in re.findall(r"<loc>(.*?)</loc>", body, re.S)]
        return _dedupe([Child(u, u, _number(pick, u, u)) for u in locs if regex.search(u)])
    if kind == "json":
        items = [i for i in json_path(json.loads(body), pick.get("items", "")) if isinstance(i, dict)]
        if pick.get("order_by"):
            items.sort(key=lambda i: (i.get(pick["order_by"]) is None, i.get(pick["order_by"]) or 0),
                       reverse=pick.get("order", "asc") == "desc")
        kids = []
        capture = pick.get("capture")
        for item in items:
            if capture:  # regex groups from one field, usable in url_template as {_1}, {_2}, ...
                m = re.search(capture["regex"], str(item.get(capture["field"], "")))
                if not m:
                    continue
                item = {**item, **{f"_{i}": g for i, g in enumerate(m.groups(), 1)}}
            if pick.get("url_template"):
                url = pick["url_template"].format(**item)
            else:
                url = urljoin(base_url, str(item.get(pick.get("url", "url"), "")))
            title = str(item.get(pick.get("title", "title"), ""))
            kids.append(Child(url, title, _number(pick, title, url)))
        return _dedupe(kids)
    if kind == "versions":
        return _versions(body, content_type, base_url, pick)
    if kind == "feed":  # a feed whose items are short teasers: each item's page holds the release notes
        items = feedparser.parse(body).entries
        return _dedupe([Child(urljoin(base_url, i.get("link", "")), i.get("title", "").strip(), None)
                        for i in items if i.get("link")])
    raise ValueError(f"unknown pick type {kind!r}")


def newest(kids: list[Child], how: str = "first") -> Child:
    if how == "max_number":
        return max(kids, key=lambda k: float("-inf") if k.number is None else k.number)
    return kids[0]


def _rewrite(url: str, rewrites: list) -> str:
    for old, new in rewrites:
        url = url.replace(old, new)
    return url


async def children(fetcher, url: str, follow: dict):
    """Fetch url, apply the rule, and follow `then` through the newest child. Returns (last FetchResult, children)."""
    result = await fetcher.fetch(url)
    if result.outcome != "ok":
        return result, []
    kids = pick_children(result.text, result.content_type, result.final_url, follow.get("pick", {"type": "links"}))
    if follow.get("then") and kids:
        return await children(fetcher, newest(kids, follow.get("newest", "first")).url, follow["then"])
    rewrites = follow.get("url_rewrite", [])
    return result, [Child(_rewrite(k.url, rewrites), k.title, k.number) for k in kids]


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def json_entries(text: str, rule: dict) -> list[Entry]:
    entries = []
    for item in json_path(json.loads(text), rule.get("items", "")):
        if not isinstance(item, dict):
            continue
        title = str(item.get(rule.get("title", "title"), "")).strip()
        raw = item.get(rule.get("body", "body")) or ""
        if isinstance(raw, list):  # a list of change lines (Gearset)
            raw = [html_to_markdown(str(x)) if rule.get("body_format", "html") == "html" else str(x).strip()
                   for x in raw]
            body = "\n".join(f"* {x}" for x in raw if x)
        else:
            body = html_to_markdown(str(raw)) if rule.get("body_format", "html") == "html" else str(raw).strip()
        entries.append(Entry(
            key=str(item.get(rule.get("key", "id"), title)), heading=title, body=f"## {title}\n\n{body}".strip(),
            url=item.get(rule["url"]) if rule.get("url") else None, published_at=_parse_date(item.get(rule.get("date", ""))),
        ))
    return entries
