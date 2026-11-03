"""Load hand-researched sources (seeds/sources.yaml). Seeds are candidates: the owner still reviews them."""

from pathlib import Path

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from release_watch.models import CatalogProduct, CatalogVendor, Entry, NewsSource, WatchedSource
from release_watch.urls import normalize_url, section_key
from release_watch.watch.rules import PICK_TYPES

WATCHED_KINDS = {"entry_page", "index_page", "feed", "section"}
NEWS_KINDS = {"feed", "listing", "github_releases"}


def _product_id(session: Session, section: str, index: int, entry: dict):
    vendor = session.scalars(select(CatalogVendor).where(CatalogVendor.name == entry.get("vendor"))).first()
    if vendor is None:
        raise ValueError(f"{section} entry {index}: unknown vendor {entry.get('vendor')!r}")
    if not entry.get("product"):
        return None
    product = session.scalars(select(CatalogProduct).where(
        CatalogProduct.vendor_de_id == vendor.de_id, CatalogProduct.name == entry["product"])).first()
    if product is None:
        raise ValueError(f"{section} entry {index}: unknown product {entry['product']!r} for {vendor.name}")
    return product.de_id


def load_seeds(session: Session, path: Path) -> dict[str, int]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    counts = {"watched_new": 0, "news_new": 0, "unchanged": 0, "skipped_rejected": 0, "configured": 0}

    for i, entry in enumerate(data.get("watched_sources") or []):
        product_id = _product_id(session, "watched_sources", i, entry)
        kind = entry.get("kind")
        if kind not in WATCHED_KINDS:
            raise ValueError(f"watched_sources entry {i}: kind must be one of {sorted(WATCHED_KINDS)}")
        if kind == "section" and not entry.get("selector"):
            raise ValueError(f"watched_sources entry {i}: kind 'section' needs a selector")
        key = section_key(entry["url"], entry["selector"]) if kind == "section" else normalize_url(entry["url"])
        if _existing(session, WatchedSource, key, counts):
            continue
        session.add(WatchedSource(
            vendor_name=entry["vendor"], product_de_id=product_id, url=entry["url"], url_key=key,
            title=entry.get("title"), kind=kind, selector=entry.get("selector"), state="candidate",
            origin="seed", discovery_reason="seed", notes=entry.get("notes"),
        ))
        session.flush()
        counts["watched_new"] += 1

    for i, entry in enumerate(data.get("news_sources") or []):
        product_id = _product_id(session, "news_sources", i, entry)
        if entry.get("kind") not in NEWS_KINDS:
            raise ValueError(f"news_sources entry {i}: kind must be one of {sorted(NEWS_KINDS)}")
        key = normalize_url(entry["url"])
        if _existing(session, NewsSource, key, counts):
            continue
        session.add(NewsSource(
            vendor_name=entry["vendor"], product_de_id=product_id, url=entry["url"], url_key=key,
            kind=entry["kind"], state="candidate", origin="seed", notes=entry.get("notes"),
        ))
        session.flush()
        counts["news_new"] += 1

    for i, entry in enumerate(data.get("source_config") or []):
        _apply_config(session, i, entry, counts)

    session.commit()
    return counts


CONFIG_FIELDS = ("selector", "split_rule", "follow", "kind", "login_walled")
RESHAPING_FIELDS = ("selector", "split_rule", "follow", "kind")


def _check_follow(index: int, follow: dict) -> None:
    while follow:
        if "pin" in follow and "pick" not in follow:
            return
        if follow.get("pick", {}).get("type") not in PICK_TYPES:
            raise ValueError(f"source_config entry {index}: follow pick.type must be one of {sorted(PICK_TYPES)}")
        follow = follow.get("then")


def _apply_config(session: Session, index: int, entry: dict, counts: dict[str, int]) -> None:
    """Per-source watch configuration (Task 12 tuning), applied to an existing source by URL."""
    key = entry.get("key") or normalize_url(entry["url"])
    row = session.scalars(select(WatchedSource).where(WatchedSource.url_key == key)).first()
    if row is None:
        raise ValueError(f"source_config entry {index}: no source with url {entry['url']!r}")
    if entry.get("kind") is not None and entry["kind"] not in WATCHED_KINDS:
        raise ValueError(f"source_config entry {index}: kind must be one of {sorted(WATCHED_KINDS)}")
    if entry.get("follow"):
        _check_follow(index, entry["follow"])
    if row.state == "rejected":
        return
    reshaped = False
    for name in CONFIG_FIELDS:
        if name in entry:
            if name in RESHAPING_FIELDS and getattr(row, name) != entry[name]:
                reshaped = True
            setattr(row, name, entry[name])
    if reshaped:  # new keys would read as new entries: start again from a fresh baseline
        session.execute(delete(Entry).where(Entry.source_id == row.id))
    counts["configured"] += 1


def _existing(session: Session, model, key: str, counts: dict[str, int]) -> bool:
    row = session.scalars(select(model).where(model.url_key == key)).first()
    if row is None:
        return False
    counts["skipped_rejected" if row.state == "rejected" else "unchanged"] += 1
    return True
