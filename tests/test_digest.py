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
    assert "**Acme**\n\n- Version **2.0** is available: [Updates](https://acme.com/v2)" in texts[0] and "Acme: 2 new" in texts[0]
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


def product(s, name, version):
    pid = uuid.uuid4()
    s.add(CatalogProduct(de_id=pid, vendor_de_id=uuid.uuid4(), name=name, de_version=version, synced_at=NOW))
    return pid


def page(s, vendor, pid, url, h1=None, title="What's new"):
    src = WatchedSource(vendor_name=vendor, product_de_id=pid, url=url, url_key=url, title=title, kind="entry_page",
                        state="active", origin="successor")
    s.add(src)
    s.flush()
    if h1:
        s.add(Entry(source_id=src.id, key="k", heading="what's new", body_hash="h", body=f"# {h1}\n\nText"))
    return src


async def test_digest_says_what_docextractor_needs(session_factory):
    with session_factory() as s:
        sp, df, rp = (product(s, "Storage Protect", "8.2.2"), product(s, "Storage Defender", "2.3.1"),
                      product(s, "Resiliency Platform", "10.3"))
        mail = page(s, "IBM", sp, "https://ibm.com/spfm/8.2.2", "What's new for IBM Storage Protect for Mail")
        sentinel = page(s, "IBM", df, "https://ibm.com/sentinel/2.3.2", "What's new for IBM Storage Defender Sentinel")
        coh = page(s, "Cohesity", rp, "https://cohesity.com/rp", title="RP docs")
        for heading in ("old mail page moved", "old domino page moved"):  # DocExtractor already tracks 8.2.2
            s.add(EntryEvent(source_id=mail.id, kind="moved", version="8.2.2", product_de_id=sp, url=mail.url,
                             heading=heading, status="queued"))
        s.add(EntryEvent(source_id=mail.id, kind="version", version="8.2.2", product_de_id=sp, url=mail.url,
                         heading="bumped since", status="queued"))
        s.add(EntryEvent(source_id=sentinel.id, kind="moved", version="2.3.2", product_de_id=df, url=sentinel.url,
                         heading="sentinel moved", status="queued"))
        s.add(EntryEvent(source_id=coh.id, kind="version", version="10.4", product_de_id=rp, url=coh.url,
                         heading="Cohesity released 10.4", status="queued"))
        s.add(EntryEvent(source_id=mail.id, kind="moved", product_de_id=sp, url="https://ibm.com/b",
                         reason="docextractor_moved", status="queued",
                         heading="Storage Protect for Mail: the release-notes page [a](https://ibm.com/a) now "
                                 "redirects to [b](https://ibm.com/b)"))
        s.commit()

    texts = [b["text"] for b in preview_digest(session_factory, web_base_url="http://rw", now=NOW)[-1]["body"]]
    assert texts[1:6] == [
        "**New versions**",
        "**Cohesity · Resiliency Platform**\n\n- Version **10.4** is available (DocExtractor tracks 10.3): "
        "[RP docs](https://cohesity.com/rp)",
        "**IBM · Storage Defender**\n\n- Version **2.3.2** is available (DocExtractor tracks 2.3.1): "
        "[What's new for IBM Storage Defender Sentinel](https://ibm.com/sentinel/2.3.2)",
        "**Update in DocExtractor**",
        "**IBM · Storage Protect**\n\n- Storage Protect for Mail: the release-notes page [a](https://ibm.com/a) now "
        "redirects to [b](https://ibm.com/b)",
    ]
    assert not any("8.2.2" in text for text in texts)

    await run_digest(session_factory, None, web_base_url="http://rw", now=NOW)
    st = statuses(session_factory)
    assert {st["old mail page moved"], st["old domino page moved"], st["bumped since"]} == {"dismissed"}
    assert {st["sentinel moved"], st["Cohesity released 10.4"]} == {"sent"}


def test_several_pages_of_one_new_version_share_a_line(session_factory):
    with session_factory() as s:
        sp = product(s, "Storage Protect", "8.2.1")
        for code in ("spfm", "spfve"):
            src = page(s, "IBM", sp, f"https://ibm.com/{code}/8.2.2", f"What's new for IBM {code}")
            s.add(EntryEvent(source_id=src.id, kind="moved", version="8.2.2", product_de_id=sp, url=src.url,
                             heading=code, status="queued"))
        s.commit()
    texts = [b["text"] for b in preview_digest(session_factory, web_base_url="http://rw", now=NOW)[-1]["body"]]
    assert "**IBM · Storage Protect**\n\n- Version **8.2.2** is available (DocExtractor tracks 8.2.1): " \
           "[What's new for IBM spfm](https://ibm.com/spfm/8.2.2), " \
           "[What's new for IBM spfve](https://ibm.com/spfve/8.2.2)" in texts


def test_generic_page_heading_gets_the_product_the_page_names(session_factory):
    with session_factory() as s:
        src = WatchedSource(vendor_name="IBM", url="https://ibm.com/scdm/2.3.2", url_key="https://ibm.com/scdm/2.3.2",
                            title="What's new", kind="entry_page", state="active", origin="successor")
        s.add(src)
        s.flush()
        s.add(Entry(source_id=src.id, key="k", heading="what's new", body_hash="h",
                    body="# What's new\n\nLearn about the new features in IBM® Storage Defender Copy Data Management. "
                         "IBM Storage Defender Copy Data Management 2.3.2 adds IBM Cloud support."))
        s.add(EntryEvent(source_id=src.id, kind="moved", version="2.3.2", url=src.url, status="queued",
                         heading="Release-notes URL for IBM Storage Defender (What's new) moved to 2.3.2"))
        s.commit()
    summary = preview_digest(session_factory, web_base_url="http://rw", now=NOW)[-1]
    assert any("[IBM Storage Defender Copy Data Management: What's new](https://ibm.com/scdm/2.3.2)" in b["text"]
               for b in summary["body"])


def test_named_product_must_be_the_tracked_product(session_factory):
    pid = uuid.uuid4()
    with session_factory() as s:
        s.add(CatalogProduct(de_id=pid, vendor_de_id=uuid.uuid4(), name="Storage Protect", synced_at=NOW))
        src = WatchedSource(vendor_name="IBM", product_de_id=pid, url="https://ibm.com/sps", url_key="https://ibm.com/sps",
                            title="What's new for Oracle", kind="entry_page", state="active", origin="successor")
        s.add(src)
        s.flush()
        s.add(Entry(source_id=src.id, key="k", heading="New in 8.2.1", body_hash="h",
                    body="## New in IBM Storage Protect Snapshot 8.2.1\n\nIBM FlashSystem snapshots on IBM FlashSystem."))
        s.add(EntryEvent(source_id=src.id, kind="moved", version="8.2.2", product_de_id=pid, url=src.url,
                         heading="x", status="queued"))
        s.commit()
    summary = preview_digest(session_factory, web_base_url="http://rw", now=NOW)[-1]
    assert any("[IBM Storage Protect Snapshot: What's new for Oracle](https://ibm.com/sps)" in b["text"]
               for b in summary["body"])
