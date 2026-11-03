"""The daily watch (spec 7.2): read, compare, confirm, gate, store, summarise."""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from release_watch.models import CatalogProduct, Entry, EntryEvent, FetchAttempt, Snapshot, WatchedSource
from release_watch.watch.compare import Changes, Known, compare, edit_diff, gate, page_suspect
from release_watch.watch.extract import body_hash, extract_markdown
from release_watch.watch.readers import ReadResult, read_source
from release_watch.watch.rules import version_tuple
from release_watch.watch.split import VERSION_RE
from release_watch.watch.successor import propose_successors
from release_watch.watch.summary import summarize

MAX_PARALLEL = 8
OK_OUTCOMES = ("baseline", "same", "changed")


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _State:
    known: list[Known]
    prev_size: int | None
    prev_entries: int


@dataclass
class _Pending:
    source: WatchedSource
    state: _State
    result: ReadResult
    changes: Changes
    events: list = field(default_factory=list)


def _load_state(s, source_id) -> _State:
    rows = s.execute(select(Entry.key, Entry.body_hash, Entry.body, Entry.removed_at)
                     .where(Entry.source_id == source_id)).all()
    prev_size = s.scalar(select(FetchAttempt.bytes).where(FetchAttempt.source_id == source_id,
                                                          FetchAttempt.outcome.in_(OK_OUTCOMES))
                         .order_by(FetchAttempt.fetched_at.desc()).limit(1))
    return _State([Known(r.key, r.body_hash, r.body) for r in rows], prev_size,
                  sum(1 for r in rows if r.removed_at is None))


def _attempt(s, source, outcome, result: ReadResult, detail=None):
    s.add(FetchAttempt(source_id=source.id, outcome=outcome, http_status=result.http_status, bytes=result.size,
                       entries=len(result.entries), detail=detail or result.reason or None))


def _mode(source) -> str:
    follow = source.follow or {}
    if follow.get("pick", {}).get("type") == "versions":
        return "versions"
    if source.kind == "index_page":
        return follow.get("mode", "version")
    return "entries"


def _confirmed(first: Changes, second: Changes) -> Changes:
    """Keep only changes that look identical in both reads."""
    def sig(entry):
        return entry.key, body_hash(entry.body)
    again_new = {sig(e) for e in second.new}
    again_edit = {sig(e) for e, _ in second.edited}
    return Changes(new=[e for e in first.new if sig(e) in again_new],
                   edited=[(e, k) for e, k in first.edited if sig(e) in again_edit],
                   unchanged=first.unchanged, removed=first.removed)


async def run_watch(session_factory, fetcher, llm, *, confirm_delay: float, now: Callable[[], datetime] = utcnow,
                    sleep=asyncio.sleep, only: str | None = None, dry_run: bool = False, out=print) -> dict:
    with session_factory() as s:
        query = select(WatchedSource).where(WatchedSource.state == "active", WatchedSource.login_walled.is_(False))
        sources = [src for src in s.scalars(query)
                   if not only or only.lower() in src.url.lower() or only.lower() in src.vendor_name.lower()]
        states = {src.id: _load_state(s, src.id) for src in sources}

    gate_sem = asyncio.Semaphore(MAX_PARALLEL)

    async def read(src):
        async with gate_sem:
            return await read_source(src, fetcher)

    started = now()
    results = await asyncio.gather(*(read(src) for src in sources))
    counts = dict.fromkeys(("sources", "baseline", "same", "changed", "suspect", "error", "gone",
                            "events_queued", "events_needs_look"), 0)
    counts["sources"] = len(sources)

    if dry_run:
        for src, r in zip(sources, results):
            keys = ", ".join(e.key for e in r.entries[:3])
            out(f"{r.outcome:7} {r.reason or '-':16} {len(r.entries):4}  {src.vendor_name} | {src.title or src.url}"
                f" | {src.url} | {keys}")
            counts[r.outcome if r.outcome in counts else "error"] += 1
        return counts

    pending: list[_Pending] = []
    for src, result in zip(sources, results):
        state = states[src.id]
        p = _settle_first(session_factory, src, state, result, now(), counts)
        if p:
            pending.append(p)

    if pending:
        wait = confirm_delay - (now() - started).total_seconds()
        if wait > 0:
            await sleep(wait)
        again = await asyncio.gather(*(read(p.source) for p in pending))
        for p, second in zip(pending, again):
            await _settle_confirmed(session_factory, fetcher, llm, p, second, now(), counts)
    counts["successors"] = await propose_successors(session_factory, fetcher, now())
    return counts


