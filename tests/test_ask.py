"""Ask the vault (spec section 10): the prompt, the answer with its
sources checked by Python, the KPI block, the thread store."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from programmind.agents.ask import ask  # noqa: E402
from programmind.ai.provider import AiProvider, AiResult  # noqa: E402

SENT = {
    "Process/VPDS/VPDS_Overview.md": "## Maturity phases and gates\n\nMG4 closes DV completion.\n",
    "Process/VPDS/VPDS_Tasks/VPDS_Design Freeze.md": "## Definition\n\nA milestone.\n",
}
BRIEFS = {"KPIs/Dual DCDC - Maturity Gates.md": "AI summary, not official. The dates."}


class FixedProvider(AiProvider):
    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    def complete(self, task: str, prompt: str, on_text=None) -> AiResult:
        self.prompts.append(prompt)
        return AiResult(text=self.text, provider="fake", model="m", input_tokens=1, output_tokens=1, duration_seconds=0)


class TestPrompt(unittest.TestCase):
    def test_prompt_carries_the_thread_the_question_the_kpis_and_the_knowledge(self):
        text = ask.ask_prompt("And who approves it?", "## Knowledge from the vault\n\nstuff", "### KPIs\nnumbers",
                              [("What is MG4?", "The gate after DV.")], project="Dual DCDC")
        self.assertIn(ask.MARKER + "\n\nAnd who approves it?", text)
        self.assertIn("## Earlier in this thread\n\nQ: What is MG4?\nA: The gate after DV.", text)
        self.assertIn("Project: Dual DCDC", text)
        self.assertIn("## KPI notes of the project\n\n### KPIs", text)
        self.assertLess(text.index("## Earlier"), text.index(ask.MARKER))
        self.assertLess(text.index(ask.MARKER), text.index("## Knowledge from the vault"))

    def test_a_long_thread_keeps_the_latest_turns(self):
        history = [(f"q{i}", "a" * 5000) for i in range(6)]
        text = ask.ask_prompt("next?", "", "", history)
        self.assertIn("Q: q5", text)
        self.assertNotIn("Q: q0", text)
        self.assertLess(text.index("Q: q4"), text.index("Q: q5"))


class TestLiveAnswer(unittest.TestCase):
    """Spec 4.1: the page shows the answer as it is written, read out of the
    half-finished JSON the stream carries."""

    def test_the_answer_so_far_is_read_out_of_half_written_json(self):
        self.assertEqual(ask.live_answer('{"answer": "MG4 closes'), "MG4 closes")
        self.assertEqual(ask.live_answer('{"answer": "a\\nb \\"q\\" '), 'a\nb "q" ')
        self.assertEqual(ask.live_answer('{"answer": "done", "sources": []}'), "done")
        self.assertEqual(ask.live_answer('{"answer": "MG\\u00964"'), "MG\u00964")

    def test_nothing_is_shown_before_the_answer_starts_or_mid_escape(self):
        self.assertEqual(ask.live_answer(""), "")
        self.assertEqual(ask.live_answer('{"sour'), "")
        self.assertEqual(ask.live_answer('{"gaps": ["none"], "answer'), "")
        self.assertEqual(ask.live_answer('{"answer": "tail\\'), "tail")        # the escape is not finished yet
        self.assertEqual(ask.live_answer('{"answer": "tail\\u00'), "tail")

    def test_a_fenced_answer_and_an_odd_key_still_read(self):
        self.assertEqual(ask.live_answer('```json\n{"answer" : "Fenced"'), "Fenced")


class TestLoopPrompt(unittest.TestCase):
    """Spec 5.5: the answer names pages it wants, sees the table of
    contents, and a later round carries the answer so far and the check."""

    def test_missing_is_parsed_in_either_shape_and_capped(self):
        text = json.dumps({"answer": "x", "sources": [], "gaps": [], "missing": ["A.md", {"path": "B.md"}, "A.md", ""]})
        self.assertEqual(ask.parse_answer(text, SENT).missing, ["A.md", "B.md"])
        text = json.dumps({"answer": "x", "sources": [], "gaps": [], "missing": "C.md"})
        self.assertEqual(ask.parse_answer(text, SENT).missing, ["C.md"])
        text = json.dumps({"answer": "x", "sources": [], "gaps": [], "missing": [f"P{i}.md" for i in range(80)]})
        self.assertEqual(len(ask.parse_answer(text, SENT).missing), ask.MAX_MISSING)
        self.assertEqual(ask.parse_answer(json.dumps({"answer": "x"}), SENT).missing, [])

    def test_the_prompt_lists_the_pages_read_earlier_and_the_table_of_contents(self):
        text = ask.ask_prompt("next?", "K", "", [], read_before=["A.md", "B.md"], contents_text="- page: A.md | A | 3 tokens")
        self.assertIn("## Pages read earlier in this thread\n\n- A.md\n- B.md\n\n" + ask.MARKER, text)
        self.assertIn("## Table of contents of the vault", text)
        self.assertLess(text.index("K"), text.index("## Table of contents"))
        self.assertNotIn("## Pages read earlier", ask.ask_prompt("next?", "K", "", []))
        self.assertNotIn("## Table of contents", ask.ask_prompt("next?", "K", "", []))

    def test_a_later_round_carries_the_answer_so_far_and_the_check(self):
        first = ask.Answer(answer="Half an answer.", gaps=["the wording"])
        provider = FixedProvider(json.dumps({"answer": "Whole answer.", "sources": [{"path": "Process/VPDS/VPDS_Overview.md"}], "gaps": [], "missing": []}))
        answer = ask.ask(provider, "The question?", "## Knowledge from the vault\n\nall pages", "", [("q", "a")], SENT, None, "Dual DCDC",
                         earlier=first, check_note="One task was not read.", round_no=2)
        prompt = provider.prompts[0]
        self.assertIn("## Your answer so far (round 1)\n\nHalf an answer.\n\nGaps you named: the wording\n\nWhat the check said: One task was not read.\n\nThis is round 2", prompt)
        self.assertLess(prompt.index(ask.MARKER), prompt.index("## Your answer so far"))
        self.assertLess(prompt.index("## Your answer so far"), prompt.index("## Knowledge from the vault"))
        self.assertEqual(answer.answer, "Whole answer.")


class TestParse(unittest.TestCase):
    def test_only_sent_sources_survive_and_headings_are_checked(self):
        text = json.dumps({
            "answer": "MG4 closes DV completion.",
            "sources": [
                {"path": "Process/VPDS/VPDS_Overview.md", "heading": "Maturity phases and gates", "why": "defines it"},
                {"path": "VPDS_Design Freeze.md", "heading": "Coaching", "why": "file name only, wrong heading"},
                {"path": "Made up/Page.md", "heading": "", "why": "invented"},
                {"path": "Dual DCDC - Maturity Gates", "heading": "", "why": "only the summary was sent"},
                {"path": "Process/VPDS/VPDS_Overview.md", "heading": "Maturity phases and gates", "why": "twice"},
                "not a dict",
            ],
            "gaps": "- the date\n- the owner",
            "decision_question": True,
        })
        answer = ask.parse_answer(text, SENT, BRIEFS)
        self.assertIsNone(answer.parse_error)
        self.assertEqual([s["path"] for s in answer.sources],
                         ["Process/VPDS/VPDS_Overview.md", "Process/VPDS/VPDS_Tasks/VPDS_Design Freeze.md", "KPIs/Dual DCDC - Maturity Gates.md"])
        self.assertEqual(answer.sources[0]["heading"], "Maturity phases and gates")
        self.assertEqual(answer.sources[1]["heading"], "")            # the page was sent, that section was not
        self.assertFalse(answer.sources[0]["brief"])
        self.assertTrue(answer.sources[2]["brief"])
        self.assertEqual(answer.dropped, 2)                           # the invented page and the non-dict
        self.assertEqual(answer.gaps, ["the date", "the owner"])
        self.assertTrue(answer.decision_question)

    def test_a_non_json_answer_is_kept_as_text_with_a_parse_error(self):
        answer = ask.parse_answer("Plain prose from the model.", SENT)
        self.assertEqual(answer.answer, "Plain prose from the model.")
        self.assertEqual(answer.sources, [])
        self.assertIn("did not parse", answer.parse_error)

    def test_ask_makes_one_call_and_returns_the_result(self):
        provider = FixedProvider(json.dumps({"answer": "Yes.", "sources": [], "gaps": []}))
        answer = ask.ask(provider, "Is it?", "", "", [], SENT)
        self.assertEqual(answer.answer, "Yes.")
        self.assertEqual(len(provider.prompts), 1)
        self.assertIn(ask.MARKER, provider.prompts[0])
        self.assertIs(answer.ai_result.provider, "fake")


class TestKpiText(unittest.TestCase):
    def test_every_kpi_note_of_the_project_with_its_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "Gates.md").write_text("---\nkind: kpi\nprojects: [Dual DCDC]\nupdated: 2026-08-01\naffected_swimlanes: [Hardware]\n---\n# Gates\n\nMG4 2026-11-01\n", encoding="utf-8")
            (vault / "Other gates.md").write_text("---\nkind: kpi\nprojects: [Other]\nupdated: 2026-08-01\n---\n# Other\n\nMG4 2027-01-01\n", encoding="utf-8")
            (vault / "Plain.md").write_text("Not a KPI.\n", encoding="utf-8")
            config = {"knowledge": {"vault_path": str(vault)}}
            text = ask.project_kpi_text(config, ["Dual DCDC"], today=date(2026, 9, 10))
        self.assertIn("### Gates.md", text)
        self.assertIn("MG4 2026-11-01", text)
        self.assertNotIn("Other gates", text)
        self.assertNotIn("Plain", text)
        self.assertIn("2026-08-01", text)                                  # the freshness line names the date
        self.assertEqual(ask.project_kpi_text({"knowledge": {}}, []), "")


class TestThreadStore(unittest.TestCase):
    def test_new_save_load_list_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ask.ThreadStore(Path(tmp) / "threads")
            self.assertEqual(store.list(), [])
            first = store.new(["Dual DCDC"], 9000)
            first.turns.append({"question": "What is MG4?", "answer": "The gate.", "sources": [], "gaps": [], "at": 1.0})
            store.save(first)
            second = store.new()
            store.save(second)
            listed = store.list()
            self.assertEqual([t["id"] for t in listed], [second.id, first.id])    # newest first
            self.assertEqual(listed[1]["title"], "What is MG4?")
            self.assertEqual(listed[1]["questions"], 1)
            self.assertEqual(listed[0]["title"], "New thread")
            loaded = store.load(first.id)
            self.assertEqual(loaded.projects, ["Dual DCDC"])
            self.assertEqual(loaded.budget, 9000)
            self.assertEqual(loaded.history(), [("What is MG4?", "The gate.")])
            self.assertEqual(loaded.status, "open")
            loaded.status = "closed"
            store.save(loaded)
            self.assertEqual(store.load(first.id).status, "closed")
            (Path(tmp) / "threads" / "brokenbroken.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(len(store.list()), 2)                    # the broken file is skipped, not deleted
            self.assertTrue(store.delete(second.id))
            self.assertFalse(store.delete(second.id))
            self.assertIsNone(store.load(second.id))
            with self.assertRaises(ValueError):
                store.load("../etc/passwd")


if __name__ == "__main__":
    unittest.main()
