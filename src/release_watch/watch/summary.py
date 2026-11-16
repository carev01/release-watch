"""The one-line summary above each entry (spec 7.4). It never touches the verbatim text."""

import json

from release_watch.llm import LLMError

PROMPT_CHARS = 12_000
SYSTEM = """You summarise one vendor release-notes entry for a busy engineer.
Reply with JSON: {"summary": "<one sentence, at most 25 words, naming the most important changes>"}.
Use only facts in the entry. No marketing language."""


async def summarize(llm, vendor: str, heading: str, text: str) -> str | None:
    user = json.dumps({"vendor": vendor, "heading": heading, "entry": text[:PROMPT_CHARS]}, ensure_ascii=False)
    try:
        reply = await llm.complete_json(SYSTEM, user, max_output_tokens=1000)
    except LLMError:
        return None
    summary = reply.get("summary") if isinstance(reply, dict) else None
    return summary.strip() if isinstance(summary, str) and summary.strip() else None


JUDGE_SYSTEM = """You check one edit to a vendor's release-notes entry before it is sent to a team that keeps a
documentation index of these release notes. The edit is shown as a diff: changed lines with new words in bold and
the old ones after them as "(was: ...)", then added and removed lines.
Meaningful: the edit changes a fact a reader would act on: a feature, fix, known issue, deprecation, version or
build number, date, limit, requirement or instruction added, changed or removed.
Noise: only formatting, markup, link targets or anchors, tracking tokens, page navigation or widgets, reordering,
rewording and typo fixes that leave every fact the same, a page's own "last updated" or "uploaded" timestamp, and
headings or links that only organise the page ("Previous releases", "Past releases", a bare year) added or removed.
Judge only what the diff shows; when a release fact changes, it is meaningful even if the change is small.
Reply with JSON: {"meaningful": true or false, "reason": "<one short sentence saying what changed>"}."""


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
