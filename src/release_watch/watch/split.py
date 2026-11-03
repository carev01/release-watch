"""Split a release-notes page into entries: one per date or version heading (spec 7.2 step 4)."""

import re
from dataclasses import dataclass
from datetime import datetime

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
DATE_RE = re.compile(
    rf"\b(?:\d{{4}}-\d{{1,2}}(?:-\d{{1,2}})?|\d{{1,2}}/\d{{1,2}}/\d{{4}}|"
    rf"{_MONTH}\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?\d{{4}}|\d{{1,2}}\s+{_MONTH}\.?,?\s+\d{{4}})\b",
    re.I,
)
VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){1,3}(?:[-.][0-9A-Za-z]+)?\b|\b\d{2}-\d\b")

BOLD_LEVEL = 5
_ATX = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_BOLD = re.compile(r"^\*\*(.+?)\*\*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Entry:
    key: str
    heading: str
    body: str
    url: str | None = None
    published_at: datetime | None = None


def entry_key(heading: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[#*_]+", " ", heading)).strip().casefold()


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, heading text), skipping fenced code."""
    found, in_fence = [], False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if m := _ATX.match(line):
            found.append((i, len(m.group(1)), m.group(2).strip()))
        elif m := _BOLD.match(line.strip()):
            found.append((i, BOLD_LEVEL, m.group(1).strip()))
    return found


def _looks_dated(text: str) -> bool:
    return bool(DATE_RE.search(text) or VERSION_RE.search(text))


def split_entries(markdown: str, rule: dict | None = None) -> list[Entry]:
    rule = rule or {}
    if rule.get("whole"):
        key = rule.get("key", "page")
        return [Entry(key=key, heading=key, body=markdown.strip())]

    lines = markdown.splitlines()
    heads = _headings(lines)
    if "heading_regex" in rule:
        pattern = re.compile(rule["heading_regex"], re.I)
        starts = [h for h in heads if pattern.search(h[2])]
    else:
        level = rule.get("heading_level")
        if level is None:
            counts: dict[int, int] = {}
            for _, lvl, text in heads:
                if _looks_dated(text):
                    counts[lvl] = counts.get(lvl, 0) + 1
            if not counts:
                return []
            level = min(counts, key=lambda lvl: (-counts[lvl], lvl))
        starts = [h for h in heads if h[1] == level]

    entries: list[Entry] = []
    seen: dict[str, int] = {}
    for idx, lvl, text in starts:
        end = next((j for j, l2, _ in heads if j > idx and l2 <= lvl), len(lines))
        key = entry_key(text)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key = f"{key} ({seen[key]})"
        entries.append(Entry(key=key, heading=text, body="\n".join(lines[idx:end]).strip()))
    return entries
