"""The daily watch (spec 7.2): read, compare, confirm, gate, store, summarise."""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from release_watch.models import CatalogProduct, CatalogSource, Entry, EntryEvent, FetchAttempt, Snapshot, WatchedSource
from release_watch.urls import host_of, normalize_url
from release_watch.watch.compare import Changes, Known, compare, gate, page_suspect, readable_diff
from release_watch.watch.extract import body_hash, extract_markdown
from release_watch.watch.readers import ReadResult, read_source
from release_watch.watch.retire import retire_older_versions
from release_watch.watch.rules import newer_than_tracked, same_version_line, version_tuple
from release_watch.watch.split import heading_version
from release_watch.watch.successor import on_successor_baseline, propose_successors
from release_watch.watch.summary import summarize

MAX_PARALLEL = 8
OK_OUTCOMES = ("baseline", "same", "changed")
EDIT_HOLD = timedelta(hours=20)  # an edit must still be there on the next daily run


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
    rows = s.execute(select(Entry.key, Entry.body_hash, Entry.body, Entry.removed_at, Entry.pending_hash,
                            Entry.pending_since).where(Entry.source_id == source_id)).all()
    prev_size = s.scalar(select(FetchAttempt.bytes).where(FetchAttempt.source_id == source_id,
                                                          FetchAttempt.outcome.in_(OK_OUTCOMES))
                         .order_by(FetchAttempt.fetched_at.desc()).limit(1))
    return _State([Known(r.key, r.body_hash, r.body, r.pending_hash, r.pending_since) for r in rows], prev_size,
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
    """The confirmation read's changes, keeping only new and edited entries identical in both reads."""
    def sig(entry):
        return entry.key, body_hash(entry.body)
    first_new = {sig(e) for e in first.new}
    first_edit = {sig(e) for e, _ in first.edited}
    return Changes(new=[e for e in second.new if sig(e) in first_new],
                   edited=[(e, k) for e, k in second.edited if sig(e) in first_edit],
                   unchanged=second.unchanged, removed=second.removed, rekeyed=second.rekeyed,
                   matched=second.matched)


async def run_watch(session_factory, fetcher, llm, *, confirm_delay: float, now: Callable[[], datetime] = utcnow,
                    sleep=asyncio.sleep, only: str | None = None, dry_run: bool = False, out=print,
                    login_hosts: tuple[str, ...] | None = None, same_visit_edits: bool = False) -> dict:
    """The daily watch, or with login_hosts a weekly login visit: only the login-walled sources on those hosts
    (read through a signed-in fetcher). same_visit_edits sends an edit seen on both reads of this run at once."""
    login_run = login_hosts is not None
    with session_factory() as s:
        query = select(WatchedSource).where(WatchedSource.state == "active",
                                            WatchedSource.login_walled.is_(login_run))
        sources = [src for src in s.scalars(query)
                   if (not login_run or host_of(src.url) in login_hosts)
                   and (not only or only.lower() in src.url.lower() or only.lower() in src.vendor_name.lower())]
        states = {src.id: _load_state(s, src.id) for src in sources}

    gate_sem = asyncio.Semaphore(MAX_PARALLEL)

    async def read(src):
        async with gate_sem:
            try:
                return await read_source(src, fetcher)
            except Exception as exc:  # one broken page must not stop the run; it shows as an error
                return ReadResult("error", f"crash: {type(exc).__name__}")

    started = now()
    results = await asyncio.gather(*(read(src) for src in sources))
    counts = dict.fromkeys(("sources", "baseline", "same", "changed", "suspect", "error", "gone",
                            "events_queued", "events_needs_look"), 0)
    counts["sources"] = len(sources)

    if dry_run:
        counts = {"sources": len(sources), "ok": 0, "suspect": 0, "error": 0, "gone": 0}
        for src, r in zip(sources, results):
            keys = ", ".join(e.key for e in r.entries[:3])
            out(f"{r.outcome:7} {r.reason or '-':16} {len(r.entries):4}  {src.vendor_name} | {src.title or src.url}"
                f" | {src.url} | {keys}")
            counts[r.outcome] += 1
        return counts

    pending: list[_Pending] = []
    for src, result in zip(sources, results):
        try:
            p = _settle_first(session_factory, src, states[src.id], result, now(), counts, login_run)
        except Exception as exc:  # one source's storage failure must not stop the others
            _record_crash(session_factory, src, exc, counts)
            continue
        if p:
            pending.append(p)

    if pending:
        wait = confirm_delay - (now() - started).total_seconds()
        if wait > 0:
            await sleep(wait)
        again = await asyncio.gather(*(read(p.source) for p in pending))
        for p, second in zip(pending, again):
            try:
                await _settle_confirmed(session_factory, fetcher, llm, p, second, now(), counts,
                                        same_visit_edits=same_visit_edits)
            except Exception as exc:
                _record_crash(session_factory, p.source, exc, counts)
    if login_run:  # successors and retirement are the daily watch's business
        return counts
    counts["successors"] = await propose_successors(session_factory, fetcher, now())
    with session_factory() as s:
        counts["retired"] = retire_older_versions(s, now())
        s.commit()
    return counts


def _record_crash(sf, src, exc: Exception, counts) -> None:
    with sf() as s:
        s.add(FetchAttempt(source_id=src.id, outcome="error", detail=f"crash: {type(exc).__name__}"))
        s.commit()
    counts["error"] += 1


def _settle_first(sf, src, state: _State, result: ReadResult, at: datetime, counts,
                  login_run: bool = False) -> _Pending | None:
    """Handle everything except confirmed changes; return the source when it needs a confirmation read."""
    if login_run and result.outcome == "gone":  # a signed-out session can answer 404: not read, never gone
        result = ReadResult("error", f"not read ({result.reason or 'gone'})", size=result.size,
                            final_url=result.final_url, http_status=result.http_status)
    with sf() as s:
        row = s.get(WatchedSource, src.id)
        if result.outcome in ("ok", "gone"):
            _docextractor_moved(s, row, result)
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
            if login_run and result.markdown:  # a weekly login page: keep its text to tune splitting offline
                s.add(Snapshot(source_id=row.id, markdown=result.markdown, hash=body_hash(result.markdown)))
            counts["suspect"] += 1
        elif not state.known:
            for e in result.entries:
                s.add(Entry(source_id=row.id, key=e.key, heading=e.heading, body=e.body, body_hash=body_hash(e.body),
                            url=e.url, published_at=e.published_at, first_seen=at, last_seen=at))
            s.add(Snapshot(source_id=row.id, markdown=result.markdown, hash=body_hash(result.markdown)))
            row.suspect_streak, row.last_ok_at = 0, at
            _attempt(s, row, "baseline", result)
            on_successor_baseline(s, row)
            _baseline_versions(s, row, result.entries)
            counts["baseline"] += 1
        else:
            changes = compare(state.known, result.entries)
            if changes.new or changes.edited:
                s.commit()
                return _Pending(src, state, result, changes)
            _write_entries(s, row, changes, at)
            _attempt(s, row, "same", result)
            counts["same"] += 1
        s.commit()
    return None


def _write_entries(s, row, changes: Changes, at: datetime, *, new: list | None = None,
                   fire: list | None = None, pend: dict | None = None) -> None:
    """Rewrite the source's stored entries from today's read. Unconfirmed new entries are left out and
    unconfirmed or held edits keep their stored body, so tomorrow's read detects them again."""
    old = {e.key: e for e in s.scalars(select(Entry).where(Entry.source_id == row.id))}
    rows: dict[str, dict] = {}

    def keep(key, src: Entry, **changes_):
        rows[key] = dict(key=key, heading=src.heading, body=src.body, body_hash=src.body_hash, url=src.url,
                         published_at=src.published_at, first_seen=src.first_seen, last_seen=at, removed_at=None,
                         pending_hash=src.pending_hash, pending_since=src.pending_since) | changes_

    for entry, known in changes.matched:
        keep(entry.key, old[known.key], heading=entry.heading, body=entry.body, url=entry.url or old[known.key].url)
    for entry in new or []:
        rows[entry.key] = dict(key=entry.key, heading=entry.heading, body=entry.body, body_hash=body_hash(entry.body),
                               url=entry.url, published_at=entry.published_at, first_seen=at, last_seen=at,
                               removed_at=None, pending_hash=None, pending_since=None)
    fired = {e.key for e in fire or []}
    for entry, known in changes.edited:
        if entry.key in fired:
            keep(entry.key, old[known.key], heading=entry.heading, body=entry.body, body_hash=body_hash(entry.body),
                 pending_hash=None, pending_since=None)
        elif pend and entry.key in pend:
            keep(entry.key, old[known.key], pending_hash=pend[entry.key][0], pending_since=pend[entry.key][1])
        else:
            keep(entry.key, old[known.key])
    used = {kn.key for _, kn in changes.matched} | {kn.key for _, kn in changes.edited}
    for key, e in old.items():  # stored entries not on today's page stay, marked removed
        if key not in rows and key not in used:
            keep(key, e, last_seen=e.last_seen, removed_at=e.removed_at or at)
    for e in old.values():
        s.delete(e)
    s.flush()
    for values in rows.values():
        s.add(Entry(source_id=row.id, **values))
    row.suspect_streak, row.last_ok_at = 0, at


async def _settle_confirmed(sf, fetcher, llm, p: _Pending, second: ReadResult, at, counts, *,
                            same_visit_edits: bool = False) -> None:
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

    # Edits wait for a second daily sighting of the same new body (spec 7.3: no false edits). A weekly login
    # visit confirms within the visit instead: both of its reads already showed the edit.
    fire, pend = ([e for e, _ in changes.edited] if same_visit_edits else []), {}
    for entry, known in [] if same_visit_edits else changes.edited:
        h = body_hash(entry.body)
        if known.pending_hash == h and known.pending_since and at - known.pending_since >= EDIT_HOLD:
            fire.append(entry)
        else:
            pend[entry.key] = (h, known.pending_since if known.pending_hash == h else at)
    to_send = Changes(new=changes.new, edited=[(e, k) for e, k in changes.edited if e in fire],
                      unchanged=changes.unchanged, removed=changes.removed)

    mode = _mode(src)
    events = await _build_events(sf, fetcher, src, mode, to_send)
    for ev in events:
        if ev.kind in ("new", "edited"):
            ev.summary = await summarize(llm, src.vendor_name, ev.heading, ev.diff or ev.body or "")

    with sf() as s:
        row = s.get(WatchedSource, src.id)
        _write_entries(s, row, changes, at, new=changes.new, fire=fire, pend=pend)
        s.add(Snapshot(source_id=row.id, markdown=second.markdown, hash=body_hash(second.markdown)))
        for ev in events:
            if ev.dedupe_key and s.scalar(select(func.count()).select_from(EntryEvent)
                                          .where(EntryEvent.dedupe_key == ev.dedupe_key)):
                continue
            s.add(ev)
            counts["events_queued" if ev.status == "queued" else "events_needs_look"] += 1
        sending = bool(changes.new or fire)
        _attempt(s, row, "changed" if sending else "same", second, None if sending else "edit held for a day")
        s.commit()
    counts["changed" if sending else "same"] += 1


def _version_of(*texts: str) -> str | None:
    for text in texts:
        if text and (version := heading_version(text)):
            return version.lstrip("vV")
    return None


async def _build_events(sf, fetcher, src, mode: str, changes: Changes) -> list[EntryEvent]:
    decided = gate(changes)
    new = [(entry, status, reason) for kind, entry, _, status, reason in decided if kind == "new"]
    if mode == "entries":
        return [EntryEvent(id=uuid.uuid4(), source_id=src.id, kind=kind, heading=entry.heading,
                           body=entry.body, diff=readable_diff(known.body, entry.body) if known else None,
                           url=entry.url or src.url, status=status, reason=reason)
                for kind, entry, known, status, reason in decided] + _titled_version(sf, src, new, mode)

    if mode == "entry":  # one page per rolling release: the child is the entry, sent verbatim
        events = []
        for entry, status, reason in new:
            bodies = []
            for page_url in (entry.url, *entry.parts):
                child = await fetcher.fetch(page_url)
                text = extract_markdown(child.text, page_url, (src.follow or {}).get("child_selector")) \
                    if child.outcome == "ok" else ""
                if not text:
                    bodies = []
                    break
                bodies.append(text)
            body = "\n\n".join(bodies)
            if not body:
                status, reason = "needs_look", "child_fetch_failed"
            events.append(EntryEvent(id=uuid.uuid4(), source_id=src.id, kind="new", heading=entry.heading,
                                     body=body or None, url=entry.url, status=status, reason=reason))
        return events + _titled_version(sf, src, new, mode)

    # Version lines: index pages of per-release documents, and version pages. Only the highest new version is
    # reported, and only if it is newer than every version the source already listed (an archived link added
    # to an index is not a release).
    with sf() as s:
        product = s.get(CatalogProduct, src.product_de_id) if src.product_de_id else None
        known = _load_state(s, src.id).known
    known_versions = [v for v in ((k.key if mode == "versions" else _version_of(k.body)) for k in known) if v]
    floor = max((version_tuple(v) for v in known_versions), default=())
    events, found = [], []
    for entry, status, reason in new:
        version = entry.key if mode == "versions" else _version_of(entry.heading, entry.url or "")
        if version is None:
            events.append(EntryEvent(id=uuid.uuid4(), source_id=src.id, kind="version", heading=entry.heading,
                                     url=entry.url, status="needs_look", reason="no_version"))
        elif version_tuple(version) > floor:
            found.append((version, entry, status, reason))
    if found:
        version, entry, status, reason = max(found, key=lambda f: version_tuple(f[0]))
        if not (product and product.de_version and not newer_than_tracked(version, product.de_version)):
            events.append(_version_event(src, product, version, entry.url or src.url, status, reason))
    return events


def _titled_version(sf, src, new: list, mode: str) -> list[EntryEvent]:
    """A new release note titled with a version newer than DocExtractor tracks ("v1.19.0" while it reads the
    1.18 docs) also gets a version line. Numbers from another scheme (a component's build) are ignored."""
    with sf() as s:
        product = s.get(CatalogProduct, src.product_de_id) if src.product_de_id else None
    tracked = product.de_version if product else None
    if not tracked:
        return []
    found = [(version, entry, status, reason) for entry, status, reason in new
             if (version := _version_of(entry.heading, entry.url if mode == "entry" else ""))
             and same_version_line(version, tracked) and newer_than_tracked(version, tracked)]
    if not found:
        return []
    version, entry, status, reason = max(found, key=lambda f: version_tuple(f[0]))
    return [_version_event(src, product, version, entry.url or src.url, status, reason)]


def _docextractor_moved(s, row, result: ReadResult) -> None:
    """A page DocExtractor extracts (one of its articles) now redirects elsewhere or is gone: DocExtractor
    needs the new URL. One line per new address; pages only the watch reads are its own business."""
    if row.de_article_id is None:
        return
    moved = result.outcome == "ok" and result.final_url and normalize_url(result.final_url) != normalize_url(row.url)
    if result.outcome != "gone" and not moved:
        return
    target = normalize_url(result.final_url) if moved else "gone"
    dedupe = f"docextractor:{row.id}:{target}"
    if s.scalar(select(func.count()).select_from(EntryEvent).where(EntryEvent.dedupe_key == dedupe)):
        return
    source = s.get(CatalogSource, row.de_source_id) if row.de_source_id else None
    page = f"{source.name if source else row.title or row.vendor_name}: the release-notes page [{row.url}]({row.url})"
    status = f" (HTTP {result.http_status})" if result.http_status else ""
    heading = f"{page} now redirects to [{result.final_url}]({result.final_url})" if moved else f"{page} is gone{status}"
    # Redirects inside a signed-in session are often harmless (return URLs, slug redirects): a person checks those.
    queue = "needs_look" if row.login_walled else "queued"
    s.add(EntryEvent(id=uuid.uuid4(), source_id=row.id, kind="moved", product_de_id=row.product_de_id,
                     heading=heading, url=result.final_url if moved else row.url, dedupe_key=dedupe, status=queue,
                     reason="docextractor_moved" if moved else "docextractor_gone"))


def _version_event(src, product, version: str, url: str, status: str = "queued", reason: str | None = None):
    name = product.name if product else (src.title or src.url)
    scope = src.product_de_id or src.id
    return EntryEvent(
        id=uuid.uuid4(), source_id=src.id, kind="version", version=version, product_de_id=src.product_de_id,
        heading=f"{src.vendor_name} released a new version of {name}: {version}", url=url,
        dedupe_key=f"version:{src.vendor_name}:{scope}:{version}", status=status, reason=reason)


def _baseline_versions(s, row, entries) -> None:
    """A version list read for the first time may already show a release DocExtractor doesn't have yet."""
    product = s.get(CatalogProduct, row.product_de_id) if row.product_de_id else None
    versions = [e.key for e in entries if version_tuple(e.key)]
    if _mode(row) != "versions" or not (product and product.de_version and versions):
        return
    top = max(versions, key=version_tuple)
    if newer_than_tracked(top, product.de_version):
        ev = _version_event(row, product, top, row.url)
        if not s.scalar(select(func.count()).select_from(EntryEvent).where(EntryEvent.dedupe_key == ev.dedupe_key)):
            s.add(ev)
