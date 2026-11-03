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
    fake = FakeDocExtractor(
        tocs={ZERTO_RN: [toc_entry("Release Notes for 10.8", "https://help.zerto.com/rn/10.8")]},
        search={"release notes": [{"id": str(uuid.uuid4()), "source_id": ZERTO_RN, "title": "Release Notes 10.7",
                                   "source_url": "https://help.zerto.com/rn/10.7"}]},
    )
    proposals = await propose(session_factory, fake)
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
    proposals = [p for p in await propose(session_factory, fake) if p.de_source_id == ZERTO_ADMIN]
    assert len(proposals) == 1
    assert proposals[0].kind_hint == "index_page"
    assert proposals[0].url == "https://help.zerto.com/admin/rn/10.0"
    assert proposals[0].reason == "toc"


async def test_search_body_only_hit_dropped(session_factory):
    fake = FakeDocExtractor(search={"release notes": [
        {"id": str(uuid.uuid4()), "source_id": KASTEN, "title": "Configuring backups",
         "source_url": "https://docs.kasten.io/latest/config"},
    ]})
    proposals = await propose(session_factory, fake)
    assert not [p for p in proposals if p.de_source_id == KASTEN]


async def test_search_title_hit_proposed(session_factory):
    article = str(uuid.uuid4())
    fake = FakeDocExtractor(search={"what's new": [
        {"id": article, "source_id": KASTEN, "title": "What's New in 8.0",
         "source_url": "https://docs.kasten.io/latest/releasenotes"},
    ]})
    proposals = [p for p in await propose(session_factory, fake) if p.de_source_id == KASTEN]
    assert len(proposals) == 1
    assert proposals[0].reason == "search"
    assert proposals[0].de_article_id == article
    assert proposals[0].vendor_name == "Veeam"


async def test_excluded_product_skipped(session_factory):
    proposals = await propose(session_factory, FakeDocExtractor())
    assert not [p for p in proposals if p.de_source_id == VEEAM_ONE_RN]


def _proposal(url, article=None, source=ZERTO_RN):
    return Proposal(vendor_name="Zerto", product_de_id=ZERTO_PRODUCT, de_source_id=source,
                    de_article_id=article, url=url, title="RN", kind_hint="entry_page", reason="search")


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
    assert (row.state, row.origin, row.kind, row.discovery_reason) == ("candidate", "heuristic", "entry_page", "search")


async def test_login_walled_flagged(session_factory):
    fake = FakeDocExtractor()
    fake.catalog["sources"][0]["auth_realm_id"] = str(uuid.uuid4())
    await seeded(session_factory, fake)
    with session_factory() as s:
        upsert_proposals(s, [_proposal("https://help.zerto.com/rn")])
        assert s.scalars(select(WatchedSource)).one().login_walled is True
