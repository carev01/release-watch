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
