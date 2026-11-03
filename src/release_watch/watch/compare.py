"""Compare fresh entries with stored ones, diff edits, and apply the plausibility gates (spec 7.2-7.3)."""

import difflib
from dataclasses import dataclass, field

from release_watch.watch.extract import body_hash, punct_insensitive
from release_watch.watch.readers import ReadResult
from release_watch.watch.split import Entry

MAX_NEW = 3
MAX_EDIT_RATIO = 0.5
SHRINK_RATIO = 0.2


@dataclass
class Known:
    key: str
    body_hash: str
    body: str


@dataclass
class Changes:
    new: list[Entry] = field(default_factory=list)
    edited: list[tuple[Entry, Known]] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)


def compare(known: list[Known], fresh: list[Entry]) -> Changes:
    by_key = {k.key: k for k in known}
    hashes = {k.body_hash for k in known}
    changes = Changes()
    for entry in fresh:
        h = body_hash(entry.body)
        old = by_key.get(entry.key)
        if old is None:
            if h in hashes:  # same body under a renamed or reordered heading
                changes.unchanged += 1
            else:
                changes.new.append(entry)
        elif old.body_hash == h or punct_insensitive(old.body) == punct_insensitive(entry.body):
            changes.unchanged += 1
        else:
            changes.edited.append((entry, old))
    fresh_keys = {e.key for e in fresh}
    changes.removed = [k.key for k in known if k.key not in fresh_keys]
    return changes


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
