import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from release_watch.catalog import sync_catalog
from release_watch.config import DEFAULT_EXCLUDED_PRODUCTS
from release_watch.discovery.heuristic import Proposal, propose_candidates, upsert_proposals
from release_watch.models import WatchedSource
from tests.fakes import FakeDocExtractor

T0 = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
ZERTO_RN = "00000000-0000-0000-0000-0000000000c1"
ZERTO_ADMIN = "00000000-0000-0000-0000-0000000000c2"
VEEAM_ONE_RN = "00000000-0000-0000-0000-0000000000c3"
KASTEN = "00000000-0000-0000-0000-0000000000c4"
ZERTO_PRODUCT = "00000000-0000-0000-0000-0000000000b1"


def toc_entry(title, url=None, children=(), article_id=None):
    return {"title": title, "url": url, "level": 0, "is_article": url is not None,
            "article_id": article_id, "children": list(children)}


async def seeded(session_factory, fake):
    with session_factory() as s:
        await sync_catalog(fake, s, DEFAULT_EXCLUDED_PRODUCTS, T0)


async def propose(session_factory, fake):
    await seeded(session_factory, fake)
    with session_factory() as s:
        return await propose_candidates(fake, s)


async def test_source_name_match_proposes_source_once(session_factory):
    fake = FakeDocExtractor(tocs={ZERTO_RN: [toc_entry("Release Notes for 10.8", "https://help.zerto.com/rn/10.8")]})
    proposals, _ = await propose(session_factory, fake)
    zerto = [p for p in proposals if p.de_source_id == ZERTO_RN]
    assert len(zerto) == 1
    assert zerto[0].reason == "source_name"
    assert zerto[0].url == "https://help.zerto.com/rn"


async def test_toc_branch_collapses_children(session_factory):
    children = [toc_entry(f"10.{i}", f"https://help.zerto.com/admin/rn/10.{i}") for i in range(5)]
    fake = FakeDocExtractor(tocs={ZERTO_ADMIN: [
        toc_entry("Installing", "https://help.zerto.com/admin/install"),
        toc_entry("Release Notes", None, children),
    ]})
    proposals = [p for p in (await propose(session_factory, fake))[0] if p.de_source_id == ZERTO_ADMIN]
    assert len(proposals) == 1
    assert proposals[0].kind_hint == "index_page"
    assert proposals[0].url == "https://help.zerto.com/admin/rn/10.0"
    assert proposals[0].reason == "toc"


async def test_toc_url_match_proposed(session_factory):
    article = str(uuid.uuid4())
    fake = FakeDocExtractor(tocs={KASTEN: [
        toc_entry("Install", "https://docs.kasten.io/latest/install"),
        toc_entry("8.0", "https://docs.kasten.io/latest/releasenotes/8.0", article_id=article),
    ]})
    proposals = [p for p in (await propose(session_factory, fake))[0] if p.de_source_id == KASTEN]
    assert len(proposals) == 1
    assert (proposals[0].reason, proposals[0].de_article_id, proposals[0].vendor_name) == ("toc", article, "Veeam")


async def test_toc_error_skips_source_and_counts(session_factory):
    fake = FakeDocExtractor(tocs={ZERTO_ADMIN: [toc_entry("Release Notes", "https://help.zerto.com/admin/rn")]},
                            toc_fail={KASTEN})
    proposals, toc_errors = await propose(session_factory, fake)
    assert toc_errors == 1
    assert [p.de_source_id for p in proposals if p.reason == "toc"] == [ZERTO_ADMIN]


async def test_excluded_product_skipped(session_factory):
    proposals, _ = await propose(session_factory, FakeDocExtractor())
    assert not [p for p in proposals if p.de_source_id == VEEAM_ONE_RN]


def _proposal(url, article=None, source=ZERTO_RN):
    return Proposal(vendor_name="Zerto", product_de_id=ZERTO_PRODUCT, de_source_id=source,
                    de_article_id=article, url=url, title="RN", kind_hint="entry_page", reason="toc")