def _settle_first(sf, src, state: _State, result: ReadResult, at: datetime, counts) -> _Pending | None:
    """Handle everything except confirmed changes; return the source when it needs a confirmation read."""
    with sf() as s:
        row = s.get(WatchedSource, src.id)
        if result.outcome == "gone":
            row.state = "gone"
            _attempt(s, row, "gone", result)
            counts["gone"] += 1
        elif result.outcome == "error":
            _attempt(s, row, "error", result)
            counts["error"] += 1
        elif (reason := page_suspect(state.prev_size, state.prev_entries, result)) or not result.entries:
            row.suspect_streak += 1
            _attempt(s, row, "suspect", result, reason or "no_entries")
            counts["suspect"] += 1
        elif not state.known:
            for e in result.entries:
                s.add(Entry(source_id=row.id, key=e.key, heading=e.heading, body=e.body, body_hash=body_hash(e.body),
                            url=e.url, published_at=e.published_at, first_seen=at, last_seen=at))
            s.add(Snapshot(source_id=row.id, markdown=result.markdown, hash=body_hash(result.markdown)))
            row.suspect_streak, row.last_ok_at = 0, at
            _attempt(s, row, "baseline", result)
            _supersede_predecessor(s, row)
            counts["baseline"] += 1
        else:
            changes = compare(state.known, result.entries)
            if changes.new or changes.edited:
                s.commit()
                return _Pending(src, state, result, changes)
            _touch(s, row, changes, result, at)
            _attempt(s, row, "same", result)
            counts["same"] += 1
        s.commit()
    return None


def _touch(s, row, changes: Changes, result: ReadResult, at: datetime) -> None:
    keys = [e.key for e in result.entries]
    s.execute(update(Entry).where(Entry.source_id == row.id, Entry.key.in_(keys))
              .values(last_seen=at, removed_at=None))
    if changes.removed:
        s.execute(update(Entry).where(Entry.source_id == row.id, Entry.key.in_(changes.removed),
                                      Entry.removed_at.is_(None)).values(removed_at=at))
    row.suspect_streak, row.last_ok_at = 0, at


def _supersede_predecessor(s, row) -> None:
    """A successor's baseline retires the version-pinned source it replaces (spec 7.1)."""
    s.execute(update(WatchedSource).where(WatchedSource.superseded_by == row.id, WatchedSource.state == "active")
              .values(state="superseded"))


