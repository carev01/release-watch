"""Daily mirror of DocExtractor's vendors, products and sources."""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from release_watch.models import CatalogProduct, CatalogSource, CatalogVendor


def _vendor_row(v: dict) -> dict:
    return {"de_id": v["id"], "name": v["name"], "website": v.get("website"), "tier_id": v.get("tier_id")}


def _product_row(p: dict, excluded: set[str]) -> dict:
    return {
        "de_id": p["id"], "vendor_de_id": p["vendor_id"], "name": p["name"],
        "de_version": p.get("version"), "de_previous_version": p.get("previous_version"),
        "excluded": p["name"] in excluded,
    }


def _source_row(s: dict) -> dict:
    keys = ("name", "source_type", "base_url", "url_template", "platform", "auth_realm_id", "status",
            "last_extracted_at")
    return {"de_id": s["id"], "product_de_id": s["product_id"], **{k: s.get(k) for k in keys}}


async def sync_catalog(client, session: Session, excluded_products: list[str], now: datetime) -> dict[str, int]:
    # Fetch everything before touching the DB: a failed call must not deactivate anything.
    vendors = await client.list_vendors()
    products = await client.list_products()
    sources = await client.list_sources()

    excluded = set(excluded_products)
    deactivated = 0
    for model, rows in (
        (CatalogVendor, [_vendor_row(v) for v in vendors]),
        (CatalogProduct, [_product_row(p, excluded) for p in products]),
        (CatalogSource, [_source_row(s) for s in sources]),
    ):
        if rows:
            rows = [{**r, "active": True, "synced_at": now} for r in rows]
            stmt = insert(model).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["de_id"],
                set_={c: stmt.excluded[c] for c in rows[0] if c != "de_id"},
            )
            session.execute(stmt)
        result = session.execute(
            update(model).where(model.active.is_(True), model.synced_at != now).values(active=False)
        )
        deactivated += result.rowcount
    session.commit()
    return {
        "vendors": len(vendors),
        "products": len(products),
        "sources": len(sources),
        "deactivated": deactivated,
        "excluded": sum(1 for p in products if p["name"] in excluded),
    }
