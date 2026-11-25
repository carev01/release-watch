"""Adaptive Cards rendered as HTML, so the Notifications page shows what Teams would show."""

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

# CommonMark with raw HTML off: card text is vendor text, never markup. markdown-it drops javascript: links.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def _block(block: dict) -> str:
    text = block.get("text", "")
    if block.get("fontType") == "Monospace":
        return f'<pre class="verbatim">{escape(text)}</pre>'
    classes = ["tb"] + (["subtle"] if block.get("isSubtle") else []) + (["big"] if block.get("size") else [])
    return f'<div class="{" ".join(classes)}">{_md.render(text)}</div>'


def markdown_html(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def _element(block: dict, toggles: dict[str, str]) -> str:
    if block.get("type") == "TextBlock":
        return _block(block)
    if block.get("type") == "Container":  # folded notes: Teams' toggle button ("Show full notes")
        inner = "".join(_element(b, toggles) for b in block.get("items", []))
        label = escape(toggles.get(block.get("id"), "Show full notes"))
        return f"<details><summary>{label}</summary>{inner}</details>" if block.get("isVisible") is False \
            else inner
    return ""


def card_html(card: dict) -> Markup:
    toggles = {target: a.get("title", "") for a in card.get("actions", [])
               if a.get("type") == "Action.ToggleVisibility" for target in a.get("targetElements", [])}
    body = "".join(_element(b, toggles) for b in card.get("body", []))
    actions = "".join(
        f'<a class="action" href="{escape(a["url"])}" rel="noreferrer">{escape(a.get("title", "Open"))}</a>'
        for a in card.get("actions", []) if str(a.get("url", "")).startswith(("http://", "https://")))
    return Markup(f'<div class="card">{body}{f"<div class=actions>{actions}</div>" if actions else ""}</div>')
