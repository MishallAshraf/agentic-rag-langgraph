"""Step 14 - Tests for the research team's orchestration and permissions.

    python -m unittest tests.test_research -v

A scripted stand-in replaces the LLM (it returns prepared plans, findings and
reviews) and search is replaced too, so these tests check the control flow and
the permission rules exactly, for free.
"""

import asyncio
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from engine import step14_research as s14  # noqa: E402
from engine.step14_research import Finding, Plan, Review, Task  # noqa: E402


class ScriptedLLM:
    """Plays the planner, researchers, reviewer and writer from a script, and records every call."""

    def __init__(self, plan: Plan, reviews: list[Review], answers: list[str]):
        self.plan, self.reviews, self.answers = plan, list(reviews), list(answers)
        self.calls: list[str] = []
        self.seen: list[str] = []  # what the researchers were shown

    def with_structured_output(self, schema, method=None):
        llm = self

        class Structured:
            async def ainvoke(self, messages):
                llm.calls.append(schema.__name__)
                if schema is Plan:
                    return llm.plan
                if schema is Finding:
                    llm.seen.append(messages[-1].content)
                    return Finding(answered=True, summary="A finding (doc.md, section \"A\").")
                return llm.reviews.pop(0)

        return Structured()

    async def ainvoke(self, messages):
        self.calls.append("write")
        return AIMessage(self.answers.pop(0), id=str(uuid.uuid4()))


def fake_search(collection, subject, question, query):
    return f'[1] {collection}.md, section "{question[:20]}"\nSome passage text.'


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s14.search = fake_search  # no index, no model
        bots = s14.all_bots()
        cls.bot = bots["research_assistant"]
        cls.sources = s14.research_sources(cls.bot, bots)

    def run_turn(self, llm, question="Q?", allowed_sources=None, messages=None):
        graph = s14.build_research_graph(self.bot, self.sources, llm)
        config = {"configurable": {"allowed_sources": allowed_sources}} if allowed_sources is not None else {}
        state = {"messages": (messages or []) + [HumanMessage(question)]}
        return asyncio.run(graph.ainvoke(state, config))

    def test_parallel_research_then_write_and_approve(self):
        llm = ScriptedLLM(
            Plan(tasks=[Task(question="Part one?", source="study_assistant"),
                        Task(question="Part two?", source="architecture_guide")]),
            reviews=[Review(approved=True, missing=[], unsupported=[])], answers=["Final answer."])
        out = self.run_turn(llm)
        self.assertEqual(llm.calls, ["Plan", "Finding", "Finding", "write", "Review"])
        self.assertEqual({f["source"] for f in out["findings"]}, {"study_assistant", "architecture_guide"})
        final = out["messages"][-1]
        self.assertEqual(final.text, "Final answer.")
        self.assertEqual(len(final.response_metadata["sources"]), 2)

    def test_researchers_run_in_parallel(self):
        import time

        def slow_search(*args):
            time.sleep(0.4)
            return fake_search(*args)

        s14.search = slow_search
        try:
            llm = ScriptedLLM(
                Plan(tasks=[Task(question=f"Part {i}?", source=s)
                            for i, s in enumerate(["study_assistant", "architecture_guide", "study_assistant"])]),
                [Review(approved=True, missing=[], unsupported=[])], ["A."])
            start = time.perf_counter()
            self.run_turn(llm)
            elapsed = time.perf_counter() - start
        finally:
            s14.search = fake_search
        self.assertLess(elapsed, 0.8, f"three 0.4 s searches took {elapsed:.2f} s: not parallel")

    def test_tasks_for_unknown_sources_are_dropped_and_capped(self):
        tasks = [Task(question=f"Q{i}?", source="study_assistant") for i in range(6)]
        tasks.insert(0, Task(question="Secret?", source="hr_records"))
        llm = ScriptedLLM(Plan(tasks=tasks), [Review(approved=True, missing=[], unsupported=[])], ["A."])
        out = self.run_turn(llm)
        self.assertEqual(len(out["findings"]), self.bot.research.max_tasks)
        self.assertNotIn("hr_records", {f["source"] for f in out["findings"]})

    def test_planner_is_only_offered_allowed_sources_and_researchers_obey(self):
        # The model "tries" a source the user may not use: it is never searched.
        llm = ScriptedLLM(
            Plan(tasks=[Task(question="Course part?", source="study_assistant"),
                        Task(question="Design part?", source="architecture_guide")]),
            [Review(approved=True, missing=[], unsupported=[])], ["A."])
        out = self.run_turn(llm, allowed_sources=["architecture_guide"])
        self.assertEqual([f["source"] for f in out["findings"]], ["architecture_guide"])
        self.assertTrue(all("documents_clean" not in s for s in llm.seen))  # the course index was never read

    def test_no_allowed_sources_means_no_model_calls(self):
        llm = ScriptedLLM(Plan(tasks=[]), [], [])
        out = self.run_turn(llm, allowed_sources=[])
        self.assertEqual(llm.calls, [])
        self.assertIn("do not have access", out["messages"][-1].text)

    def test_reviewer_gaps_trigger_one_more_round_and_the_draft_is_replaced(self):
        llm = ScriptedLLM(
            Plan(tasks=[Task(question="Part one?", source="study_assistant")]),
            reviews=[Review(approved=False, missing=[Task(question="Part two?", source="architecture_guide")],
                            unsupported=[])],
            answers=["First draft.", "Second draft."])
        out = self.run_turn(llm)
        # review_rounds is 1: after the second draft the reviewer is not asked again.
        self.assertEqual(llm.calls, ["Plan", "Finding", "write", "Review", "Finding", "write"])
        texts = [m.text for m in out["messages"] if isinstance(m, AIMessage)]
        self.assertEqual(texts, ["Second draft."])  # the rejected draft is gone
        self.assertEqual(len(out["findings"]), 2)

    def test_unsupported_claims_trigger_a_rewrite_without_new_research(self):
        llm = ScriptedLLM(
            Plan(tasks=[Task(question="Part one?", source="study_assistant")]),
            reviews=[Review(approved=False, missing=[], unsupported=["an invented number"])],
            answers=["Draft with a claim.", "Corrected draft."])
        out = self.run_turn(llm)
        self.assertEqual(llm.calls, ["Plan", "Finding", "write", "Review", "write"])
        self.assertEqual(out["messages"][-1].text, "Corrected draft.")

    def test_findings_do_not_leak_into_the_next_turn(self):
        plan = Plan(tasks=[Task(question="Part one?", source="study_assistant")])
        approve = Review(approved=True, missing=[], unsupported=[])
        graph = s14.build_research_graph(self.bot, self.sources, ScriptedLLM(plan, [approve, approve], ["A.", "B."]))
        first = asyncio.run(graph.ainvoke({"messages": [HumanMessage("Q1?")]}))
        second = asyncio.run(graph.ainvoke({**first, "messages": first["messages"] + [HumanMessage("Q2?")]}))
        self.assertEqual(len(second["findings"]), 1)

    def test_a_research_bot_cannot_list_itself(self):
        data = self.bot.model_dump()
        data["research"]["sources"] = ["research_assistant"]
        with self.assertRaises(ValueError):
            type(self.bot).model_validate(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
