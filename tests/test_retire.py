import uuid
from datetime import UTC, datetime

from release_watch.models import Entry, WatchedSource
from release_watch.watch.retire import retire_older_versions

NOW = datetime(2026, 11, 5, tzinfo=UTC)
OCP = "https://docs.redhat.com/en/documentation/openshift_container_platform/{}/html/backup_and_restore/oadp"


def add(s, url, product=None, baseline=True, vendor="Red Hat"):
    src = WatchedSource(vendor_name=vendor, product_de_id=product, url=url, url_key=url, kind="entry_page",
                        state="active", origin="seed")
    s.add(src)
    s.flush()
    if baseline:
        s.add(Entry(source_id=src.id, key="k", heading="k", body="b", body_hash="h"))
    return src


def test_older_version_page_is_retired_once_the_newer_one_has_a_baseline(session_factory):
    pid = uuid.uuid4()
    with session_factory() as s:
        old, mid, new = (add(s, OCP.format(v), pid) for v in ("4.21", "4.22", "5.0"))
        assert retire_older_versions(s, NOW) == 2
        assert (old.state, old.superseded_by, mid.state, mid.superseded_by) == ("superseded", new.id,
                                                                                 "superseded", new.id)
        assert new.state == "active"


def test_nothing_retired_before_the_newer_page_has_a_baseline(session_factory):
    with session_factory() as s:
        old, new = add(s, OCP.format("4.22")), add(s, OCP.format("5.0"), baseline=False)
        assert retire_older_versions(s, NOW) == 0
        assert old.state == "active"


def test_other_pages_products_and_vendors_are_left_alone(session_factory):
    with session_factory() as s:
        rows = [add(s, OCP.format("4.22")), add(s, OCP.format("5.0").replace("/oadp", "/velero")),
                add(s, OCP.format("4.21"), product=uuid.uuid4()), add(s, OCP.format("4.20"), vendor="IBM"),
                add(s, "https://x.com/v1/rn"), add(s, "https://x.com/v2/rn")]
        assert retire_older_versions(s, NOW) == 0
        assert {r.state for r in rows} == {"active"}
