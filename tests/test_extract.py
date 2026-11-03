from pathlib import Path

from release_watch.watch.extract import body_hash, extract_markdown, html_to_markdown, normalize, punct_insensitive

PAGES = Path(__file__).parent / "fixtures" / "pages"
URL = "https://x.com/rn"


def test_selector_extracts_only_that_node():
    html = '<nav>Menu stuff</nav><div id="c"><h2>2026-10-01</h2><p>A</p></div>'
    md = extract_markdown(html, URL, "#c")
    assert md.startswith("## 2026-10-01")
    assert "A" in md and "Menu" not in md


def test_missing_selector_returns_empty():
    assert extract_markdown("<p>x</p>", URL, "#nope") == ""


def test_no_selector_uses_trafilatura():
    md = extract_markdown((PAGES / "changelog.html").read_text(), URL)
    assert "# October 28, 2026" in md
    assert "Pricing" not in md


def test_html_to_markdown_keeps_lists():
    assert "* one" in html_to_markdown("<ul><li>one</li></ul>") or "- one" in html_to_markdown("<ul><li>one</li></ul>")


def test_normalize_strips_tokens_and_noise():
    a = "## 2026-10-01\n\n![x](https://cdn.x.com/i.png?sig=abc&se=1)\nNew thing.\nLast updated: Oct 3"
    b = "## 2026-10-01\n![x](https://cdn.x.com/i.png?sig=def&se=2)\n\nNew   thing.\nLast updated: Oct 9"
    assert body_hash(a) == body_hash(b)
    assert "sig=" not in normalize(a)


def test_punct_insensitive_ignores_punctuation():
    assert punct_insensitive("Fixed bug.") == punct_insensitive("Fixed  bug")
    assert punct_insensitive("Fixed bug") != punct_insensitive("Fixed bugs")
