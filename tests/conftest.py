"""Shared pytest fixtures.

DB tests need a disposable Postgres, e.g.:
  docker run -d --name rw-test-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw_test -p 55432:5432 postgres:16-alpine
  export RW_TEST_DATABASE_URL=postgresql+psycopg://postgres:rw@localhost:55432/rw_test
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from release_watch.db import Base, make_engine, make_session_factory

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def db_url():
    url = os.environ.get("RW_TEST_DATABASE_URL")
    if not url:
        pytest.skip("RW_TEST_DATABASE_URL not set; DB tests need a disposable Postgres")
    return url


@pytest.fixture(scope="session")
def migrated_engine(db_url):
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(migrated_engine):
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with migrated_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    return make_session_factory(migrated_engine)
