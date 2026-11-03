from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from release_watch.catalog import sync_catalog
from release_watch.config import DEFAULT_EXCLUDED_PRODUCTS
from release_watch.docextractor import DocExtractorError
from release_watch.models import CatalogProduct, CatalogSource, CatalogVendor
from tests.fakes import FakeDocExtractor

T0 = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)


async def test_sync_inserts_and_counts(session_factory):
    with session_factory() as s:
        counts = await sync_catalog(FakeDocExtractor(), s, DEFAULT_EXCLUDED_PRODUCTS, T0)
    assert counts == {"vendors": 3, "products": 4, "sources": 5, "deactivated": 0, "excluded": 1}


async def test_sync_deactivates_missing(session_factory):
    fake = FakeDocExtractor()
    with session_factory() as s:
        await sync_catalog(fake, s, DEFAULT_EXCLUDED_PRODUCTS, T0)
    fake.catalog["sources"].pop()
    with session_factory() as s:
        counts = await sync_catalog(fake, s, DEFAULT_EXCLUDED_PRODUCTS, T0 + timedelta(days=1))
        inactive = s.scalars(select(CatalogSource).where(CatalogSource.active.is_(False))).all()
    assert counts["deactivated"] == 1
    assert [x.name for x in inactive] == ["Commvault Docs"]


async def test_sync_failure_changes_nothing(session_factory):
    with session_factory() as s:
        await sync_catalog(FakeDocExtractor(), s, DEFAULT_EXCLUDED_PRODUCTS, T0)
    with session_factory() as s:
        with pytest.raises(DocExtractorError):
            await sync_catalog(FakeDocExtractor(fail_on="sources"), s, DEFAULT_EXCLUDED_PRODUCTS,
                               T0 + timedelta(days=1))
    with session_factory() as s:
        for model in (CatalogVendor, CatalogProduct, CatalogSource):
            rows = s.scalars(select(model)).all()
            assert rows and all(r.active and r.synced_at == T0 for r in rows)


async def test_kasten_not_excluded(session_factory):
    with session_factory() as s:
        await sync_catalog(FakeDocExtractor(), s, DEFAULT_EXCLUDED_PRODUCTS, T0)
        kasten = s.scalars(select(CatalogProduct).where(
            CatalogProduct.name == "Veeam Kasten for Kubernetes")).one()
        one = s.scalars(select(CatalogProduct).where(CatalogProduct.name == "Veeam ONE")).one()
    assert kasten.excluded is False
    assert one.excluded is True
