import uuid

from release_watch.models import EntryEvent
from release_watch.notify.cards import entry_cards
from release_watch.web.cards import card_html


def test_folded_notes_render_behind_a_show_full_notes_toggle():
    body = "\n\n".join(f"* Fix number {i}: " + "detail " * 30 for i in range(30))
    ev = EntryEvent(id=uuid.uuid4(), source_id=uuid.uuid4(), kind="new", heading="7.4.2", body=body,
                    url="https://v.com/rn", status="queued")
    [card] = entry_cards(ev, "Vendor", None, "http://rw.lan")
    html = str(card_html(card))
    assert "Fix number 0" in html and "<details><summary>Show full notes</summary>" in html
    assert html.index("<details>") < html.index("Fix number 29") < html.index("</details>")
    assert 'href="https://v.com/rn"' in html
