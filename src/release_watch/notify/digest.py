"""The daily digest (spec 9): a summary card with health, then one card per new and edited entry."""

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from release_watch.health import compute_health
from release_watch.models import CatalogProduct, EntryEvent, WatchedSource
from release_watch.notify.cards import entry_cards, envelope, summary_card, text_card
from release_watch.notify.teams import TeamsError

STALE_WATCH = timedelta(hours=26)


async def run_digest(session_factory, teams, *, web_base_url: str, now: datetime, dry_run: bool = False,
                     out=print) -> dict:
    if teams is None and not dry_run:
        raise TeamsError("RW_TEAMS_WEBHOOK_URL is not set; run with --dry-run to preview")
    with session_factory() as s:
        health = compute_health(s, now)
        queued = list(s.scalars(select(EntryEvent).where(EntryEvent.status == "queued")
                                .order_by(EntryEvent.created_at)))
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

    plan: list[tuple[list, list[dict]]] = []  # (events marked sent after these cards, cards)
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

    if dry_run:
        for _, cards in plan:
            for card in cards:
                out(json.dumps(envelope(card), ensure_ascii=False))
        return {"cards": sum(len(c) for _, c in plan), "events_sent": 0}

    sent_cards = sent_events = 0
    for events, cards in plan:
        for card in cards:
            await teams.post(card)
            sent_cards += 1
        with session_factory() as s:
            for ev in events:
                row = s.get(EntryEvent, ev.id)
                row.status, row.sent_at, row.cards_sent = "sent", now, len(cards)
                sent_events += 1
            s.commit()
    return {"cards": sent_cards, "events_sent": sent_events}