async def test_upsert_moved_article_updates_url(session_factory):
    await seeded(session_factory, FakeDocExtractor())
    article = str(uuid.uuid4())
    with session_factory() as s:
        upsert_proposals(s, [_proposal("https://docs.x.com/11.44/rn", article)])
        counts = upsert_proposals(s, [_proposal("https://docs.x.com/11.46/rn", article)])
        rows = s.scalars(select(WatchedSource)).all()
    assert counts["moved"] == 1
    assert [r.url for r in rows] == ["https://docs.x.com/11.46/rn"]
    assert rows[0].url_key == "https://docs.x.com/11.46/rn"


async def test_upsert_rejected_stays_rejected(session_factory):
    await seeded(session_factory, FakeDocExtractor())
    with session_factory() as s:
        upsert_proposals(s, [_proposal("https://x.com/rn")])
        s.scalars(select(WatchedSource)).one().state = "rejected"
        s.commit()
        counts = upsert_proposals(s, [_proposal("http://x.com/rn/#top")])
        assert s.scalar(select(func.count()).select_from(WatchedSource)) == 1
        assert s.scalars(select(WatchedSource)).one().state == "rejected"
    assert counts["skipped_rejected"] == 1


async def test_upsert_new_row_is_candidate(session_factory):
    await seeded(session_factory, FakeDocExtractor())
    with session_factory() as s:
        counts = upsert_proposals(s, [_proposal("https://x.com/rn")])
        row = s.scalars(select(WatchedSource)).one()
    assert counts["new"] == 1
    assert (row.state, row.origin, row.kind, row.discovery_reason) == ("candidate", "heuristic", "entry_page", "toc")


async def test_login_walled_flagged(session_factory):
    fake = FakeDocExtractor()
    fake.catalog["sources"][0]["auth_realm_id"] = str(uuid.uuid4())
    await seeded(session_factory, fake)
    with session_factory() as s:
        upsert_proposals(s, [_proposal("https://help.zerto.com/rn")])
        assert s.scalars(select(WatchedSource)).one().login_walled is True


def test_release_pattern_variants():
    from release_watch.discovery.heuristic import RELEASE_PATTERN
    for text in ("What’s New in 8.0", "whats-new", "/docs/what-s-new.html", "What's new", "Readme"):
        assert RELEASE_PATTERN.search(text), text
    assert not RELEASE_PATTERN.search("Understanding readMetadata issues")


async def test_upsert_moved_onto_existing_key_is_conflict(session_factory):
    await seeded(session_factory, FakeDocExtractor())
    article = str(uuid.uuid4())
    with session_factory() as s:
        upsert_proposals(s, [_proposal("https://docs.x.com/11.44/rn", article),
                             _proposal("https://docs.x.com/11.46/rn")])
        counts = upsert_proposals(s, [_proposal("https://docs.x.com/11.46/rn", article)])
        assert s.scalar(select(func.count()).select_from(WatchedSource)) == 2
    assert counts["conflict"] == 1


async def test_source_name_row_follows_base_url_move(session_factory):
    fake = FakeDocExtractor()
    await seeded(session_factory, fake)
    with session_factory() as s:
        upsert_proposals(s, (await propose_candidates(fake, s))[0])
    fake.catalog["sources"][0]["base_url"] = "https://help.zerto.com/rn-10.9"
    await seeded(session_factory, fake)
    with session_factory() as s:
        counts = upsert_proposals(s, (await propose_candidates(fake, s))[0])
        rows = s.scalars(select(WatchedSource).where(WatchedSource.de_source_id == uuid.UUID(ZERTO_RN))).all()
    assert [r.url for r in rows] == ["https://help.zerto.com/rn-10.9"]
    assert counts["moved"] == 1


