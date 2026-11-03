"""Propose release-notes candidates from DocExtractor's catalog (briefing: "Finding release-notes pages").

DocExtractor's full-text search is not used: it takes ~100 s per page and shares a database with the crawler.
Matching TOC titles *and* URLs finds the same pages, since search hits matching only in the body are noise.
"""

import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from release_watch.models import CatalogProduct, CatalogSource, CatalogVendor, WatchedSource
from release_watch.docextractor import DocExtractorError
from release_watch.urls import normalize_url

log = logging.getLogger(__name__)

RELEASE_PATTERN = re.compile(
    r"release[ _-]?notes?|what[’'`]?[ _-]?s[ _-]?new|new[ _-]features|change[ _-]?log|revision[ _-]history"
    r"|(?<![a-z])readme(?![a-z])"
    r"|product[ _-]updates",
    re.IGNORECASE,
)


@dataclass
class Proposal:
    vendor_name: str
    product_de_id: str | None
    de_source_id: str | None
    de_article_id: str | None
    url: str
    title: str | None
    kind_hint: str
    reason: str


def _watchable_sources(session: Session) -> dict[str, tuple[CatalogSource, CatalogProduct, str]]:
    rows = session.execute(
        select(CatalogSource, CatalogProduct, CatalogVendor.name)
        .join(CatalogProduct, CatalogProduct.de_id == CatalogSource.product_de_id)
        .join(CatalogVendor, CatalogVendor.de_id == CatalogProduct.vendor_de_id)
        .where(CatalogSource.active.is_(True), CatalogProduct.active.is_(True),
               CatalogProduct.excluded.is_(False), CatalogVendor.active.is_(True))
    ).all()
    return {str(src.de_id): (src, prod, vendor) for src, prod, vendor in rows}


def _walk(entries: list[dict]):
    for entry in entries:
        yield entry
        yield from _walk(entry.get("children") or [])


def _first_url(entry: dict) -> str | None:
    if entry.get("url"):
        return entry["url"]
    for child in _walk(entry.get("children") or []):
        if child.get("url"):
            return child["url"]
    return None


def _toc_matches(entries: list[dict]):
    """Matching TOC entries; a matching branch is returned once and its children are not searched."""
    for entry in entries:
        if RELEASE_PATTERN.search(entry.get("title") or "") or RELEASE_PATTERN.search(entry.get("url") or ""):
            yield entry
        else:
            yield from _toc_matches(entry.get("children") or [])


async def _toc(client, source_id: str, errors: list[str]) -> list[dict]:
    try:
        return await client.get_toc(source_id)
    except DocExtractorError as exc:
        log.warning("toc skipped for source %s: %s", source_id, exc)
        errors.append(source_id)
        return []


async def propose_candidates(client, session: Session) -> tuple[list[Proposal], int]:
    """Proposals, plus how many sources were skipped because their TOC couldn't be read."""
    sources = _watchable_sources(session)
    toc_errors: list[str] = []
    proposals: dict[str, Proposal] = {}
    covered: set[str] = set()

    def add(p: Proposal) -> None:
        proposals.setdefault(normalize_url(p.url), p)
        if p.de_source_id:
            covered.add(p.de_source_id)

    # Pass 1: whole sources whose name says release notes.
    for sid, (src, prod, vendor) in sources.items():
        if not RELEASE_PATTERN.search(src.name):
            continue
        kind = "index_page" if src.source_type == "pdf" else "entry_page"
        if src.source_type == "web":
            toc = await _toc(client, sid, toc_errors)
            if sum(1 for e in _walk(toc) if e.get("url")) > 1:
                kind = "index_page"
        add(Proposal(vendor, str(prod.de_id), sid, None, src.base_url, src.name, kind, "source_name"))

    # Pass 2: TOC branches or pages whose title or URL says release notes, inside other web sources.
    for sid, (src, prod, vendor) in sources.items():
        if sid in covered or src.source_type != "web":
            continue
        for entry in _toc_matches(await _toc(client, sid, toc_errors)):
            url = _first_url(entry)
            if not url:
                continue
            if entry.get("children"):
                add(Proposal(vendor, str(prod.de_id), sid, None, url, entry["title"], "index_page", "toc"))
            else:
                add(Proposal(vendor, str(prod.de_id), sid, entry.get("article_id"), url, entry["title"],
                             "entry_page", "toc"))

    return list(proposals.values()), len(toc_errors)


def _same_proposal(session: Session, p: Proposal, ambiguous: set) -> WatchedSource | None:
    """The row this proposal already is, matched on identity that survives URL changes."""
    q = select(WatchedSource)
    if p.de_article_id:
        return session.scalars(q.where(WatchedSource.de_article_id == uuid.UUID(p.de_article_id))).first()
    if not p.de_source_id or (p.de_source_id, p.reason, p.title) in ambiguous:
        return None
    # Whole sources move on version bumps; index branches change URL when a newer release is added on top.
    q = q.where(WatchedSource.de_source_id == uuid.UUID(p.de_source_id),
                WatchedSource.discovery_reason == p.reason, WatchedSource.de_article_id.is_(None))
    if p.reason == "toc":
        q = q.where(WatchedSource.title == p.title)
    rows = session.scalars(q.limit(2)).all()
    return rows[0] if len(rows) == 1 else None


def upsert_proposals(session: Session, proposals: list[Proposal]) -> dict[str, int]:
    counts = {"new": 0, "existing": 0, "moved": 0, "skipped_rejected": 0, "conflict": 0}
    walled = {str(s.de_id) for s in session.scalars(
        select(CatalogSource).where(CatalogSource.auth_realm_id.is_not(None)))}
    # Two TOC branches titled alike in one source can't be told apart by title; match those by URL only.
    seen = Counter((p.de_source_id, p.reason, p.title) for p in proposals if not p.de_article_id)
    ambiguous = {k for k, n in seen.items() if n > 1}
    for p in proposals:
        key = normalize_url(p.url)
        at_key = session.scalars(select(WatchedSource).where(WatchedSource.url_key == key)).first()
        row = _same_proposal(session, p, ambiguous)
        if row and row.url_key != key:
            if at_key:  # another row already owns the new URL; leave both as they are
                counts["conflict"] += 1
                continue
            row.url, row.url_key = p.url, key
            session.flush()
            counts["moved"] += 1
            continue
        row = row or at_key
        if row:
            counts["skipped_rejected" if row.state == "rejected" else "existing"] += 1
            continue
        session.add(WatchedSource(
            vendor_name=p.vendor_name, product_de_id=p.product_de_id, de_source_id=p.de_source_id,
            de_article_id=p.de_article_id, url=p.url, url_key=key, title=p.title, kind=p.kind_hint,
            state="candidate", origin="heuristic", discovery_reason=p.reason,
            login_walled=p.de_source_id in walled,
        ))
        session.flush()
        counts["new"] += 1
    session.commit()
    return counts
