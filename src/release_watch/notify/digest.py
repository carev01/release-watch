"""The daily digest (spec 9): a summary card with health, then one card per new and edited entry.

With no webhook set, the digest is kept for the web UI's Notifications page instead of posted.
"""

import json
import re
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select

from release_watch.health import compute_health
from release_watch.models import CatalogProduct, Digest, Entry, EntryEvent, WatchedSource
from release_watch.notify.cards import entry_cards, envelope, summary_card, text_card
from release_watch.watch.rules import version_tuple

STALE_WATCH = timedelta(hours=26)


def _named_product(vendor: str, product: str | None, bodies: list[str]) -> str | None:
    """The product name the page text uses most, e.g. "IBM Storage Defender Copy Data Management".

    Only names containing the tracked product count, so "IBM FlashSystem" on a Storage Protect page doesn't.
    """
    found = re.compile(rf"\b{re.escape(vendor)} ((?:(?:[A-Z][\w-]*|for|and|&) )*[A-Z][\w-]*)")
    names = Counter(m.group(1) for body in bodies for m in found.finditer(body[:2000].replace("®", ""))
                    if product is None or product.casefold() in m.group(1).casefold())
    return f"{vendor} {names.most_common(1)[0][0]}" if names else None


def _page_title(s, src: WatchedSource | None, products: dict) -> str | None:
    """The page's own top heading (e.g. "What's new for IBM Storage Protect for Mail"), else the source title.

    A heading that doesn't name the vendor ("What's new") gets the product the page text names in front.
    """
    if src is None:
        return None
    bodies = list(s.scalars(select(Entry.body).where(Entry.source_id == src.id, Entry.removed_at.is_(None))))
    title = next((b.lstrip().split("\n", 1)[0][2:].strip() for b in bodies if b.lstrip().startswith("# ")),
                 src.title)
    if not title or src.vendor_name.casefold() in title.casefold():
        return title
    tracked = products.get(src.product_de_id)
    named = _named_product(src.vendor_name, tracked.name if tracked else None, bodies)
    return f"{named}: {title}" if named else title


def _link(text: str, url: str | None) -> str:
    return f"[{text}]({url})" if url else text


def _behind(product, version: str | None) -> bool:
    """DocExtractor still tracks an older version than this one (or none at all)."""
    return bool(version) and not (product and product.de_version
                                  and version_tuple(version) <= version_tuple(product.de_version))


def _version_lines(s, events: list, sources: dict, products: dict) -> list[str]:
    """One line per new version, linking every page that shows it."""
    by_version: dict[str, dict[str, str]] = {}
    for ev in events:
        title = _page_title(s, sources.get(ev.source_id), products) or ev.heading
        by_version.setdefault(ev.version, {}).setdefault(ev.url, title)
    lines = []
    for version in sorted(by_version, key=version_tuple, reverse=True):
        product = products.get(events[0].product_de_id)
        tracked = f" (DocExtractor tracks {product.de_version})" if product and product.de_version else ""
        pages = ", ".join(_link(title, url) for url, title in by_version[version].items())
        lines.append(f"Version **{version}** is available{tracked}" + (f": {pages}" if pages else ""))
    return lines


def _grouped(events: list, sources: dict, products: dict) -> dict[str, list]:
    groups: dict[str, list] = {}
    for ev in events:
        src = sources.get(ev.source_id)
        product = products.get(ev.product_de_id or (src.product_de_id if src else None))
        vendor = src.vendor_name if src else "?"
        groups.setdefault(f"{vendor} · {product.name}" if product else vendor, []).append(ev)
    return dict(sorted(groups.items()))


def _plan(s, *, web_base_url: str, now: datetime) -> tuple[list[tuple[list, list[dict]]], list]:
    """((events marked sent after these cards, cards) in posting order, events with nothing to tell the owner).

    The digest says what DocExtractor needs: a version newer than it tracks, or one of its pages that moved.
    The watch following a page to its next-version URL is its own housekeeping, so those lines stay out.
    """
    health = compute_health(s, now)
    queued = list(s.scalars(select(EntryEvent).where(EntryEvent.status == "queued").order_by(EntryEvent.created_at)))
    sources = {w.id: w for w in s.scalars(select(WatchedSource).where(
        WatchedSource.id.in_({e.source_id for e in queued})))}
    products = {p.de_id: p for p in s.scalars(select(CatalogProduct))}

    versions, updates, silent, entry_events, counts = [], [], [], [], {}
    for ev in queued:
        src = sources.get(ev.source_id)
        if ev.reason in ("docextractor_moved", "docextractor_gone"):
            updates.append(ev)
        elif ev.kind in ("version", "moved"):
            product = products.get(ev.product_de_id or (src.product_de_id if src else None))
            (versions if _behind(product, ev.version) else silent).append(ev)
        else:
            counts.setdefault(src.vendor_name if src else "?", {"new": 0, "edited": 0})[ev.kind] += 1
            entry_events.append(ev)
    version_groups = [(name, _version_lines(s, evs, sources, products))
                      for name, evs in _grouped(versions, sources, products).items()]
    update_groups = [(name, [ev.heading for ev in evs]) for name, evs in _grouped(updates, sources, products).items()]

    plan: list[tuple[list, list[dict]]] = []
    if health.last_watch_ok is None or now - health.last_watch_ok > STALE_WATCH:
        age = "never" if health.last_watch_ok is None else \
            f"{int((now - health.last_watch_ok).total_seconds() // 3600)} h ago"
        plan.append(([], [text_card(f"Release Watch: last successful watch was {age}. Check the watch job.")]))
    plan.append((versions + updates, [summary_card(health, version_groups, counts, now, updates=update_groups)]))
    for ev in sorted(entry_events, key=lambda e: (e.kind != "new", e.created_at)):
        src = sources.get(ev.source_id)
        product = products.get(src.product_de_id) if src and src.product_de_id else None
        plan.append(([ev], entry_cards(ev, src.vendor_name if src else "?", product.name if product else None,
                                       web_base_url)))
    return plan, silent


def preview_digest(session_factory, *, web_base_url: str, now: datetime) -> list[dict]:
    """The cards the next digest would carry, if it ran now. Marks nothing sent."""
    with session_factory() as s:
        plan, _ = _plan(s, web_base_url=web_base_url, now=now)
        return [card for _, cards in plan for card in cards]


async def run_digest(session_factory, teams, *, web_base_url: str, now: datetime, dry_run: bool = False,
                     out=print) -> dict:
    with session_factory() as s:
        plan, silent = _plan(s, web_base_url=web_base_url, now=now)

    if dry_run:
        for _, cards in plan:
            for card in cards:
                out(json.dumps(envelope(card), ensure_ascii=False))
        return {"cards": sum(len(c) for _, c in plan), "events_sent": 0}

    channel = "teams" if teams is not None else "web"
    delivered: list[dict] = []
    sent_events = 0
    for events, cards in plan:
        for card in cards:
            if teams is not None:
                await teams.post(card)
            delivered.append(card)
        with session_factory() as s:
            for ev in events:
                row = s.get(EntryEvent, ev.id)
                row.status, row.sent_at, row.cards_sent = "sent", now, len(cards)
                sent_events += 1
            s.commit()
    with session_factory() as s:
        for ev in silent:
            s.get(EntryEvent, ev.id).status = "dismissed"
        s.add(Digest(created_at=now, channel=channel, cards=delivered))
        s.commit()
    return {"cards": len(delivered), "events_sent": sent_events, "channel": channel}
