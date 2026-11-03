import json
import re
import uuid
from datetime import UTC, datetime

from release_watch.health import Health
from release_watch.models import EntryEvent
from release_watch.notify.cards import card_markdown, entry_cards, envelope, split_parts, summary_card

NOW = datetime(2026, 11, 4, 8, tzinfo=UTC)
WEB = "http://rw.lan"


def words(text):
    return re.findall(r"[A-Za-z0-9]+", text)


def card_text(card):
    return "\n".join(b.get("text", "") for b in card["body"])


def test_card_markdown_keeps_words():
    md = "## October 28, 2026\n\n| Feature | Status |\n|---|---|\n| Blob immutability | GA |\n\n* Fixed ![logo](https://x/i.png) restore"
    out = card_markdown(md)
    assert "**October 28, 2026**" in out and "##" not in out
    assert "Feature | Status" in out and "---" not in out
    assert words(md) == words(out)


def test_split_parts_on_paragraphs():
    text = "\n\n".join(["a" * 9000] * 5)
    parts = split_parts(text, 20_000)
    assert len(parts) == 3 and all(len(p) <= 20_000 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def event(body, kind="new", diff=None):
    return EntryEvent(id=uuid.uuid4(), source_id=uuid.uuid4(), kind=kind, heading="October 2026", body=body,
                      diff=diff, summary="Adds X.", url="https://v.com/rn", status="queued")


def test_entry_cards_numbered_parts():
    ev = event("\n\n".join(["word " * 1800] * 5))  # ~45 KB
    cards = entry_cards(ev, "Vendor", "Product", WEB)
    assert len(cards) == 3
    assert [c["body"][0]["text"] for c in cards] == [f"**Vendor · Product: October 2026 ({i}/3)**" for i in (1, 2, 3)]
    assert "Adds X." in card_text(cards[0]) and "Adds X." not in card_text(cards[1])
    assert cards[0]["actions"][0]["url"] == "https://v.com/rn"


def test_entry_cards_over_three_parts_link_to_web():
    ev = event("\n\n".join(["word " * 1800] * 8))  # ~72 KB
    cards = entry_cards(ev, "Vendor", None, WEB)
    assert len(cards) == 1
    assert f"Full entry: {WEB}/events/{ev.id}" in card_text(cards[0])
    assert cards[0]["body"][0]["text"] == "**Vendor: October 2026 (1 of 4 parts)**"


def test_edited_card_shows_diff_monospace():
    ev = event("body", kind="edited", diff="-* old\n+* new")
    [card] = entry_cards(ev, "Vendor", None, WEB)
    mono = [b for b in card["body"] if b.get("fontType") == "Monospace"]
    assert mono and "+* new" in mono[0]["text"]
    assert card["body"][0]["text"].startswith("**Edited · Vendor")


def test_summary_card_empty_day():
    card = summary_card(Health(checked=742, total=748), [], {}, NOW)
    assert "No changes; 742 of 748 sources checked" in card_text(card)


def test_summary_card_lists_versions_counts_and_health():
    h = Health(checked=10, total=12, failed=["V: a (http_500)"], broken=["V: b"], waiting_login={"AvePoint": 26},
               needs_look=2, needs_look_stale=1, sync_error="boom", last_watch_ok=NOW)
    card = summary_card(h, ["[Acme released a new version of Box: 2.0](https://a)"], {"Acme": {"new": 2, "edited": 1}}, NOW)
    text = card_text(card)
    for needle in ("Acme released a new version of Box: 2.0", "Acme: 2 new, 1 edited", "Checked 10 of 12 sources",
                   "V: a (http_500)", "V: b", "AvePoint 26", "Rubrik, Cohesity", "2 in needs a look (1 older than 3 days)",
                   "boom"):
        assert needle in text, needle


def test_envelope_shape():
    env = envelope({"type": "AdaptiveCard"})
    assert env["type"] == "message"
    assert env["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert env["attachments"][0]["content"] == {"type": "AdaptiveCard"}
    json.dumps(env)
