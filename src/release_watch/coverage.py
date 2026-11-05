"""Which DocExtractor products would we notice a new version of?

A product whose DocExtractor URLs are pinned to a version ("/19.0/", "networker-19-14") needs a watched
page that shows its versions: a version list, an index of per-version documents, or release notes titled
with versions. Evergreen docs ("/latest/", SaaS help) always show the current release and need none.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select

from release_watch.models import CatalogProduct, CatalogSource, CatalogVendor, Entry, WatchedSource
from release_watch.watch.rules import newer_than_tracked, same_version_line, version_tuple
from release_watch.watch.split import heading_version

# A version in a doc URL: "19.0", "v5.2.1", "19-14", "7_5", "AcronisCyberProtect_17".
_URL_VERSION = re.compile(r"(?<![\w.])v?(\d+(?:[._-]\d+)+)(?![\w.]*\d)|[A-Za-z]_(\d{1,2})(?=[/._])")

VERDICTS = {  # in the order the page lists them: what needs attention first
    "behind": "A newer version is out than DocExtractor tracks.",
    "no_signal": "No watched page shows its versions, so a new version would go unnoticed.",
    "mismatch": "The watched pages show version numbers from another scheme than DocExtractor's, so they "
                "can't be compared.",
    "no_tracked_version": "DocExtractor has no version for it, though its URLs name one. Set it in "
                          "DocExtractor so new versions can be compared.",
    "covered": "A watched page shows its versions.",
    "evergreen": "Its docs always show the current release; there is no version to follow.",
}


@dataclass
class Signal:
    how: str
    url: str
    newest: str | None


@dataclass
class ProductCoverage:
    vendor: str
    product: str
    tracked: str | None
    url_version: str | None
    verdict: str = "no_signal"
    newest: str | None = None
    signals: list[Signal] = field(default_factory=list)

    @property
    def explanation(self) -> str:
        return VERDICTS[self.verdict]


def _url_version(urls: list[str]) -> str | None:
    found = Counter()
    for url in urls:
        path = url.split("?")[0]
        for m in _URL_VERSION.finditer(path):
            found[m.group(1) or m.group(2)] += 1
    return found.most_common(1)[0][0] if found else None


def _signal(src: WatchedSource, keys_and_headings: list[tuple[str, str, str | None]]) -> Signal | None:
    follow = src.follow or {}
    if follow.get("pick", {}).get("type") == "versions":
        how, values = "version list", [key for key, _, _ in keys_and_headings]
    elif src.kind == "index_page" and follow.get("mode", "version") == "version":
        how = "index of versions"
        values = [v for _, heading, url in keys_and_headings
                  if (v := heading_version(heading) or heading_version(url or ""))]
    else:
        how = "release-note titles"
        values = [v for _, heading, _ in keys_and_headings if (v := heading_version(heading))]
    values = [v.lstrip("vV") for v in values if version_tuple(v)]
    if not values:
        return None
    return Signal(how, src.url, max(values, key=version_tuple))


def _newest_on_line(values: list[str], tracked: str) -> str | None:
    on_line = [v for v in values if same_version_line(v, tracked)]
    return max(on_line, key=version_tuple) if on_line else None


def compute_coverage(s) -> list[ProductCoverage]:
    vendors = {v.de_id: v.name for v in s.scalars(select(CatalogVendor).where(CatalogVendor.active))}
    products = [p for p in s.scalars(select(CatalogProduct).where(CatalogProduct.active,
                                                                    CatalogProduct.excluded.is_(False)))
                if p.vendor_de_id in vendors]
    doc_urls: dict = {}
    for cs in s.scalars(select(CatalogSource).where(CatalogSource.active)):
        doc_urls.setdefault(cs.product_de_id, []).append(cs.base_url)
    sources: dict = {}
    for src in s.scalars(select(WatchedSource).where(WatchedSource.state == "active",
                                                     WatchedSource.login_walled.is_(False))):
        sources.setdefault(src.product_de_id, []).append(src)
    entries: dict = {}
    ids = [src.id for rows in sources.values() for src in rows]
    for source_id, key, heading, url in s.execute(select(Entry.source_id, Entry.key, Entry.heading, Entry.url).where(
            Entry.source_id.in_(ids), Entry.removed_at.is_(None))):
        entries.setdefault(source_id, []).append((key, heading, url))

    out = []
    for p in products:
        row = ProductCoverage(vendors[p.vendor_de_id], p.name, p.de_version, _url_version(doc_urls.get(p.de_id, [])))
        row.signals = [sig for src in sources.get(p.de_id, []) if (sig := _signal(src, entries.get(src.id, [])))]
        if not (row.tracked or row.url_version):
            row.verdict = "evergreen"
        elif not row.tracked:
            row.verdict = "no_tracked_version"
        elif not row.signals:
            row.verdict = "no_signal"
        elif (newest := _newest_on_line([sig.newest for sig in row.signals], row.tracked)) is None:
            row.verdict = "mismatch"
        else:
            row.newest = newest
            row.verdict = "behind" if newer_than_tracked(newest, row.tracked) else "covered"
        out.append(row)
    order = list(VERDICTS)
    return sorted(out, key=lambda r: (order.index(r.verdict), r.vendor.casefold(), r.product.casefold()))
