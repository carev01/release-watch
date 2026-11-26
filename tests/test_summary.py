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


async def test_summarize_says_when_an_entry_has_nothing_in_it():
    from release_watch.watch.summary import summarize_entry
    empty = SummaryLLM(reply={"summary": "No release details are provided.", "substantive": False})
    assert await summarize_entry(empty, "Gearset", "November 25th, 2026", "## November 25th, 2026") == (
        "No release details are provided.", False)
    assert await summarize_entry(SummaryLLM(), "V", "h", "t") == ("Adds X.", True)  # no flag: assume content
    assert await summarize_entry(SummaryLLM(error=LLMError("x")), "V", "h", "t") == (None, True)


async def test_api_check_reads_capability_and_summary():
    from release_watch.watch.summary import judge_api
    llm = SummaryLLM(api={"capability": False, "summary": "Only renamed fields and removed deprecated types."})
    assert await judge_api(llm, "Rubrik", "November 16, 2026", "* `FlagAttribute` removed") == (
        False, "Only renamed fields and removed deprecated types.")
    assert await judge_api(SummaryLLM(), "V", "h", "t") == (True, "Backs up Y.")
    assert (await judge_api(SummaryLLM(api=LLMError("down")), "V", "h", "t"))[0] is None
    assert (await judge_api(SummaryLLM(api={"capability": "maybe"}), "V", "h", "t"))[0] is None
