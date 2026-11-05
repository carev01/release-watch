import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from release_watch.coverage import compute_coverage
from release_watch.models import CatalogProduct, CatalogSource, CatalogVendor, Entry, WatchedSource
from release_watch.web.app import create_app

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def product(s, name, version=None, doc_url="https://docs.x.com/latest/guide", vendor="Acme"):
    vid = s.scalar(CatalogVendor.__table__.select().with_only_columns(CatalogVendor.de_id)
                   .where(CatalogVendor.name == vendor))
    if vid is None:
        vid = uuid.uuid4()
        s.add(CatalogVendor(de_id=vid, name=vendor, synced_at=NOW))
    pid = uuid.uuid4()
    s.add(CatalogProduct(de_id=pid, vendor_de_id=vid, name=name, de_version=version, synced_at=NOW))
    s.add(CatalogSource(de_id=uuid.uuid4(), product_de_id=pid, name="Guide", source_type="web", base_url=doc_url,
                        active=True, synced_at=NOW))
    s.flush()
    return pid


def source(s, pid, url, keys, follow=None, kind="entry_page", vendor="Acme"):
    src = WatchedSource(vendor_name=vendor, product_de_id=pid, url=url, url_key=url, kind=kind, state="active",
                        origin="seed", follow=follow)
    s.add(src)
    s.flush()
    for key in keys:
        s.add(Entry(source_id=src.id, key=key, heading=key, body=key, body_hash=key))
    return src


VERSIONS = {"pick": {"type": "versions", "regex": r"(\d+(?:\.\d+)+)"}}


def test_each_product_gets_a_verdict(session_factory):
    with session_factory() as s:
        product(s, "Evergreen")
        gap = product(s, "Backup", "19.0", "https://docs.x.com/Backup/19.0/admin")
        source(s, gap, "https://x.com/backup-rn", ["October 2026", "September 2026"])
        oadp = product(s, "OADP", "5.0", "https://docs.x.com/ocp/5.0/oadp")
        source(s, oadp, "https://x.com/lifecycle", ["1.6", "1.5"], VERSIONS, "index_page")
        rp = product(s, "Resiliency", "10.3", "https://docs.x.com/rp/10.3/x")
        source(s, rp, "https://x.com/rp", ["10.4", "10.3"], VERSIONS, "index_page")
        nb = product(s, "NetBackup", "11.2", "https://docs.x.com/nb/11.2/x")
        source(s, nb, "https://x.com/nb", ["11.2", "11.1", "9.1"], VERSIONS, "index_page")
        product(s, "NetWorker", None, "https://dl.x.com/manual1-dell-networker-19-14-administration-guide.pdf")
        velero = product(s, "Velero", "1.18", "https://velero.io/docs/v1.18/")
        source(s, velero, "https://x.com/velero.atom", ["v1.18.4", "v1.18.3"], kind="feed")
        s.commit()
        rows = {r.product: r for r in compute_coverage(s)}

    assert rows["Evergreen"].verdict == "evergreen"
    assert rows["Backup"].verdict == "no_signal"
    assert (rows["OADP"].verdict, rows["OADP"].signals[0].newest) == ("mismatch", "1.6")
    assert (rows["Resiliency"].verdict, rows["Resiliency"].newest) == ("behind", "10.4")
    assert (rows["NetBackup"].verdict, rows["NetBackup"].newest) == ("covered", "11.2")
    assert (rows["NetWorker"].verdict, rows["NetWorker"].url_version) == ("no_tracked_version", "19-14")
    assert (rows["Velero"].verdict, rows["Velero"].newest) == ("covered", "1.18.4")
    assert rows["Velero"].signals[0].how == "release-note titles"


def test_login_walled_and_inactive_sources_are_no_signal(session_factory):
    with session_factory() as s:
        pid = product(s, "Backup", "19.0", "https://docs.x.com/Backup/19.0/admin")
        source(s, pid, "https://x.com/v", ["19.1"], VERSIONS, "index_page").login_walled = True
        source(s, pid, "https://x.com/w", ["19.1"], VERSIONS, "index_page").state = "rejected"
        s.commit()
        [row] = compute_coverage(s)
    assert row.verdict == "no_signal"


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(session_factory))


def test_coverage_page_lists_gaps_first(client, session_factory):
    with session_factory() as s:
        product(s, "Evergreen")
        product(s, "Backup", "19.0", "https://docs.x.com/Backup/19.0/admin")
        s.commit()
    page = client.get("/coverage").text
    assert page.index("Backup") < page.index("Evergreen")
    assert "No watched page shows its versions" in page
