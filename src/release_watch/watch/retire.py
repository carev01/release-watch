"""Stop watching an older version's release notes once the newer version's page is watched.

Two active sources of the same vendor and product whose URLs differ only in a version path segment
(".../openshift_container_platform/4.22/html/..." and ".../5.0/html/...") are one page across versions.
When the newest has a baseline, the older ones are marked superseded by it, as a verified successor is.
"""

import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select

from release_watch.models import Entry, WatchedSource
from release_watch.watch.rules import version_tuple

# A path segment that is only a dotted version: "4.22", "8.2.1", "v5.0". A bare "v1" is too ambiguous.
_VERSION_SEGMENT = re.compile(r"^v?\d+(?:\.\d+)+$", re.I)


def _template(url_key: str) -> tuple[str, str] | None:
    """(the URL with its version segments blanked, the version) or None when the path names no version."""
    parts = urlsplit(url_key)
    segments = parts.path.split("/")
    versions = [seg for seg in segments if _VERSION_SEGMENT.match(seg)]
    if not versions:
        return None
    blanked = "/".join("{version}" if _VERSION_SEGMENT.match(seg) else seg for seg in segments)
    return urlunsplit(parts._replace(path=blanked)), ".".join(versions)


def retire_older_versions(s, now: datetime) -> int:
    groups: dict[tuple, list[tuple[str, WatchedSource]]] = {}
    for row in s.scalars(select(WatchedSource).where(WatchedSource.state == "active")):
        if found := _template(row.url_key):
            template, version = found
            groups.setdefault((row.vendor_name, row.product_de_id, template), []).append((version, row))
    retired = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        _, newest = max(rows, key=lambda r: version_tuple(r[0]))
        has_baseline = s.scalar(select(func.count()).select_from(Entry).where(Entry.source_id == newest.id))
        if not has_baseline:
            continue
        for _, row in rows:
            if row is not newest:
                row.state, row.superseded_by, row.decided_at = "superseded", newest.id, now
                retired += 1
    s.flush()
    return retired
