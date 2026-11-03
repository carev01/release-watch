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
