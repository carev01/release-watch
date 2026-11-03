"""Phase-1 tables. DocExtractor ids are plain references, never cross-stack foreign keys."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from release_watch.db import Base

SOURCE_KIND = Enum("entry_page", "index_page", "feed", "section", "unknown", name="source_kind")
SOURCE_STATE = Enum("candidate", "active", "rejected", "gone", name="source_state")
ORIGIN = Enum("heuristic", "seed", name="source_origin")
LABEL = Enum("release_notes", "not_release_notes", "unsure", "blocked", name="source_label")
NEWS_KIND = Enum("feed", "listing", "github_releases", name="news_kind")
JOB_STATUS = Enum("running", "ok", "failed", name="job_status")


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CatalogVendor(Timestamps, Base):
    __tablename__ = "catalog_vendor"
    de_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(Text)
    tier_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CatalogProduct(Timestamps, Base):
    __tablename__ = "catalog_product"
    de_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    vendor_de_id: Mapped[uuid.UUID] = mapped_column(UUID, index=True)
    name: Mapped[str] = mapped_column(String(255))
    de_version: Mapped[str | None] = mapped_column(String(64))
    de_previous_version: Mapped[str | None] = mapped_column(String(64))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CatalogSource(Timestamps, Base):
    __tablename__ = "catalog_source"
    de_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    product_de_id: Mapped[uuid.UUID] = mapped_column(UUID, index=True)
    name: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(16))
    base_url: Mapped[str] = mapped_column(Text)
    url_template: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(String(64))
    auth_realm_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    status: Mapped[str | None] = mapped_column(String(32))
    last_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WatchedSource(Timestamps, Base):
    __tablename__ = "watched_source"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    vendor_name: Mapped[str] = mapped_column(String(255), index=True)
    product_de_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    de_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    de_article_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    url: Mapped[str] = mapped_column(Text)
    url_key: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(SOURCE_KIND, default="unknown")
    state: Mapped[str] = mapped_column(SOURCE_STATE, default="candidate", index=True)
    origin: Mapped[str] = mapped_column(ORIGIN)
    discovery_reason: Mapped[str | None] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(LABEL)
    label_reason: Mapped[str | None] = mapped_column(String(64))
    label_evidence: Mapped[str | None] = mapped_column(Text)
    labelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    login_walled: Mapped[bool] = mapped_column(Boolean, default=False)
    selector: Mapped[str | None] = mapped_column(Text)
    split_rule: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_watched_source_article", "de_article_id", unique=True,
            postgresql_where=text("de_article_id IS NOT NULL"),
        ),
    )


class NewsSource(Timestamps, Base):
    __tablename__ = "news_source"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    vendor_name: Mapped[str] = mapped_column(String(255), index=True)
    product_de_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    url: Mapped[str] = mapped_column(Text)
    url_key: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(NEWS_KIND)
    state: Mapped[str] = mapped_column(SOURCE_STATE, default="candidate", index=True)
    origin: Mapped[str] = mapped_column(ORIGIN)
    notes: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRun(Base):
    __tablename__ = "job_run"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    job: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(JOB_STATUS, default="running")
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
