"""Adaptive Cards for the Teams chat. Formatting is simplified; the words stay verbatim."""

import re
from datetime import datetime

PART_LIMIT = 20_000
MAX_PARTS = 3
LIST_CAP = 15
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def card_markdown(md: str) -> str:
    out = []
    for line in md.splitlines():
        if _TABLE_SEP.match(line):
            continue
        if m := _HEADING.match(line):
            line = f"**{m.group(1)}**"
        elif line.strip().startswith("|"):
            line = " | ".join(c.strip() for c in line.strip().strip("|").split("|"))
        out.append(_IMAGE.sub(lambda m: f"[{m.group(1) or 'image'}]({m.group(2)})", line))
    return "\n".join(out).strip()


def split_parts(text: str, limit: int = PART_LIMIT) -> list[str]:
    parts, current = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:  # only a single oversized paragraph is cut mid-text
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:limit])
            para = para[limit:]
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit:
            parts.append(current)
            current = para
        else:
            current = candidate
    if current or not parts:
        parts.append(current)
    return parts


def _text(text: str, **kw) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, **kw}


def _card(blocks: list[dict], url: str | None = None) -> dict:
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "msteams": {"width": "Full"}, "body": blocks}
    if url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open page", "url": url}]
    return card


def entry_cards(event, vendor: str, product: str | None, web_base_url: str) -> list[dict]:
    edited = event.kind == "edited"
    title = f"{'Edited · ' if edited else ''}{vendor}{f' · {product}' if product else ''}: {event.heading}"
    text = event.diff or "" if edited else card_markdown(event.body or "")
    parts = split_parts(text)
    summary = [_text(f"_{event.summary}_", isSubtle=True)] if event.summary else []

    def body_block(part):
        return _text(part, fontType="Monospace") if edited else _text(part)

    if len(parts) > MAX_PARTS:
        return [_card([_text(f"**{title} (1 of {len(parts)} parts)**"), *summary, body_block(parts[0]),
                       _text(f"Full entry: {web_base_url}/events/{event.id}")], event.url)]
    cards = []
    for i, part in enumerate(parts, 1):
        suffix = f" ({i}/{len(parts)})" if len(parts) > 1 else ""
        cards.append(_card([_text(f"**{title}{suffix}**"), *(summary if i == 1 else []), body_block(part)],
                           event.url))
    return cards


def _capped(items: list[str]) -> list[str]:
    extra = len(items) - LIST_CAP
    return items[:LIST_CAP] + ([f"…and {extra} more"] if extra > 0 else [])


def summary_card(health, versions: list[tuple[str, list[str]]], counts: dict[str, dict[str, int]],
                 now: datetime, updates: list[tuple[str, list[str]]] = ()) -> dict:
    """versions and updates: (vendor · product, its lines) groups of versions newer than DocExtractor tracks
    and of DocExtractor pages that moved."""
    blocks = [_text(f"**Release Watch · {now:%a %d %b %Y}**", size="Medium")]
    for title, groups in (("New versions", versions), ("Update in DocExtractor", updates)):
        if groups:
            blocks.append(_text(f"**{title}**"))
            blocks += [_text(f"**{name}**\n\n" + "\n".join(f"- {line}" for line in lines)) for name, lines in groups]
    if counts:
        lines = [f"- {vendor}: " + ", ".join(f"{n} {kind}" for kind, n in c.items() if n)
                 for vendor, c in sorted(counts.items())]
        blocks.append(_text("**Release notes**\n\n" + "\n".join(lines)))
    if not versions and not updates and not counts:
        blocks.append(_text(f"No changes; {health.checked} of {health.total} sources checked"))

    h = [f"Checked {health.checked} of {health.total} sources"]
    for label, items in (("Failed today", health.failed), ("Suspect today", health.suspect),
                         ("Broken (3+ suspect days)", health.broken), ("Gone", health.gone_new)):
        if items:
            h.append(f"{label}:\n" + "\n".join(f"- {i}" for i in _capped(items)))
    if health.waiting_login:
        h.append("Waiting for login (phase 3): " + ", ".join(f"{v} {n}" for v, n in health.waiting_login.items()))
    h.append("Public sources only: " + ", ".join(health.public_only))
    if health.needs_look:
        h.append(f"{health.needs_look} in needs a look ({health.needs_look_stale} older than 3 days)")
    if health.sync_error:
        h.append(f"DocExtractor sync failed: {health.sync_error}")
    if health.stale_jobs:
        h.append("Jobs that never finished: " + ", ".join(health.stale_jobs))
    h.append("Last successful watch: " + (f"{health.last_watch_ok:%a %d %b %H:%M} UTC" if health.last_watch_ok
                                         else "never"))
    blocks.append(_text("**Health**\n\n" + "\n\n".join(h)))
    return _card(blocks)


def envelope(card: dict) -> dict:
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def text_card(text: str) -> dict:
    return _card([_text(text)])
