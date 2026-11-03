from release_watch.llm import LLMError
from release_watch.watch.summary import summarize
from tests.fakes import SummaryLLM


async def test_summarize_returns_sentence():
    assert await summarize(SummaryLLM(), "V", "Oct 2026", "text") == "Adds X."


async def test_summarize_none_on_error():
    assert await summarize(SummaryLLM(error=LLMError("x")), "V", "h", "t") is None
    assert await summarize(SummaryLLM(reply={"nope": 1}), "V", "h", "t") is None
