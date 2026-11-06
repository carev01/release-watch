from pathlib import Path

from release_watch.watch.split import entry_key, own_release, split_entries

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


def test_heading_regex_to_next_match_keeps_same_level_sections_in_the_entry():
    # Securiti Integrations: each "## 1.153" has its own "## Monitored Data Systems" and "## IaaS Connectors".
    page = ("## 1.153\n\n## Monitored Data Systems\n\n* New connector.\n\n## IaaS Connectors\n\n* Fix.\n\n"
            "## 1.152\n\n## Monitored Data Systems\n\n* Older.\n")
    entries = split_entries(page, {"heading_regex": r"^\d+\.\d+$", "to_next_match": True})
    assert [e.key for e in entries] == ["1.153", "1.152"]
    assert "New connector." in entries[0].body and "Fix." in entries[0].body and "Older." not in entries[0].body
    assert entries[1].body.endswith("* Older.")


def test_entry_key_strips_markup():
    assert entry_key("**Oct 2026**") == "oct 2026"


def test_heading_text_drops_link_and_image_markup():
    text = "### [](#v5)Device Version 5.4.5 - Jan 29, 2026 [![](copy.png)](#v5)\n\nBody"
    [entry] = split_entries(text)
    assert entry.heading == "Device Version 5.4.5 - Jan 29, 2026"
    assert entry.key == "device version 5.4.5 - jan 29, 2026"
    assert entry.body.startswith("### [](#v5)Device")


def test_heading_text_drops_icon_glyphs_and_bold():
    text = "## **5.4.1**\n\nA\n\n## Bacula Enterprise 18.2.5\n\nB"
    entries = split_entries(text)
    assert [e.heading for e in entries] == ["5.4.1", "Bacula Enterprise 18.2.5"]


def test_copy_link_widget_text_is_not_part_of_the_heading():
    md = "## 5.2.1.1. OADP 1.6.1 release notes Copy linkLink copied to clipboard!\n\nFixed things in 1.6.1.\n"
    [e] = split_entries(md, {"heading_level": 2})
    assert e.heading == "5.2.1.1. OADP 1.6.1 release notes"


def test_line_regex_splits_on_dropdown_date_lines():
    md = """# Release notes
## Current release
[![Closed](../x.gif)September 30, 2026](#)
## Graph migration complete
Text A.
## Previous releases
## 2026
[![Closed](../x.gif)September 22, 2026](#)
## Enhancements
Text B.
"""
    entries = split_entries(md, {"line_regex": r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}, \d{4}$"})
    assert [e.heading for e in entries] == ["September 30, 2026", "September 22, 2026"]
    assert "Text A." in entries[0].body and "Text B." not in entries[0].body
    assert entries[1].body.endswith("Text B.")


def test_heading_version_skips_chapter_numbers():
    from release_watch.watch.split import heading_version
    assert heading_version("5.2.1.1. OADP 1.6.2 release notes") == "1.6.2"
    assert heading_version("5.13.1.2. Install the Data Protection Application") is None
    assert heading_version("10.4 release notes") == "10.4" and heading_version("v1.19.0") == "v1.19.0"


def test_own_release_drops_the_older_releases_a_cumulative_page_repeats():
    # Atempo's "Lina 7.4.2" page carries 7.4.1's and 7.4.0's notes under it.
    page = ("# Lina 7.4.2\n\n## Fixes and improvements\n\n* Agent fix.\n\n## Lina 7.4.1\n\n* Older fix.\n\n"
            "### Lina 7.4.0\n\n* GA notes.")
    assert own_release(page, "Lina 7.4.2") == "# Lina 7.4.2\n\n## Fixes and improvements\n\n* Agent fix."


def test_own_release_keeps_headings_that_only_mention_a_version():
    page = "# Lina 7.4.2\n\n## Fixed since 7.4.0\n\n* Fix.\n\n## Upgrading from Lina 7.3\n\n* Steps."
    assert own_release(page, "Lina 7.4.2") == page
    assert own_release(page, "October 2026") == page  # a heading without a version: nothing to compare
