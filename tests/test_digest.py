import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from release_watch.models import EntryEvent, JobRun, WatchedSource
from release_watch.notify.digest import run_digest
from release_watch.notify.teams import Teams, TeamsError

HOOK = "https://prod.westus.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SECRETSIG"
NOW = datetime(2026, 11, 4, 8, tzinfo=UTC)


async def no_sleep(_):
    return None


def teams():
    return Teams(HOOK, sleep=no_sleep)


def seed(sf, n_new=2, watch_ok_at=NOW - timedelta(hours=2)):
    with sf() as s:
        src = WatchedSource(vendor_name="Acme", url="https://acme.com/rn", url_key="https://acme.com/rn",
                            title="Updates", kind="entry_page", state="active", origin="seed")
        s.add(src)
        s.flush()
        s.add(JobRun(job="watch", started_at=watch_ok_at - timedelta(minutes=10), finished_at=watch_ok_at,
                     status="ok"))
        for i in range(n_new):
            s.add(EntryEvent(source_id=src.id, kind="new", heading=f"Entry {i}", body=f"## Entry {i}\n\nText {i}",
                             summary=f"Sum {i}", url=src.url, status="queued", created_at=NOW - timedelta(hours=3 - i)))
        s.add(EntryEvent(source_id=src.id, kind="version", heading="Acme released a new version of Box: 2.0",
                         url="https://acme.com/v2", version="2.0", status="queued", created_at=NOW - timedelta(hours=4)))
        s.add(EntryEvent(source_id=src.id, kind="new", heading="Held", body="x", status="needs_look"))
        s.commit()


def statuses(sf):
    with sf() as s:
        return {e.heading: e.status for e in s.scalars(select(EntryEvent))}


def posted_texts(route):
    out = []
    for call in route.calls:
        card = json.loads(call.request.content)["attachments"][0]["content"]
        out.append("\n".join(b.get("text", "") for b in card["body"]))
    return out


@respx.mock
async def test_digest_sends_summary_then_entries(session_factory):
    seed(session_factory)
    route = respx.post(HOOK).mock(return_value=httpx.Response(202))
    counts = await run_digest(session_factory, teams(), web_base_url="http://rw", now=NOW)
    texts = posted_texts(route)
    assert len(texts) == 3
    assert "Acme released a new version of Box: 2.0" in texts[0] and "Acme: 2 new" in texts[0]
    assert "Entry 0" in texts[1] and "Sum 0" in texts[1] and "Entry 1" in texts[2]
    assert counts == {"cards": 3, "events_sent": 3}
    st = statuses(session_factory)
    assert st["Held"] == "needs_look"
    assert {st["Entry 0"], st["Entry 1"], st["Acme released a new version of Box: 2.0"]} == {"sent"}


@respx.mock
async def test_digest_marks_sent_per_card(session_factory):
    seed(session_factory, n_new=3)
    route = respx.post(HOOK).mock(side_effect=[httpx.Response(202), httpx.Response(202), httpx.Response(400),
                                               httpx.Response(400)])
    with pytest.raises(TeamsError):
        await run_digest(session_factory, teams(), web_base_url="http://rw", now=NOW)
    st = statuses(session_factory)
    assert st["Entry 0"] == "sent" and st["Entry 1"] == "queued" and st["Entry 2"] == "queued"
    assert route.call_count == 3


@respx.mock
async def test_digest_needs_look_not_sent_and_empty_day(session_factory):
    seed(session_factory, n_new=0)
    with session_factory() as s:
        s.query(EntryEvent).filter(EntryEvent.kind == "version").delete()
        s.commit()
    route = respx.post(HOOK).mock(return_value=httpx.Response(202))
    await run_digest(session_factory, teams(), web_base_url="http://rw", now=NOW)
    [text] = posted_texts(route)
    assert "No changes; 0 of 1 sources checked" in text and "1 in needs a look" in text


@respx.mock
async def test_digest_stale_watch_alert(session_factory):
    seed(session_factory, n_new=0, watch_ok_at=NOW - timedelta(hours=30))
    route = respx.post(HOOK).mock(return_value=httpx.Response(202))
    await run_digest(session_factory, teams(), web_base_url="http://rw", now=NOW)
    texts = posted_texts(route)
    assert texts[0].startswith("Release Watch: last successful watch was 30 h ago")


async def test_digest_dry_run_changes_nothing(session_factory):
    seed(session_factory)
    printed = []
    await run_digest(session_factory, None, web_base_url="http://rw", now=NOW, dry_run=True, out=printed.append)
    assert len(printed) == 3 and json.loads(printed[0])["type"] == "message"
    assert "sent" not in statuses(session_factory).values()


async def test_digest_without_webhook_fails(session_factory):
    seed(session_factory)
    with pytest.raises(TeamsError, match="RW_TEAMS_WEBHOOK_URL"):
        await run_digest(session_factory, None, web_base_url="http://rw", now=NOW)


@respx.mock
async def test_webhook_url_never_logged(caplog):
    respx.post(HOOK).mock(side_effect=httpx.ConnectError("boom"))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TeamsError) as exc:
        await teams().post({"type": "AdaptiveCard"})
    assert "SECRETSIG" not in caplog.text and "SECRETSIG" not in str(exc.value)
    assert "logic.azure.com" not in str(exc.value)


@respx.mock
async def test_teams_retries_once_on_429():
    route = respx.post(HOOK).mock(side_effect=[httpx.Response(429), httpx.Response(202)])
    await teams().post({"type": "AdaptiveCard"})
    assert route.call_count == 2
