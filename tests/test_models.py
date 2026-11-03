import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from release_watch.models import WatchedSource

TABLES = {
    "catalog_vendor", "catalog_product", "catalog_source",
    "watched_source", "news_source", "job_run",
}


def test_upgrade_creates_tables(session_factory, migrated_engine):
    assert TABLES <= set(inspect(migrated_engine).get_table_names())


def _source(url_key):
    return WatchedSource(vendor_name="Zerto", url="https://x.com/rn", url_key=url_key,
                         title="RN", kind="entry_page", state="candidate", origin="heuristic")


def test_url_key_unique(session_factory):
    with session_factory() as s:
        s.add(_source("https://x.com/rn"))
        s.commit()
        s.add(_source("https://x.com/rn"))
        with pytest.raises(IntegrityError):
            s.commit()
