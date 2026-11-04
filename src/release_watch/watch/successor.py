"""Version-pinned release-notes URLs: propose and verify the successor when a newer version appears (spec 7.1).

The pinned source is never re-pointed silently. A verified successor becomes a new source and the owner
gets one line; an unverified one goes to "needs a look". The old source stays watched until the
successor has a baseline (run.py marks it superseded then).
"""

import re
import uuid

from sqlalchemy import func, select

from release_watch.models import CatalogProduct, Entry, EntryEvent, WatchedSource
from release_watch.urls import normalize_url
from release_watch.watch.extract import extract_markdown
from release_watch.watch.rules import version_tuple

MIN_TEXT_CHARS = 200
PREVIEW_CHARS = 20_000
NOT_FOUND = re.compile(r"page not found|not found|does not exist|no longer available|\b404\b", re.I)


def _fill(template: str, version: str) -> str:
    major = version.split(".")[0]
    return (template.replace("{version_compact}", re.sub(r"\D", "", version))
            .replace("{version_major}", major).replace("{version}", version))


def check_successor(result, old_url: str, selector: str | None, template: str, version: str) -> tuple[str | None, str]:
    """(None if the page passes, else the reason code; what the page shows, for the owner to judge).

    No size check: a new version's what's-new page lists only its own changes, so it is often much
    shorter than the cumulative page it replaces.
    """
    if result.outcome != "ok":  # soft-404 markers and login redirects are already outcomes in the fetcher
        return "successor_fetch_failed", f"The page answered HTTP {result.status} ({result.reason or result.outcome})."
    is_pdf = "pdf" in result.content_type.lower() or result.text.startswith("%PDF")
    text = "(a PDF)" if is_pdf else extract_markdown(result.text, result.final_url, selector)
    if normalize_url(result.final_url) != normalize_url(result.url):  # redirected to a landing page
        return "successor_redirected", f"Redirected to {result.final_url}\n\n{text[:PREVIEW_CHARS]}"
    if is_pdf:
        return (None if old_url.lower().endswith(".pdf") else "successor_no_version"), text
    preview = text[:PREVIEW_CHARS]
    if NOT_FOUND.search(text[:1000]):
        return "successor_not_found", preview
    if len(text) < MIN_TEXT_CHARS:
        return "successor_too_short", preview
    needle = version.split(".")[0] if "{version}" not in template and "{version_compact}" not in template else version
    return (None if needle in text else "successor_no_version"), preview


def source_name(s, src: WatchedSource) -> str:
    product = s.get(CatalogProduct, src.product_de_id) if src.product_de_id else None
    name = f"{src.vendor_name} {product.name}" if product else src.vendor_name
    return f"{name} ({src.title})" if src.title else name


def moved_heading(s, old: WatchedSource, version: str) -> str:
    return f"Release-notes URL for {source_name(s, old)} moved to {version}"


def on_successor_baseline(s, new: WatchedSource, version: str | None = None) -> None:
    """The successor is being watched now: retire the pinned source and send the one-line notice."""
    version = version or ((new.follow or {}).get("pin") or {}).get("version")
    for old in s.scalars(select(WatchedSource).where(WatchedSource.superseded_by == new.id,
                                                     WatchedSource.state == "active")):
        old.state = "superseded"
        dedupe = f"moved:{old.id}:{version}"
        if version and not s.scalar(select(func.count()).select_from(EntryEvent).where(EntryEvent.dedupe_key == dedupe)):
            s.add(EntryEvent(source_id=new.id, kind="moved", version=version, product_de_id=old.product_de_id,
                             heading=moved_heading(s, old, version), url=new.url, dedupe_key=dedupe,
                             status="queued"))


def _add_successor(s, old: WatchedSource, url: str, version: str, now) -> WatchedSource:
    pin = old.follow["pin"]
    new = WatchedSource(
        id=uuid.uuid4(), vendor_name=old.vendor_name, product_de_id=old.product_de_id, url=url,
        url_key=normalize_url(url), kind=old.kind, state="active", origin="successor",
        title=(old.title or "").replace(pin["version"], version) or None, selector=old.selector,
        split_rule=old.split_rule, follow={**old.follow, "pin": {**pin, "version": version}},
        notes=f"Successor of {old.url}", confirmed_by="successor", decided_at=now,
        label=old.label, label_reason="successor")
    s.add(new)
    s.flush()
    # The moved line is sent once the successor has a baseline (run.py), not on the probe alone.
    old.superseded_by = new.id
    return new


def watch_successor(s, event: EntryEvent, url: str, now) -> WatchedSource:
    """The owner picked the new page for an unverified "may have moved" item: watch it from now on.

    The held item steps aside (its dedupe key freed) so the successor's baseline sends the moved line.
    """
    old = s.get(WatchedSource, event.source_id)
    key = normalize_url(url)
    new = s.scalar(select(WatchedSource).where(WatchedSource.url_key == key))
    if new is None:
        new = _add_successor(s, old, url, event.version, now)
    else:
        new.state = "active"
        old.superseded_by = new.id
    event.status, event.decided_at, event.dedupe_key = "dismissed", now, None
    s.flush()
    return new


async def propose_successors(session_factory, fetcher, now) -> dict[str, int]:
    counts = {"proposed": 0, "unverified": 0}
    with session_factory() as s:
        pinned = [w for w in s.scalars(select(WatchedSource).where(WatchedSource.state == "active"))
                  if (w.follow or {}).get("pin")]
        work = []
        for old in pinned:
            pin = old.follow["pin"]
            signal = s.scalar(select(WatchedSource).where(WatchedSource.url_key == normalize_url(pin["signal"]),
                                                          WatchedSource.state == "active"))
            if signal is None or old.superseded_by:
                continue
            keys = list(s.scalars(select(Entry.key).where(Entry.source_id == signal.id, Entry.removed_at.is_(None))))
            versioned = [k for k in keys if version_tuple(k)]
            if not versioned:
                continue
            version = max(versioned, key=version_tuple)
            if version_tuple(version) <= version_tuple(pin["version"]):
                continue
            dedupe = f"moved:{old.id}:{version}"
            if s.scalar(select(func.count()).select_from(EntryEvent).where(EntryEvent.dedupe_key == dedupe)):
                continue
            work.append((old.id, version, _fill(pin["successor"], version), dedupe))

    for old_id, version, url, dedupe in work:
        with session_factory() as s:
            old = s.get(WatchedSource, old_id)
            pin = old.follow["pin"]
            result = await fetcher.fetch(url)
            why, shown = check_successor(result, old.url, old.selector, pin["successor"], version)
            taken = s.scalar(select(WatchedSource).where(WatchedSource.url_key == normalize_url(url)))
            if why is None and taken is not None and taken.state in ("active", "candidate"):
                taken.state, old.superseded_by = "active", taken.id  # already known, e.g. via DocExtractor
                s.flush()
                if s.scalar(select(func.count()).select_from(Entry).where(Entry.source_id == taken.id)):
                    on_successor_baseline(s, taken, version)  # it has its baseline already
                counts["proposed"] += 1
            elif why is None and taken is None:
                _add_successor(s, old, url, version, now)
                counts["proposed"] += 1
            else:
                s.add(EntryEvent(source_id=old.id, kind="moved", version=version, product_de_id=old.product_de_id,
                                 heading=moved_heading(s, old, version).replace(" moved to ", " may have moved to "),
                                 body=shown or None, url=url, dedupe_key=dedupe, status="needs_look",
                                 reason=why or "successor_url_taken"))
                counts["unverified"] += 1
            s.commit()
    return counts
