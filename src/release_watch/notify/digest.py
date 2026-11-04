"""The daily digest (spec 9): a summary card with health, then one card per new and edited entry.

With no webhook set, the digest is kept for the web UI's Notifications page instead of posted.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from release_watch.health import compute_health
from release_watch.models import CatalogProduct, Digest, EntryEvent, WatchedSource
from release_watch.notify.cards import entry_cards, envelope, summary_card, text_card

STALE_WATCH = timedelta(hours=26)


def _plan(s, *, web_base_url: str, now: datetime) -> list[tuple[list, list[dict]]]:
    """(events marked sent after these cards, cards), in posting order."""
    health = compute_health(s, now)
    queued = list(s.scalars(select(EntryEvent).where(EntryEvent.status == "queued").order_by(EntryEvent.created_at)))
    sources = {w.id: w for w in s.scalars(select(WatchedSource).where(
        WatchedSource.id.in_({e.source_id for e in queued})))}
    products = {p.de_id: p for p in s.scalars(select(CatalogProduct))}

    lines, line_events, entry_events, counts = [], [], [], {}
    for ev in queued:
        src = sources.get(ev.source_id)
        vendor = src.vendor_name if src else "?"
        if ev.kind in ("version", "moved"):
            product = products.get(ev.product_de_id)
            tracked = f" (tracked: {product.de_version})" if ev.kind == "version" and product and product.de_version else ""
            lines.append(f"[{ev.heading}]({ev.url}){tracked}" if ev.url else ev.heading + tracked)
            line_events.append(ev)
        else:
            counts.setdefault(vendor, {"new": 0, "edited": 0})[ev.kind] += 1
            entry_events.append(ev)

    plan: list[tuple[list, list[dict]]] = []
    if health.last_watch_ok is None or now - health.last_watch_ok > STALE_WATCH:
        age = "never" if health.last_watch_ok is None else \
            f"{int((now - health.last_watch_ok).total_seconds() // 3600)} h ago"
        plan.append(([], [text_card(f"Release Watch: last successful watch was {age}. Check the watch job.")]))
    plan.append((line_events, [summary_card(health, lines, counts, now)]))
    for ev in sorted(entry_events, key=lambda e: (e.kind != "new", e.created_at)):
        src = sources.get(ev.source_id)
        product = products.get(src.product_de_id) if src and src.product_de_id else None
        plan.append(([ev], entry_cards(ev, src.vendor_name if src else "?", product.name if product else None,
                                       web_base_url)))
    return plan


def preview_digest(session_factory, *, web_base_url: str, now: datetime) -> list[dict]:
    """The cards the next digest would carry, if it ran now. Marks nothing sent."""
    with session_factory() as s:
        return [card for _, cards in _plan(s, web_base_url=web_base_url, now=now) for card in cards]


async def run_digest(session_factory, teams, *, web_base_url: str, now: datetime, dry_run: bool = False,
                     out=print) -> dict:
    with session_factory() as s:
        plan = _plan(s, web_base_url=web_base_url, now=now)

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
        s.add(Digest(created_at=now, channel=channel, cards=delivered))
        s.commit()
    return {"cards": len(delivered), "events_sent": sent_events, "channel": channel}
