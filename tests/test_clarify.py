#!/usr/bin/env python3
"""Tests for the clarifier (FR-3.2 as decided 8 September 2026) and the
memory writer (spec section 5), the model mocked at the ``AiProvider``
boundary."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from programmind.agents.board import clarify

from programmind.memory import memory_writer  # noqa: E402
from programmind.ai.provider import AiProvider, AiResult  # noqa: E402


class FakeProvider(AiProvider):
    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    def complete(self, task: str, prompt: str, on_text=None) -> AiResult:
        self.prompts.append(prompt)
        return AiResult(text=self.text, provider="fake", model="m", input_tokens=1, output_tokens=1, duration_seconds=0)


class TestClarify(unittest.TestCase):
    def test_extracts_inputs_and_questions(self):
        provider = FakeProvider(json.dumps({
            "topic": "Rework or switch", "context": "SOP fixed", "options": ["Rework", "Switch"],
            "constraints": ["SOP cannot move"], "questions": ["What is the budget?", "Who owns the tooling?"],
        }))
        result = clarify.clarify(provider, "Rework the tooling or switch supplier?", "## Knowledge from the vault\n\nnote")
        self.assertEqual(result.topic, "Rework or switch")
        self.assertEqual(result.options, ["Rework", "Switch"])
        self.assertEqual(len(result.questions), 2)
        self.assertIn("## Question from Alex", provider.prompts[0])
        self.assertIn("## Knowledge from the vault", provider.prompts[0])

    def test_minimum_one_question_is_enforced_in_python(self):
        result = clarify.parse_clarification(json.dumps({"topic": "T", "questions": []}), "raw")
        self.assertEqual(result.questions, [clarify.FALLBACK_QUESTION])

    def test_unparsable_response_falls_back_to_the_raw_question(self):
        result = clarify.parse_clarification("not json", "Should we switch supplier?")
        self.assertEqual(result.topic, "Should we switch supplier?")
        self.assertEqual(result.questions, [clarify.FALLBACK_QUESTION])
        self.assertIsNotNone(result.parse_error)

    def test_a_later_round_carries_the_earlier_questions_and_answers(self):
        provider = FakeProvider(json.dumps({"topic": "T", "context": "c", "questions": [], "clear": True}))
        rounds = [(["What is the budget?", "Who owns it?"], ["200k", ""])]
        result = clarify.clarify(provider, "Rework?", "", rounds)
        prompt = provider.prompts[0]
        self.assertIn("## Clarification so far", prompt)
        self.assertIn("Q: What is the budget?\nA: 200k", prompt)
        self.assertIn("Q: Who owns it?\nA: (not answered)", prompt)
        self.assertIn(f"round 2 of at most {clarify.MAX_ROUNDS}", prompt)
        self.assertEqual(result.questions, [])          # clear: no fallback question after round one

    def test_the_first_round_still_asks_at_least_one_question(self):
        self.assertEqual(clarify.parse_clarification(json.dumps({"questions": []}), "q").questions,
                         [clarify.FALLBACK_QUESTION])
        self.assertEqual(clarify.parse_clarification(json.dumps({"questions": []}), "q", first_round=False).questions, [])
        self.assertEqual(clarify.parse_clarification("not json", "q", first_round=False).questions, [])

    def test_every_round_is_folded_into_the_context(self):
        text = clarify.merge_rounds("SOP fixed", [(["Budget?"], ["200k"]), (["Owner?"], [""])])
        self.assertTrue(text.startswith("SOP fixed"))
        self.assertIn("Q: Budget?\nA: 200k", text)
        self.assertIn("Q: Owner?\nA: (not answered)", text)

    def test_answers_are_folded_into_the_context_deterministically(self):
        clarification = clarify.Clarification(topic="T", context="Background.", questions=["Q1?", "Q2?"])
        context = clarify.merge_rounds(clarification.context, [(clarification.questions, ["A1", ""])])
        self.assertIn("Background.", context)
        self.assertIn("Q: Q1?\nA: A1", context)
        self.assertIn("Q: Q2?\nA: (not answered)", context)


class TestMemoryWriter(unittest.TestCase):
    def test_safe_path_cannot_escape_the_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            self.assertEqual(memory_writer.safe_relative_path(vault, "../../etc/passwd.md", "Topic"), "etc/passwd.md")
            self.assertTrue(memory_writer.safe_relative_path(vault, "", "My Topic").endswith("My Topic.md"))
            self.assertEqual(memory_writer.safe_relative_path(vault, "Decisions/note", "x"), "Decisions/note.md")

    def test_dotted_tricks_and_null_bytes_stay_inside_the_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            self.assertEqual(memory_writer.safe_relative_path(vault, "..:/..:/evil.md", "Topic"), "evil.md")
            self.assertTrue(memory_writer.safe_relative_path(vault, "a\x00b.md", "Topic").endswith("ab.md"))

    def test_propose_parses_and_marks_create_or_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "Existing.md").write_text("# Existing\n", encoding="utf-8")
            provider = FakeProvider(json.dumps({"path": "Existing.md", "title": "Rework", "tags": ["tooling"], "body": "Decided: rework."}))
            proposal = memory_writer.propose(provider, vault, topic="Rework", inputs={"context": "c"}, synthesis={"overall_recommendation": "Rework"}, turns=[])
            self.assertEqual(proposal.mode, "append")
            self.assertIn("decision-board", proposal.tags)
            self.assertIn("## Vault outline", provider.prompts[0])
            provider2 = FakeProvider(json.dumps({"path": "Decisions/Rework.md", "title": "Rework", "tags": [], "body": "x"}))
            proposal2 = memory_writer.propose(provider2, vault, topic="Rework", inputs={}, synthesis="s", turns=[("q", "a")])
            self.assertEqual(proposal2.mode, "create")

    def test_write_creates_with_front_matter_and_appends_on_second_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            proposal = memory_writer.MemoryProposal(path="Decisions/Rework.md", title="Rework", tags=["a"], body="Body.", mode="create")
            target = memory_writer.write_note(vault, proposal)
            text = target.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\ntitle: \"Rework\"\ntags: [a, decision-board]\n"), text)
            self.assertTrue(text.endswith("Body.\n"))
            memory_writer.write_note(vault, proposal)
            text = target.read_text(encoding="utf-8")
            self.assertEqual(text.count("Body."), 2)
            self.assertIn("\n---\n\n## Rework\n", text)

    def test_preview_is_exactly_what_gets_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            proposal = memory_writer.MemoryProposal(path="N.md", title="N", tags=[], body="B", mode="create")
            preview = memory_writer.preview(proposal)
            target = memory_writer.write_note(vault, proposal)
            self.assertEqual(target.read_text(encoding="utf-8"), preview)


if __name__ == "__main__":
    unittest.main()
