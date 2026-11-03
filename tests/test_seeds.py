from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from release_watch.catalog import sync_catalog
from release_watch.config import DEFAULT_EXCLUDED_PRODUCTS
from release_watch.discovery.seeds import load_seeds
from release_watch.models import NewsSource, WatchedSource
from tests.fakes import FakeDocExtractor

TWO_ENTRIES = """
watched_sources:
  - vendor: Zerto
    product: Zerto
    url: https://help.zerto.com/changelog
    kind: entry_page
    title: Zerto changelog
    notes: "Public changelog"
news_sources:
  - vendor: Commvault
    product: null
    url: https://www.commvault.com/news
    kind: listing
    notes: "Press releases"
"""


@pytest.fixture
async def catalog(session_factory):
    with session_factory() as s:
        await sync_catalog(FakeDocExtractor(), s, DEFAULT_EXCLUDED_PRODUCTS, datetime(2026, 11, 3, tzinfo=UTC))
    return session_factory


def write(tmp_path, text):
    path = tmp_path / "sources.yaml"
    path.write_text(text)
    return path


def test_seed_inserts_candidates(catalog, tmp_path):
    with catalog() as s:
        counts = load_seeds(s, write(tmp_path, TWO_ENTRIES))
        watched = s.scalars(select(WatchedSource)).one()
        news = s.scalars(select(NewsSource)).one()
    assert counts == {"watched_new": 1, "news_new": 1, "unchanged": 0, "skipped_rejected": 0}
    assert (watched.state, watched.origin, watched.notes) == ("candidate", "seed", "Public changelog")
    assert watched.product_de_id is not None
    assert (news.state, news.origin, news.kind) == ("candidate", "seed", "listing")


def test_seed_idempotent(catalog, tmp_path):
    path = write(tmp_path, TWO_ENTRIES)
    with catalog() as s:
        load_seeds(s, path)
        counts = load_seeds(s, path)
        assert s.scalar(select(func.count()).select_from(WatchedSource)) == 1
    assert counts["unchanged"] == 2


def test_seed_rejected_not_reopened(catalog, tmp_path):
    path = write(tmp_path, TWO_ENTRIES)
    with catalog() as s:
        load_seeds(s, path)
        s.scalars(select(WatchedSource)).one().state = "rejected"
        s.commit()
        counts = load_seeds(s, path)
        assert s.scalars(select(WatchedSource)).one().state == "rejected"
    assert counts["skipped_rejected"] == 1


def test_seed_unknown_vendor_errors(catalog, tmp_path):
    bad = "watched_sources:\n  - vendor: Nope\n    url: https://nope.com\n    kind: entry_page\n"
    with catalog() as s, pytest.raises(ValueError, match="entry 0"):
        load_seeds(s, write(tmp_path, bad))


def test_section_key_includes_selector(catalog, tmp_path):
    two_sections = """
watched_sources:
  - {vendor: Zerto, url: "https://help.zerto.com/updates", kind: section, selector: "#updates"}
  - {vendor: Zerto, url: "https://help.zerto.com/updates", kind: section, selector: "#pmc"}
"""
    with catalog() as s:
        counts = load_seeds(s, write(tmp_path, two_sections))
        keys = sorted(r.url_key for r in s.scalars(select(WatchedSource)))
    assert counts["watched_new"] == 2
    assert keys == ["https://help.zerto.com/updates#selector=#pmc", "https://help.zerto.com/updates#selector=#updates"]


def test_section_without_selector_errors(catalog, tmp_path):
    bad = "watched_sources:\n  - {vendor: Zerto, url: 'https://help.zerto.com/u', kind: section}\n"
    with catalog() as s, pytest.raises(ValueError, match="selector"):
        load_seeds(s, write(tmp_path, bad))
