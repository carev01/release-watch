from release_watch.llm import LLMError
from release_watch.watch.summary import summarize
from tests.fakes import SummaryLLM


async def test_summarize_returns_sentence():
    assert await summarize(SummaryLLM(), "V", "Oct 2026", "text") == "Adds X."


async def test_summarize_none_on_error():
    assert await summarize(SummaryLLM(error=LLMError("x")), "V", "h", "t") is None
    assert await summarize(SummaryLLM(reply={"nope": 1}), "V", "h", "t") is None


async def test_edit_judge_reads_the_verdict_and_its_reason():
    from release_watch.watch.summary import judge_edit
    noise = SummaryLLM(verdict={"meaningful": False, "reason": "Only link anchors were renumbered."})
    assert await judge_edit(noise, "Kasten", "8.0.11", "**Changed**\n- Bug Fixes (#bug-fixes-27)") == (
        False, "Only link anchors were renumbered.")
    assert await judge_edit(SummaryLLM(), "V", "h", "d") == (True, "Adds a feature.")


async def test_edit_judge_without_a_clear_answer_says_so():
    from release_watch.watch.summary import judge_edit
    assert (await judge_edit(SummaryLLM(verdict=LLMError("down")), "V", "h", "d"))[0] is None
    assert (await judge_edit(SummaryLLM(verdict={"meaningful": "maybe"}), "V", "h", "d"))[0] is None