async def test_toc_branch_row_follows_new_first_child(session_factory):
    def tree(first):
        return {ZERTO_ADMIN: [toc_entry("Release Notes", None, [
            toc_entry(f"10.{i}", f"https://help.zerto.com/admin/rn/10.{i}") for i in range(first, 3, -1)])]}
    fake = FakeDocExtractor(tocs=tree(4))
    await seeded(session_factory, fake)
    with session_factory() as s:
        upsert_proposals(s, (await propose_candidates(fake, s))[0])
    fake.tocs = tree(5)
    with session_factory() as s:
        upsert_proposals(s, (await propose_candidates(fake, s))[0])
        rows = s.scalars(select(WatchedSource).where(WatchedSource.de_source_id == uuid.UUID(ZERTO_ADMIN))).all()
    assert len(rows) == 1
    assert rows[0].url == "https://help.zerto.com/admin/rn/10.5"


async def test_same_titled_branches_stay_separate(session_factory):
    def branch(prefix):
        return toc_entry("Release Notes", None, [toc_entry("1", f"https://help.zerto.com/admin/{prefix}/1"),
                                                 toc_entry("2", f"https://help.zerto.com/admin/{prefix}/2")])
    fake = FakeDocExtractor(tocs={ZERTO_ADMIN: [toc_entry("Server", None, [branch("server")]),
                                                toc_entry("Agent", None, [branch("agent")])]})
    await seeded(session_factory, fake)
    with session_factory() as s:
        first = upsert_proposals(s, (await propose_candidates(fake, s))[0])
        again = upsert_proposals(s, (await propose_candidates(fake, s))[0])
        urls = sorted(r.url for r in s.scalars(
            select(WatchedSource).where(WatchedSource.de_source_id == uuid.UUID(ZERTO_ADMIN))))
    assert first["new"] == 3  # the two branches, plus the "Zerto Release Notes" source itself
    assert (again["existing"], again["moved"], again["conflict"]) == (3, 0, 0)
    assert urls == ["https://help.zerto.com/admin/agent/1", "https://help.zerto.com/admin/server/1"]


async def test_moved_active_row_rebaselines_without_a_line(session_factory):
    from release_watch.models import Entry, EntryEvent, Snapshot
    await seeded(session_factory, FakeDocExtractor())
    article = str(uuid.uuid4())
    with session_factory() as s:
        upsert_proposals(s, [_proposal("https://docs.x.com/11.44/rn", article)])
        row = s.scalars(select(WatchedSource)).one()
        row.state, row.title = "active", "RN 11.44"
        s.add_all([Entry(source_id=row.id, key="k", heading="k", body="b", body_hash="h"),
                   Snapshot(source_id=row.id, markdown="m", hash="h"),
                   EntryEvent(source_id=row.id, kind="moved", heading="may have moved", status="needs_look",
                              reason="successor_redirected", dedupe_key=f"moved:{row.id}:11.46")])
        s.commit()
        upsert_proposals(s, [_proposal("https://docs.x.com/11.46/rn", article)])
        assert s.scalar(select(func.count()).select_from(Entry)) == 0
        assert s.scalar(select(func.count()).select_from(Snapshot)) == 0
        evs = {e.status: e for e in s.scalars(select(EntryEvent))}
    assert evs["dismissed"].heading == "may have moved"  # DocExtractor settled it
    assert "queued" not in evs  # DocExtractor made the change itself: nothing to tell the owner


async def test_moved_candidate_row_gets_no_line(session_factory):
    from release_watch.models import EntryEvent
    await seeded(session_factory, FakeDocExtractor())
    article = str(uuid.uuid4())
    with session_factory() as s:
        upsert_proposals(s, [_proposal("https://docs.x.com/11.44/rn", article)])
        upsert_proposals(s, [_proposal("https://docs.x.com/11.46/rn", article)])
        assert s.scalar(select(func.count()).select_from(EntryEvent)) == 0
