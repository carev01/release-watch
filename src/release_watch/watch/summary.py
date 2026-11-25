"""The one-line summary above each entry (spec 7.4). It never touches the verbatim text."""

import json

from release_watch.llm import LLMError

PROMPT_CHARS = 12_000
SYSTEM = """You summarise one vendor release-notes entry for someone who uses the product. The summary is the main
text of a chat card; the entry itself sits behind a "Show full notes" button.
Reply with JSON: {"summary": "<two or three short sentences, at most 60 words>"}.
Say what changes for the product's users, most important first: new or changed capabilities, licensing, pricing or
usage terms, supported platforms and requirements, deprecations and end of support, security fixes, and only then
notable bug fixes. Name the features. Don't repeat the vendor, product, heading or version number, and don't list
section names ("adds new features and bug fixes"): say what they are.
Use only facts in the entry. No marketing language."""


async def summarize(llm, vendor: str, heading: str, text: str) -> str | None:
    user = json.dumps({"vendor": vendor, "heading": heading, "entry": text[:PROMPT_CHARS]}, ensure_ascii=False)
    try:
        reply = await llm.complete_json(SYSTEM, user, max_output_tokens=1000)
    except LLMError:
        return None
    summary = reply.get("summary") if isinstance(reply, dict) else None
    return summary.strip() if isinstance(summary, str) and summary.strip() else None


JUDGE_SYSTEM = """You check one edit to a vendor's release-notes entry before it is sent to people who use the product.
The edit is shown as a diff: changed lines with new words in bold and the old ones after them as "(was: ...)", then
added and removed lines. Only edits with real impact on the product's users are sent; everything else is noise.
Meaningful, only when the edit adds, changes or removes one of these:
- a product capability: a feature added or removed, or behaving differently;
- licensing, metering, pricing or subscription terms, usage or support terms, deprecation or end of support;
- a release or availability date;
- supported platforms, versions or system requirements;
- a security issue or a data-loss risk that the reader must act on (an upgrade, a workaround).
Noise: everything else, including bug fixes and known issues added to or changed in an entry (unless they are the
security or data-loss kind above), renamed features, menus or labels, documentation and contribution instructions,
links and anchors, section names or a table of contents without the facts in them, formatting, reordering,
rewording and typo fixes, a page's own "last updated" timestamp, and headings that only organise the page.
Judge only what the diff shows, not the rest of the entry. When in doubt, it is noise.
Reply with JSON: {"meaningful": true or false, "reason": "<one or two short sentences saying only what the edit
changed, e.g. 'Commercial availability moved from 6 to 13 December 2026.'; never a summary of the whole entry>"}."""


async def judge_edit(llm, vendor: str, heading: str, diff: str) -> tuple[bool | None, str]:
    """(meaningful?, the model's reason); None when the model gave no clear answer."""
    user = json.dumps({"vendor": vendor, "heading": heading, "diff": diff[:PROMPT_CHARS]}, ensure_ascii=False)
    try:
        reply = await llm.complete_json(JUDGE_SYSTEM, user, max_output_tokens=1000)
    except LLMError:
        return None, ""
    verdict = reply.get("meaningful") if isinstance(reply, dict) else None
    reason = reply.get("reason") if isinstance(reply, dict) else None
    return (verdict if isinstance(verdict, bool) else None), (reason.strip() if isinstance(reason, str) else "")
