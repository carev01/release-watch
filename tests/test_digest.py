import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from release_watch.models import CatalogProduct, Digest, Entry, EntryEvent, JobRun, WatchedSource
from release_watch.notify.digest import preview_digest, run_digest
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
                            title="Updates", kind="entry_page", state="active", origin="seed",
                            created_at=NOW - timedelta(days=1))
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
    assert "**Acme**\n\n- New version **2.0** ([docs page](https://acme.com/v2))" in texts[0] and "Acme: 2 new" in texts[0]
    assert "Entry 0" in texts[1] and "Sum 0" in texts[1] and "Entry 1" in texts[2]
    assert counts == {"cards": 3, "events_sent": 3, "channel": "teams"}
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


async def test_digest_without_webhook_is_kept_for_the_web_ui(session_factory):
    seed(session_factory)
    counts = await run_digest(session_factory, None, web_base_url="http://rw", now=NOW)
    assert counts == {"cards": 3, "events_sent": 3, "channel": "web"}
    st = statuses(session_factory)
    assert st["Entry 0"] == "sent" and st["Held"] == "needs_look"
    with session_factory() as s:
        [d] = s.scalars(select(Digest)).all()
    assert d.channel == "web" and len(d.cards) == 3 and d.created_at == NOW
    assert "Entry 0" in json.dumps(d.cards[1])


@respx.mock
async def test_digest_sent_to_teams_is_kept_too(session_factory):
    seed(session_factory)
    respx.post(HOOK).mock(return_value=httpx.Response(202))
    await run_digest(session_factory, teams(), web_base_url="http://rw", now=NOW)
    with session_factory() as s:
        [d] = s.scalars(select(Digest)).all()
    assert d.channel == "teams" and len(d.cards) == 3


async def test_preview_changes_nothing(session_factory):
    seed(session_factory)
    cards = preview_digest(session_factory, web_base_url="http://rw", now=NOW)
    assert len(cards) == 3 and "Entry 1" in json.dumps(cards[2])
    assert "sent" not in statuses(session_factory).values()
    with session_factory() as s:
        assert s.scalars(select(Digest)).all() == []


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


async def test_any_post_error_hides_the_url():
    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise ValueError(f"bad url {request.url}")

    with pytest.raises(TeamsError) as exc:
        await Teams(HOOK, transport=Boom(), sleep=no_sleep).post({"type": "AdaptiveCard"})
    assert "SECRETSIG" not in str(exc.value) and "logic.azure.com" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_version_and_moved_lines_grouped_by_vendor_and_product(session_factory):
    sp, rp = uuid.uuid4(), uuid.uuid4()
    with session_factory() as s:
        s.add(CatalogProduct(de_id=sp, vendor_de_id=uuid.uuid4(), name="Storage Protect", synced_at=NOW))
        s.add(CatalogProduct(de_id=rp, vendor_de_id=uuid.uuid4(), name="Resiliency Platform", de_version="10.3",
                             synced_at=NOW))
        mail = WatchedSource(vendor_name="IBM", product_de_id=sp, url="https://ibm.com/spfm/8.2.2",
                             url_key="https://ibm.com/spfm/8.2.2", title="What's new", kind="entry_page",
                             state="active", origin="seed")
        db = WatchedSource(vendor_name="IBM", product_de_id=sp, url="https://ibm.com/spfd/8.2.2",
                           url_key="https://ibm.com/spfd/8.2.2", title="What's new", kind="entry_page",
                           state="active", origin="heuristic")
        coh = WatchedSource(vendor_name="Cohesity", product_de_id=rp, url="https://cohesity.com/rp",
                            url_key="https://cohesity.com/rp", title="RP docs", kind="index_page",
                            state="active", origin="seed")
        s.add_all([mail, db, coh])
        s.flush()
        s.add(Entry(source_id=mail.id, key="k", heading="what's new", body_hash="h",
                    body="# What's new for IBM Storage Protect for Mail\n\nText"))
        for _ in range(2):  # two old pages merged into the same new one
            s.add(EntryEvent(source_id=mail.id, kind="moved", version="8.2.2", product_de_id=sp, url=mail.url,
                             heading="Release-notes URL for IBM Storage Protect (What's new) moved to 8.2.2",
                             status="queued"))
        s.add(EntryEvent(source_id=db.id, kind="moved", product_de_id=sp, url=db.url, status="queued",
                         heading="Release-notes URL for IBM Storage Protect (What's new) moved: DocExtractor now uses "
                                 "the new URL"))
        s.add(EntryEvent(source_id=coh.id, kind="version", version="10.4", product_de_id=rp, url=coh.url,
                         heading="Cohesity released a new version of Resiliency Platform: 10.4", status="queued"))
        s.commit()

    summary = preview_digest(session_factory, web_base_url="http://rw", now=NOW)[-1]
    texts = [b["text"] for b in summary["body"]]
    assert "**Cohesity · Resiliency Platform**\n\n- New version **10.4** (DocExtractor tracks 10.3) " \
           "([docs page](https://cohesity.com/rp))" in texts
    assert "**IBM · Storage Protect**\n\n" \
           "- [What's new for IBM Storage Protect for Mail](https://ibm.com/spfm/8.2.2) is the 8.2.2 page " \
           "(replaces 2 older pages)\n" \
           "- [What's new](https://ibm.com/spfd/8.2.2): DocExtractor now uses this URL" in texts
    assert texts.index(next(t for t in texts if t.startswith("**Cohesity"))) < \
        texts.index(next(t for t in texts if t.startswith("**IBM")))
