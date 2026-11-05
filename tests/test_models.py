import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from release_watch.models import Entry, EntryEvent, WatchedSource
from tests.conftest import ROOT

TABLES = {
    "catalog_vendor", "catalog_product", "catalog_source",
    "watched_source", "news_source", "job_run",
    "fetch_attempt", "snapshot", "entry", "entry_event",
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


def _saved_source(s, url_key="https://x.com/rn"):
    src = _source(url_key)
    s.add(src)
    s.commit()
    return src


def test_entry_key_unique_per_source(session_factory):
    with session_factory() as s:
        src = _saved_source(s)
        for _ in range(2):
            s.add(Entry(source_id=src.id, key="2026-10-01", heading="2026-10-01", body="b", body_hash="h"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_event_dedupe_key_unique(session_factory):
    with session_factory() as s:
        src = _saved_source(s)
        s.add(EntryEvent(source_id=src.id, kind="version", heading="x", status="queued", dedupe_key=None))
        s.add(EntryEvent(source_id=src.id, kind="version", heading="y", status="queued", dedupe_key=None))
        s.commit()
        s.add(EntryEvent(source_id=src.id, kind="version", heading="a", status="queued", dedupe_key="v:1"))
        s.add(EntryEvent(source_id=src.id, kind="version", heading="b", status="queued", dedupe_key="v:1"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_migration_marks_login_walled(session_factory, migrated_engine, db_url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.downgrade(cfg, "0001")
    try:
        with migrated_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO watched_source (id, vendor_name, url, url_key, kind, state, origin, label, label_reason,"
                " login_walled) VALUES (gen_random_uuid(), 'AvePoint', 'https://a/x', 'https://a/x', 'entry_page',"
                " 'active', 'heuristic', 'blocked', 'login_redirect', false),"
                " (gen_random_uuid(), 'Red Hat', 'https://r/x', 'https://r/x', 'entry_page', 'active', 'heuristic',"
                " 'blocked', 'forbidden', false)"))
    finally:
        command.upgrade(cfg, "head")
    with migrated_engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT vendor_name, login_walled FROM watched_source")).all())
    assert rows == {"AvePoint": True, "Red Hat": False}