async def _settle_confirmed(sf, fetcher, llm, p: _Pending, second: ReadResult, at, counts) -> None:
    src = p.source
    if second.outcome != "ok" or page_suspect(p.state.prev_size, p.state.prev_entries, second):
        with sf() as s:
            _attempt(s, src, "error", second, f"confirmation failed: {second.reason or second.outcome}")
            s.commit()
        counts["error"] += 1
        return
    changes = _confirmed(p.changes, compare(p.state.known, second.entries))
    if not (changes.new or changes.edited):
        with sf() as s:
            row = s.get(WatchedSource, src.id)
            row.suspect_streak += 1
            _attempt(s, row, "suspect", second, "unconfirmed")
            s.commit()
        counts["suspect"] += 1
        return

    mode = _mode(src)
    events = await _build_events(sf, fetcher, src, mode, changes)
    for ev in events:
        if ev.kind in ("new", "edited"):
            ev.summary = await summarize(llm, src.vendor_name, ev.heading, ev.diff or ev.body or "")

    with sf() as s:
        row = s.get(WatchedSource, src.id)
        by_key = {e.key: e for e in s.scalars(select(Entry).where(Entry.source_id == row.id))}
        for e in changes.new:
            s.add(Entry(source_id=row.id, key=e.key, heading=e.heading, body=e.body, body_hash=body_hash(e.body),
                        url=e.url, published_at=e.published_at, first_seen=at, last_seen=at))
        for e, _ in changes.edited:
            stored = by_key[e.key]
            stored.heading, stored.body, stored.body_hash = e.heading, e.body, body_hash(e.body)
        _touch(s, row, changes, second, at)
        s.add(Snapshot(source_id=row.id, markdown=second.markdown, hash=body_hash(second.markdown)))
        for ev in events:
            if ev.dedupe_key and s.scalar(select(func.count()).select_from(EntryEvent)
                                          .where(EntryEvent.dedupe_key == ev.dedupe_key)):
                continue
            s.add(ev)
            counts["events_queued" if ev.status == "queued" else "events_needs_look"] += 1
        _attempt(s, row, "changed", second)
        s.commit()
    counts["changed"] += 1


def _version_of(*texts: str) -> str | None:
    for text in texts:
        if text and (m := VERSION_RE.search(text)):
            return m.group(0).lstrip("vV")
    return None


async def _build_events(sf, fetcher, src, mode: str, changes: Changes) -> list[EntryEvent]:
    decided = gate(changes)
    if mode == "entries":
        return [EntryEvent(id=uuid.uuid4(), source_id=src.id, kind=kind, heading=entry.heading,
                           body=entry.body, diff=edit_diff(known.body, entry.body)[0] if known else None,
                           url=entry.url or src.url, status=status, reason=reason)
                for kind, entry, known, status, reason in decided]

    new = [(entry, status, reason) for kind, entry, _, status, reason in decided if kind == "new"]
    if mode == "entry":  # one page per rolling release: the child is the entry, sent verbatim
        events = []
        for entry, status, reason in new:
            child = await fetcher.fetch(entry.url)
            body = extract_markdown(child.text, entry.url, (src.follow or {}).get("child_selector")) \
                if child.outcome == "ok" else ""
            if not body:
                status, reason = "needs_look", "child_fetch_failed"
            events.append(EntryEvent(id=uuid.uuid4(), source_id=src.id, kind="new", heading=entry.heading,
                                     body=body or None, url=entry.url, status=status, reason=reason))
        return events

    # Version lines: index pages of per-release documents, and version pages.
    with sf() as s:
        product = s.get(CatalogProduct, src.product_de_id) if src.product_de_id else None
        known_versions = [k.key for k in _load_state(s, src.id).known]
    if mode == "versions":
        candidates = [(e, st, rs) for e, st, rs in new[:1]
                      if all(version_tuple(e.key) > version_tuple(k) for k in known_versions)]
    else:
        candidates = new
    events = []
    for entry, status, reason in candidates:
        version = entry.key if mode == "versions" else _version_of(entry.heading, entry.url or "")
        if version is None:
            events.append(EntryEvent(id=uuid.uuid4(), source_id=src.id, kind="version", heading=entry.heading,
                                     url=entry.url, status="needs_look", reason="no_version"))
            continue
        if product and product.de_version and version_tuple(version) <= version_tuple(product.de_version):
            continue
        name = product.name if product else (src.title or src.url)
        scope = src.product_de_id or src.id
        events.append(EntryEvent(
            id=uuid.uuid4(), source_id=src.id, kind="version", version=version, product_de_id=src.product_de_id,
            heading=f"{src.vendor_name} released a new version of {name}: {version}", url=entry.url or src.url,
            dedupe_key=f"version:{src.vendor_name}:{scope}:{version}", status=status, reason=reason))
    return events
