"""Turn a fetched page into Markdown (verbatim, sent to the owner) and a normalised form (comparison only)."""

import hashlib
import re

import trafilatura
from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser

# Per-page chrome that changes without the content changing (spec 7.2 step 3).
NOISE_PATTERNS = re.compile(
    r"^.*\b(last (updated|visited|modified)|\d+ views?|was this (article|page) helpful|print this page)\b.*$",
    re.I | re.M,
)
_LINK_QUERY = re.compile(r"(\]\([^)\s?]+)\?[^)\s]*")
_BARE_QUERY = re.compile(r"(https?://[^\s)?\]]+)\?[^\s)\]]*")


# Main-content containers, most specific first. Readability extractors (trafilatura) drop headings that
# hold anchors (MadCap Flare) and trim lists, so a real content node converted as-is is preferred.
CONTENT_SELECTORS = ("[role=main]", "main", "article", "#mc-main-content", ".article-body", "#content")
MIN_CONTENT_CHARS = 200
CHROME_SELECTORS = "nav, header, footer, aside, script, style, noscript, form"


def html_to_markdown(fragment: str) -> str:
    return markdownify(fragment, heading_style="ATX", bullets="*").strip()


def extract_markdown(html: str, url: str, selector: str | None = None) -> str:
    if selector:
        node = LexborHTMLParser(html).css_first(selector)
        return html_to_markdown(node.html) if node is not None else ""
    tree = LexborHTMLParser(html)
    for candidate in CONTENT_SELECTORS:
        node = tree.css_first(candidate)
        if node is not None and len(node.text(strip=True)) >= MIN_CONTENT_CHARS:
            return html_to_markdown(node.html)
    if tree.body is not None:
        for chrome in tree.body.css(CHROME_SELECTORS):
            chrome.decompose()
        md = html_to_markdown(tree.body.html)
        if md:
            return md
    return trafilatura.extract(html, url=url, output_format="markdown", include_formatting=True,
                               include_tables=True, include_links=True) or ""


def normalize(text: str) -> str:
    text = _LINK_QUERY.sub(r"\1", text)
    text = _BARE_QUERY.sub(r"\1", text)
    text = NOISE_PATTERNS.sub("", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def body_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def punct_insensitive(text: str) -> str:
    return re.sub(r"[\W_]+", "", normalize(text))
