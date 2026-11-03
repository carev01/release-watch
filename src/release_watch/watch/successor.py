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


def _fill(template: str, version: str) -> str:
    major = version.split(".")[0]
    return (template.replace("{version_compact}", re.sub(r"\D", "", version))
            .replace("{version_major}", major).replace("{version}", version))


def _accepts(result, old_url: str, selector: str | None, template: str, version: str) -> bool:
    if result.outcome != "ok":
        return False
    is_pdf = "pdf" in result.content_type.lower() or result.text.startswith("%PDF")
    if is_pdf:
        return old_url.lower().endswith(".pdf")
    text = extract_markdown(result.text, result.final_url, selector)
    needle = version.split(".")[0] if "{version}" not in template and "{version_compact}" not in template else version
    return len(text) >= MIN_TEXT_CHARS and needle in text


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
            product = s.get(CatalogProduct, old.product_de_id) if old.product_de_id else None
            name = f"{old.vendor_name} {product.name}" if product else old.vendor_name
            if old.title:
                name = f"{name} ({old.title})"
            if _accepts(result, old.url, old.selector, pin["successor"], version) and not s.scalar(
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
                old.superseded_by = new.id
                s.add(EntryEvent(source_id=new.id, kind="moved", version=version, product_de_id=old.product_de_id,
                                 heading=f"Release-notes URL for {name} moved to {version}", url=url,
                                 dedupe_key=dedupe, status="queued"))
                counts["proposed"] += 1
            else:
                s.add(EntryEvent(source_id=old.id, kind="moved", version=version, product_de_id=old.product_de_id,
                                 heading=f"Release-notes URL for {name} may have moved to {version}", url=url,
                                 dedupe_key=dedupe, status="needs_look", reason="successor_unverified"))
                counts["unverified"] += 1
            s.commit()
    return counts
