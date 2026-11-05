import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from release_watch.models import NewsSource, WatchedSource
from release_watch.web.app import create_app


def watched(vendor, title, state="candidate", label="release_notes"):
    url = f"https://{vendor.lower()}.com/{title.lower().replace(' ', '-')}"
    return WatchedSource(vendor_name=vendor, url=url, url_key=url, title=title, kind="entry_page",
                         state=state, origin="heuristic", label=label, label_reason="llm",
                         label_evidence="October 28, 2026")


@pytest.fixture
def client(session_factory):
    with session_factory() as s:
        s.add_all([
            watched("Zerto", "Zerto Release Notes"),
            watched("Zerto", "What's New in 10.8", label="unsure"),
            watched("Commvault", "Commvault Changelog"),
            watched("Commvault", "Already Active", state="active"),
            NewsSource(vendor_name="Commvault", url="https://www.commvault.com/news",
                       url_key="https://www.commvault.com/news", kind="listing", state="candidate", origin="seed"),
        ])
        s.commit()
    return TestClient(create_app(session_factory))


def ids(session_factory, model=WatchedSource, **where):
    with session_factory() as s:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return [str(r.id) for r in s.scalars(q)]


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


def test_queue_lists_candidates_grouped_by_vendor(client):
    html = client.get("/").text
    for text in ("Zerto", "Commvault", "Zerto Release Notes", "What&#39;s New in 10.8", "Commvault Changelog",
                 "https://www.commvault.com/news"):
        assert text in html
    assert "Already Active" not in html
    assert "3 candidates" in html


def test_confirm_sets_active(client, session_factory):
    targets = ids(session_factory, vendor_name="Zerto")
    resp = client.post("/decide", data={"ids": targets, "table": "watched", "decision": "confirm"},
                       follow_redirects=False)
    assert resp.status_code == 303
    with session_factory() as s:
        rows = [s.get(WatchedSource, i) for i in targets]
    assert all(r.state == "active" and r.decided_at and r.confirmed_by == "owner" for r in rows)


def test_reject_then_reset(client, session_factory):
    target = ids(session_factory, title="Commvault Changelog")
    client.post("/decide", data={"ids": target, "table": "watched", "decision": "reject"})
    assert ids(session_factory, state="rejected") == target
    client.post("/decide", data={"ids": target, "table": "watched", "decision": "reset"})
    assert ids(session_factory, state="rejected") == []


def test_news_source_decision(client, session_factory):
    target = ids(session_factory, NewsSource)
    client.post("/decide", data={"ids": target, "table": "news", "decision": "confirm"})
    assert ids(session_factory, NewsSource, state="active") == target


def test_label_filter(client):
    html = client.get("/?label=unsure").text
    assert "What&#39;s New in 10.8" in html
    assert "Zerto Release Notes" not in html


def test_redirect_keeps_filters(client, session_factory):
    target = ids(session_factory, title="Commvault Changelog")
    resp = client.post("/decide", data={"ids": target, "table": "watched", "decision": "reject",
                                        "back": "/?label=unsure&vendor=Zerto"}, follow_redirects=False)
    assert resp.headers["location"] == "/?label=unsure&vendor=Zerto"


def test_bad_decision_rejected(client, session_factory):
    target = ids(session_factory, title="Commvault Changelog")
    resp = client.post("/decide", data={"ids": target, "table": "watched", "decision": "delete"})
    assert resp.status_code == 422


def add_event(session_factory, heading, status="needs_look", body="body", kind="new", **kw):
    from release_watch.models import EntryEvent

    with session_factory() as s:
        src = s.scalars(select(WatchedSource)).first()
        ev = EntryEvent(source_id=src.id, kind=kind, heading=heading, body=body, status=status,
                        reason=kw.pop("reason", "too_many_new"), url=kw.pop("url_override", src.url), **kw)
        s.add(ev)
        s.commit()
        return str(ev.id)


def event_status(session_factory, eid):
    from release_watch.models import EntryEvent

    with session_factory() as s:
        return s.get(EntryEvent, eid).status


def test_needs_look_lists_events(client, session_factory):
    add_event(session_factory, "October 28, 2026", summary="Adds X.")
    add_event(session_factory, "Already sent", status="sent")
    page = client.get("/needs-look").text
    assert "October 28, 2026" in page and "Adds X." in page
    assert "More than 3 new entries appeared on this page in one day" in page
    assert "Already sent" not in page
    assert "Needs a look (1)" in client.get("/").text


