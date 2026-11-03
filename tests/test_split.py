from pathlib import Path

from release_watch.watch.split import entry_key, split_entries

PAGES = Path(__file__).parent / "fixtures" / "pages"


def md(name):
    return (PAGES / name).read_text()


def test_splits_on_date_headings():
    entries = split_entries(md("rolling.md"))
    assert [e.key for e in entries] == ["october 28, 2026", "october 14, 2026", "september 30, 2026"]
    first = entries[0]
    assert first.heading == "October 28, 2026"
    assert first.body.startswith("## October 28, 2026")
    assert "### Fixes" in first.body and "Restore was slow." in first.body
    assert "October 14" not in first.body
    assert "Intro text" not in "".join(e.body for e in entries)


def test_prefers_level_with_most_dated_headings():
    entries = split_entries(md("versions.md"))
    assert [e.key for e in entries] == ["11.48", "11.46", "11.44", "known issues"]


def test_bold_line_dates_split():
    entries = split_entries(md("bold_dates.md"))
    assert [e.key for e in entries] == ["2026-10-01", "2026-09-15"]
    assert "One." in entries[0].body and "Two." not in entries[0].body


def test_no_dated_headings_gives_zero_entries():
    assert split_entries("# Guide\n\n## Install\n\ntext\n\n## Configure\n\nmore") == []


def test_duplicate_keys_get_stable_suffixes():
    text = "## 2026-10-01\n\na\n\n## 2026-10-01\n\nb\n\n## 2026-09-01\n\nc"
    assert [e.key for e in split_entries(text)] == ["2026-10-01", "2026-10-01 (2)", "2026-09-01"]


def test_whole_rule_returns_one_entry():
    entries = split_entries("# Anything\n\ntext", {"whole": True})
    assert len(entries) == 1 and entries[0].key == "page" and entries[0].body == "# Anything\n\ntext"


def test_heading_level_override():
    entries = split_entries(md("versions.md"), {"heading_level": 2})
    assert [e.key for e in entries] == ["what's new in 2026"]


def test_heading_regex_override():
    entries = split_entries(md("versions.md"), {"heading_regex": r"^\d+\.\d+$"})
    assert [e.key for e in entries] == ["11.48", "11.46", "11.44"]


def test_entry_key_strips_markup():
    assert entry_key("**Oct 2026**") == "oct 2026"


def test_heading_text_drops_link_and_image_markup():
    text = "### [](#v5)Device Version 5.4.5 - Jan 29, 2026 [![](copy.png)](#v5)\n\nBody"
    [entry] = split_entries(text)
    assert entry.heading == "Device Version 5.4.5 - Jan 29, 2026"
    assert entry.key == "device version 5.4.5 - jan 29, 2026"
    assert entry.body.startswith("### [](#v5)Device")
