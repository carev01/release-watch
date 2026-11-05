from release_watch.watch.compare import Known, compare, edit_diff, gate, page_suspect, readable_diff
from release_watch.watch.extract import body_hash
from release_watch.watch.readers import ReadResult
from release_watch.watch.split import Entry


def e(key, body):
    return Entry(key=key, heading=key, body=body)


def k(key, body):
    return Known(key=key, body_hash=body_hash(body), body=body)


def test_new_entry_detected():
    ch = compare([k("a", "## a\nold")], [e("b", "## b\nnew"), e("a", "## a\nold")])
    assert [x.key for x in ch.new] == ["b"] and ch.edited == [] and ch.unchanged == 1


def test_reordered_heading_same_body_not_new():
    ch = compare([k("oct 1", "same body")], [e("october 1", "same body")])
    assert ch.new == [] and ch.edited == [] and ch.removed == []
    assert [(entry.key, old.key) for entry, old in ch.rekeyed] == [("october 1", "oct 1")]


def test_edited_entry_detected_with_diff():
    ch = compare([k("a", "## a\n* one")], [e("a", "## a\n* one\n* two")])
    assert len(ch.edited) == 1
    entry, known = ch.edited[0]
    diff, ratio = edit_diff(known.body, entry.body)
    assert "+* two" in diff and 0 < ratio < 1


def test_edit_whitespace_or_punctuation_only_is_dropped():
    ch = compare([k("a", "## a\nFixed bug.")], [e("a", "## a\n\nFixed   bug")])
    assert ch.edited == [] and ch.new == []


def test_more_than_three_new_go_to_needs_look():
    four = compare([k("x", "x")], [e(str(i), f"b{i}") for i in range(4)] + [e("x", "x")])
    assert {(d[3], d[4]) for d in gate(four)} == {("needs_look", "too_many_new")}
    three = compare([k("x", "x")], [e(str(i), f"b{i}") for i in range(3)] + [e("x", "x")])
    assert {d[3] for d in gate(three)} == {"queued"}


def test_large_edit_needs_look():
    ch = compare([k("a", "## a\n" + "\n".join(f"line {i}" for i in range(10)))],
                 [e("a", "## a\n" + "\n".join(f"other {i}" for i in range(10)))])
    [(kind, _, known, status, reason)] = gate(ch)
    assert (kind, status, reason) == ("edited", "needs_look", "edit_too_large")
    assert known.key == "a"


def test_page_suspect_shrunk_and_no_entries():
    ok = ReadResult("ok", "", [e("a", "b")], size=1000)
    assert page_suspect(10_000, 5, ok) == "shrunk"
    assert page_suspect(1000, 5, ReadResult("ok", "", [], size=1000)) == "no_entries"
    assert page_suspect(1000, 5, ok) is None
    assert page_suspect(None, 0, ReadResult("ok", "", [], size=10)) is None
    assert page_suspect(1000, 5, ReadResult("suspect", "login_redirect")) == "login_redirect"


def test_second_entry_under_same_heading_is_new_not_edited():
    # Day 1: one 2026-10-01 entry. Day 2: the vendor adds another 2026-10-01 entry above it.
    known = [k("2026-10-01", "## 2026-10-01\nA")]
    fresh = [e("2026-10-01", "## 2026-10-01\nB"), e("2026-10-01 (2)", "## 2026-10-01\nA")]
    ch = compare(known, fresh)
    assert [x.body for x in ch.new] == ["## 2026-10-01\nB"]
    assert ch.edited == []
    assert [(entry.key, old.key) for entry, old in ch.rekeyed] == [("2026-10-01 (2)", "2026-10-01")]


def test_page_chrome_change_is_not_an_edit():
    old = "* Fix A.\n\n[PreviousModels](/a)[NextHome](/b)\n\nLast updated 20 days ago"
    new = "* Fix A.\n\n[PreviousModels](/a)[NextProduct Lifecycle](/c)\n\nLast updated 21 days ago"
    assert compare([k("v", old)], [e("v", new)]).edited == []


def test_readable_diff_marks_added_words_inline():
    old = "Early Access is available on the US West, US East, and Australia control planes."
    new = "Early Access is available on the US West, US East, Europe, Canada, and Australia control planes."
    assert readable_diff(old, new) == ("**Changed**\n- Early Access is available on the US West, US East, "
                                       "**Europe, Canada,** and Australia control planes.")


def test_readable_diff_shows_a_rewritten_line_with_what_it_was():
    old = "## Oct 23\n\n#### Singapore AWS and GCP data plane regions\n\nText."
    new = "## Oct 23\n\n#### Southeast Asia control plane\n\nText."
    assert readable_diff(old, new) == ("**Changed**\n- Southeast Asia control plane "
                                       "_(was: Singapore AWS and GCP data plane regions)_")


def test_readable_diff_marks_replaced_and_removed_words():
    old = "* Supports version 8.2.1 on Linux and AIX."
    new = "* Supports version 8.2.2 on Linux."
    assert readable_diff(old, new) == "**Changed**\n- Supports version **8.2.2** _(was: 8.2.1)_ on Linux " \
                                      "_(removed: and AIX)_."


def test_readable_diff_lists_added_and_removed_lines():
    old = "## 1.2\n\n* Fix A.\n* Fix B."
    new = "## 1.2\n\n* Fix A.\n* Fix C is new and unrelated to anything.\n* Fix D."
    assert readable_diff(old, "## 1.2\n\n* Fix A.\n* Fix B.\n* Fix D.") == "**Added**\n- Fix D."
    assert readable_diff(old, "## 1.2\n\n* Fix A.") == "**Removed**\n- Fix B."
    assert "**Added**" in readable_diff(old, new) or "**Changed**" in readable_diff(old, new)


def test_readable_diff_ignores_page_chrome():
    old = "* Fix A.\n\nLast updated 20 days ago"
    new = "* Fix A.\n* Fix B.\n\nLast updated 21 days ago"
    assert readable_diff(old, new) == "**Added**\n- Fix B."
