"""Compare fresh entries with stored ones, diff edits, and apply the plausibility gates (spec 7.2-7.3)."""

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime

from release_watch.watch.extract import body_hash, punct_insensitive
from release_watch.watch.readers import ReadResult
from release_watch.watch.split import Entry

# Page furniture that changes on its own: "Last updated 21 days ago", "[PreviousX](..)[NextY](..)" footers.
_CHROME = re.compile(r"^(last (updated|modified)\b.{0,60}|(\[(previous|next)[^\]]*\]\([^)]*\)\s*)+)$", re.I)
_MARKER = re.compile(r"^\s*(#{1,6}\s+|[*+-]\s+|\d+\.\s+)")
_TOKEN = re.compile(r"\s+|\w[\w.-]*\w|\w|[^\w\s]")

MAX_NEW = 3
MAX_EDIT_RATIO = 0.5
SHRINK_RATIO = 0.2


@dataclass
class Known:
    key: str
    body_hash: str
    body: str
    pending_hash: str | None = None
    pending_since: datetime | None = None


@dataclass
class Changes:
    new: list[Entry] = field(default_factory=list)
    edited: list[tuple[Entry, Known]] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)
    rekeyed: list[tuple[Entry, Known]] = field(default_factory=list)  # same body under a new key
    matched: list[tuple[Entry, Known]] = field(default_factory=list)  # every unchanged entry and its stored row


def compare(known: list[Known], fresh: list[Entry]) -> Changes:
    """Match by body first, then by key: a known body under another key (a renamed heading, or an older
    same-heading entry pushed down to "x (2)") is never new or edited."""
    by_hash: dict[str, Known] = {}
    for k in known:
        by_hash.setdefault(k.body_hash, k)
    by_key = {k.key: k for k in known}
    consumed: set[str] = set()
    changes = Changes()
    rest = []
    for entry in fresh:
        old = by_hash.get(body_hash(entry.body))
        if old is not None and old.key not in consumed:
            consumed.add(old.key)
            changes.unchanged += 1
            changes.matched.append((entry, old))
            if old.key != entry.key:
                changes.rekeyed.append((entry, old))
        else:
            rest.append(entry)
    for entry in rest:
        old = by_key.get(entry.key)
        if old is None or old.key in consumed:
            changes.new.append(entry)
            continue
        consumed.add(old.key)
        if _comparable(old.body) == _comparable(entry.body):
            changes.unchanged += 1
            changes.matched.append((entry, old))
        else:
            changes.edited.append((entry, old))
    changes.removed = [k.key for k in known if k.key not in consumed]
    return changes


def _content_lines(text: str) -> list[str]:
    """The text's lines without list and heading markers, blank lines or page furniture."""
    lines = (_MARKER.sub("", line).strip() for line in text.splitlines())
    return [line for line in lines if line and not _CHROME.match(line)]


def _comparable(body: str) -> str:
    return punct_insensitive("\n".join(_content_lines(body)))


def _marked(tokens: list[str], before: str, after: str) -> str:
    """Wrap a run of changed words, keeping the spaces (and a leading comma or the like) outside the markers."""
    text = "".join(tokens)
    lead = re.match(r"[\s,;:.]*", text).group(0)
    core = text[len(lead):].rstrip()
    return lead + before + core + after + text[len(lead) + len(core):] if core else text


def _changed_line(old: str, new: str) -> str:
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    words = difflib.SequenceMatcher(None, old.split(), new.split(), autojunk=False)
    if words.ratio() < 0.5:  # rewritten, not edited: show it whole
        return f"{new} _(was: {old})_"
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append("".join(b[j1:j2]))
        elif op == "insert":
            out.append(_marked(b[j1:j2], "**", "**"))
        elif op == "delete":
            out.append(_marked(a[i1:i2], "_(removed: ", ")_"))
        else:
            out.append(_marked(b[j1:j2], "**", f"** _(was: {''.join(a[i1:i2]).strip()})_"))
    return "".join(out)


def readable_diff(old: str, new: str) -> str:
    """What changed in an entry, for people: changed lines with the new words in bold and the old ones after
    them, then added and removed lines. Teams cards have no strikethrough, hence "(was: ...)"."""
    a, b = _content_lines(old), _content_lines(new)
    changed, added, removed = [], [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "insert":
            added += b[j1:j2]
        elif op == "delete":
            removed += a[i1:i2]
        elif op == "replace":
            olds, news = a[i1:i2], b[j1:j2]
            changed += [_changed_line(o, n) for o, n in zip(olds, news)]
            removed += olds[len(news):]
            added += news[len(olds):]
    sections = [(title, lines) for title, lines in (("Changed", changed), ("Added", added), ("Removed", removed))
                if lines]
    return "\n\n".join(f"**{title}**\n" + "\n".join(f"- {line}" for line in lines) for title, lines in sections)


def edit_diff(old: str, new: str) -> tuple[str, float]:
    a, b = old.splitlines(), new.splitlines()
    diff = "\n".join(difflib.unified_diff(a, b, lineterm="", n=1))
    return diff, 1 - difflib.SequenceMatcher(None, a, b).ratio()


def page_suspect(prev_size: int | None, prev_entries: int, result: ReadResult) -> str | None:
    if result.outcome == "suspect":
        return result.reason
    if prev_size and result.size < SHRINK_RATIO * prev_size:
        return "shrunk"
    if prev_entries and not result.entries:
        return "no_entries"
    return None


def gate(changes: Changes, max_new: int = MAX_NEW, max_edit_ratio: float = MAX_EDIT_RATIO):
    """(kind, entry, known, status, reason) for each change."""
    out = []
    too_many = len(changes.new) > max_new
    for entry in changes.new:
        out.append(("new", entry, None, "needs_look" if too_many else "queued", "too_many_new" if too_many else None))
    for entry, known in changes.edited:
        _, ratio = edit_diff(known.body, entry.body)
        big = ratio > max_edit_ratio
        out.append(("edited", entry, known, "needs_look" if big else "queued", "edit_too_large" if big else None))
    return out
