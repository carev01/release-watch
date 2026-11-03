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


def test_no_selector_uses_main_content():
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


def test_main_content_preferred_over_trafilatura():
    html = ('<html><body><nav>Menu</nav><div role="main"><h1>Notes</h1><p>' + "Intro text. " * 20 + '</p>'
            '<h3><a name="v5"></a>Device Version 5.7.8 - Jul 23, 2026</h3><p>Fixed it.</p></div>'
            '<footer>Footer</footer></body></html>')
    md = extract_markdown(html, URL)
    assert "Device Version 5.7.8 - Jul 23, 2026" in md and "Fixed it." in md
    assert "Menu" not in md and "Footer" not in md


def test_falls_back_to_body_without_chrome():
    html = ("<html><body><nav>Menu</nav><div><h2>October 1, 2026</h2><p>" + "Text here. " * 40
            + "</p></div><footer>Foot</footer></body></html>")
    md = extract_markdown(html, URL)
    assert md.startswith("## October 1, 2026") and "Menu" not in md and "Foot" not in md
