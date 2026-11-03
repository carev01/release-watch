"""Version-pinned release-notes URLs: propose and verify the successor when a newer version appears (spec 7.1).

The pinned source is never re-pointed silently. A verified successor becomes a new source and the owner
gets one line; an unverified one goes to "needs a look". The old source stays watched until the
successor has a baseline (run.py marks it superseded then).
"""

import re
import uuid

from sqlalchemy import func, select

from release_watch.models import CatalogProduct, Entry, EntryEvent, Snapshot, WatchedSource
from release_watch.urls import normalize_url
from release_watch.watch.extract import extract_markdown
from release_watch.watch.rules import version_tuple

MIN_TEXT_CHARS = 200
SIZE_RATIO = (0.3, 3.0)  # a successor's text is within this ratio of the old page's last snapshot
MIN_SIZE_FOR_RATIO = 500
NOT_FOUND = re.compile(r"page not found|not found|does not exist|no longer available|\b404\b", re.I)


def _fill(template: str, version: str) -> str:
    major = version.split(".")[0]
    return (template.replace("{version_compact}", re.sub(r"\D", "", version))
            .replace("{version_major}", major).replace("{version}", version))


def _accepts(result, old_url: str, selector: str | None, template: str, version: str,
             old_size: int | None) -> bool:
    if result.outcome != "ok":  # soft-404 markers and login redirects are already outcomes in the fetcher
        return False
    if normalize_url(result.final_url) != normalize_url(result.url):  # redirected to a landing page
        return False
    is_pdf = "pdf" in result.content_type.lower() or result.text.startswith("%PDF")
    if is_pdf:
        return old_url.lower().endswith(".pdf")
    text = extract_markdown(result.text, result.final_url, selector)
    if NOT_FOUND.search(text[:1000]):
        return False
    if old_size and old_size >= MIN_SIZE_FOR_RATIO and not SIZE_RATIO[0] * old_size <= len(text) <= SIZE_RATIO[1] * old_size:
        return False
    needle = version.split(".")[0] if "{version}" not in template and "{version_compact}" not in template else version
    return len(text) >= MIN_TEXT_CHARS and needle in text


def moved_heading(s, old: WatchedSource, version: str) -> str:
    product = s.get(CatalogProduct, old.product_de_id) if old.product_de_id else None
    name = f"{old.vendor_name} {product.name}" if product else old.vendor_name
    if old.title:
        name = f"{name} ({old.title})"
    return f"Release-notes URL for {name} moved to {version}"


def on_successor_baseline(s, new: WatchedSource) -> None:
    """The successor is being watched now: retire the pinned source and send the one-line notice."""
    version = ((new.follow or {}).get("pin") or {}).get("version")
    for old in s.scalars(select(WatchedSource).where(WatchedSource.superseded_by == new.id,
                                                     WatchedSource.state == "active")):
        old.state = "superseded"
        dedupe = f"moved:{old.id}:{version}"
        if version and not s.scalar(select(func.count()).select_from(EntryEvent).where(EntryEvent.dedupe_key == dedupe)):
            s.add(EntryEvent(source_id=new.id, kind="moved", version=version, product_de_id=old.product_de_id,
                             heading=moved_heading(s, old, version), url=new.url, dedupe_key=dedupe,
                             status="queued"))


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
            old_size = s.scalar(select(func.length(Snapshot.markdown)).where(Snapshot.source_id == old.id)
                                .order_by(Snapshot.fetched_at.desc()).limit(1))
            if _accepts(result, old.url, old.selector, pin["successor"], version, old_size) and not s.scalar(
                    select(func.count()).select_from(WatchedSource).where(WatchedSource.url_key == normalize_url(url))):
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
                counts["proposed"] += 1
            else:
                s.add(EntryEvent(source_id=old.id, kind="moved", version=version, product_de_id=old.product_de_id,
                                 heading=moved_heading(s, old, version).replace(" moved to ", " may have moved to "),
                                 url=url, dedupe_key=dedupe, status="needs_look", reason="successor_unverified"))
                counts["unverified"] += 1
            s.commit()
    return counts