def test_send_moves_to_queued(client, session_factory):
    eid = add_event(session_factory, "A")
    resp = client.post("/events/decide", data={"ids": [eid], "decision": "send", "back": "/needs-look"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/needs-look"
    assert event_status(session_factory, eid) == "queued"


def test_dismiss(client, session_factory):
    eid = add_event(session_factory, "A")
    client.post("/events/decide", data={"ids": [eid], "decision": "dismiss"}, follow_redirects=False)
    assert event_status(session_factory, eid) == "dismissed"


def test_event_page_shows_verbatim_escaped(client, session_factory):
    eid = add_event(session_factory, "Edited one", kind="edited", body="<script>alert(1)</script> text",
                    diff="**Added**\n- new <b>line</b>")
    page = client.get(f"/events/{eid}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; text" in page and "<script>alert" not in page
    assert "<strong>Added</strong>" in page and "<li>new &lt;b&gt;line&lt;/b&gt;</li>" in page


def test_event_page_404(client):
    assert client.get("/events/00000000-0000-0000-0000-000000000000").status_code == 404


def test_invalid_filter_400(client):
    assert client.get("/?state=bogus").status_code == 400
    assert client.get("/?label=bogus").status_code == 400


def test_back_backslash_rejected(client, session_factory):
    eid = add_event(session_factory, "A")
    for back in ("/\\evil.com", "//evil.com", "https://evil.com"):
        resp = client.post("/events/decide", data={"ids": [eid], "decision": "dismiss", "back": back},
                           follow_redirects=False)
        assert resp.headers["location"] == "/needs-look"


def test_notifications_shows_next_digest_and_past_ones(client, session_factory):
    from datetime import UTC, datetime

    from release_watch.models import Digest
    from release_watch.notify.cards import _card, _text

    add_event(session_factory, "Queued entry heading", status="queued", body="## Queued entry heading\n\nNew **bold** text")
    with session_factory() as s:
        s.add(Digest(created_at=datetime(2026, 11, 3, 8, tzinfo=UTC), channel="web", cards=[
            _card([_text("**Past card** with [a link](https://acme.com/rn) and [bad](javascript:alert(1))")],
                  "https://acme.com/rn")]))
        s.commit()
    page = client.get("/notifications").text
    assert "Next digest" in page and "Queued entry heading" in page and "<strong>bold</strong>" in page
    assert "Tue 03 Nov 2026" in page and "<strong>Past card</strong>" in page
    assert '<a href="https://acme.com/rn"' in page and 'href="javascript' not in page
    assert "Open page" in page
    assert '<a href="/notifications">' in client.get("/").text


def add_moved(session_factory, reason="successor_redirected", body="Redirected to https://ibm.com/landing\n\nPreview words"):
    from release_watch.models import EntryEvent

    with session_factory() as s:
        src = s.scalars(select(WatchedSource).where(WatchedSource.state == "active")).first()
        src.follow = {"pin": {"version": "8.2.1", "signal": "https://ibm.com/sig", "successor": "https://ibm.com/{version}"}}
        ev = EntryEvent(source_id=src.id, kind="moved", heading="Release-notes URL for X may have moved to 8.2.2",
                        body=body, url="https://ibm.com/8.2.2", version="8.2.2", status="needs_look",
                        reason=reason, dedupe_key=f"moved:{src.id}:8.2.2")
        s.add(ev)
        s.commit()
        return str(ev.id), src.url


def test_needs_look_moved_item_explains_and_previews(client, session_factory):
    eid, old_url = add_moved(session_factory)
    page = client.get("/needs-look").text
    assert old_url in page and "https://ibm.com/8.2.2" in page and "8.2.1" in page
    assert "redirects to another page" in page and "Preview words" in page
    assert f'action="/events/{eid}/watch-new"' in page and "Watch the new page" in page


def test_watch_new_page_creates_the_successor(client, session_factory):
    eid, old_url = add_moved(session_factory)
    resp = client.post(f"/events/{eid}/watch-new", data={"url": "https://ibm.com/docs/8.2.2/whats-new"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/needs-look"
    with session_factory() as s:
        new = s.scalars(select(WatchedSource).where(WatchedSource.url == "https://ibm.com/docs/8.2.2/whats-new")).one()
        old = s.scalars(select(WatchedSource).where(WatchedSource.url == old_url)).one()
        assert (new.state, new.origin, old.superseded_by) == ("active", "successor", new.id)
    assert event_status(session_factory, eid) == "dismissed"


def test_watch_new_page_needs_a_web_url(client, session_factory):
    eid, _ = add_moved(session_factory)
    assert client.post(f"/events/{eid}/watch-new", data={"url": "javascript:alert(1)"}).status_code == 400
    assert event_status(session_factory, eid) == "needs_look"


def test_event_page_never_links_javascript(client, session_factory):
    eid = add_event(session_factory, "A", url_override="javascript:alert(1)")
    assert 'href="javascript' not in client.get(f"/events/{eid}").text
