#!/usr/bin/env python3
"""KPI notes (spec 3.4, 9 September 2026): a vault note with kind: kpi is
attached to the member it names on every call, is never part of the ranked
selection, and its absence is said out loud."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from programmind.agents.board import board

from programmind.knowledge import knowledge

from programmind.agents.board import roles  # noqa: E402
from programmind.ai.provider import AiProvider, AiResult  # noqa: E402
from _roles_fixture import make_roles  # noqa: E402


def _vault(tmp: Path) -> Path:
    vault = tmp / "vault"
    (vault / "KPI").mkdir(parents=True)
    (vault / "KPI" / "KPI Hardware.md").write_text(
        "---\nkind: kpi\naffected_swimlanes: [Hardware]\nbaseline: MG0\n---\n# KPI Hardware\n\n"
        "| KPI | MG0 | Target | Current |\n|---|---|---|---|\n| cBOM cost | 100 | 95 | 104 |\n", encoding="utf-8")
    (vault / "KPI" / "KPI shared.md").write_text(
        "---\nkind: kpi\nmember: [Hardware, Finance]\n---\nMilestone slip: 2 weeks.\n", encoding="utf-8")
    (vault / "Tooling.md").write_text("The cBOM cost of the tooling is under review.\n", encoding="utf-8")
    return vault


class TestKpiNotes(unittest.TestCase):
    def test_lead_swimlane_receives_the_note_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            (vault / "KPI" / "KPI lead.md").write_text(
                "---\nkind: kpi\nlead_swimlane: Software\naffected_swimlanes: [Hardware]\n---\nRelease slip: 1 week.\n", encoding="utf-8")
            data = knowledge.kpi_notes({"knowledge": {"vault_path": str(vault)}}, ["Software", "Hardware", "Finance"])
        self.assertIn("Release slip", data["Software"])
        self.assertIn("Release slip", data["Hardware"])
        self.assertNotIn("Release slip", data.get("Finance", ""))

    def test_the_old_member_key_is_still_read(self):
        # KPI shared.md in the fixture uses "member:"; both keys attach the note
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            data = knowledge.kpi_notes({"knowledge": {"vault_path": str(vault)}}, ["Finance"])
        self.assertIn("Milestone slip", data["Finance"])

    def test_notes_are_attached_to_the_members_they_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            data = knowledge.kpi_notes({"knowledge": {"vault_path": str(vault)}}, ["Hardware", "Finance", "Software"])
        self.assertEqual(sorted(data), ["Finance", "Hardware"])
        self.assertIn("| cBOM cost | 100 | 95 | 104 |", data["Hardware"])
        self.assertIn("Milestone slip: 2 weeks.", data["Hardware"])
        self.assertIn("Milestone slip: 2 weeks.", data["Finance"])
        self.assertNotIn("cBOM cost | 100", data["Finance"])

    def test_kpi_notes_are_not_part_of_the_ranked_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            selection = knowledge.gather({"knowledge": {"vault_path": str(vault)}}, "cBOM cost")
        self.assertEqual(selection.relative_paths, ["Tooling.md"])

    def test_no_vault_means_no_data_and_no_error(self):
        self.assertEqual(knowledge.kpi_notes({}, ["Hardware"]), {})

    def test_member_names_match_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            data = knowledge.kpi_notes({"knowledge": {"vault_path": str(vault)}}, ["hardware"])
        self.assertIn("hardware", data)


class TestKpiInPrompts(unittest.TestCase):
    class Recorder(AiProvider):
        def __init__(self): self.prompts = []
        def complete(self, task, prompt, on_text=None):
            self.prompts.append(prompt)
            text = json.dumps({"overall_recommendation": "x", "decisive_criterion": "y",
                               "counter_arguments": [], "what_would_change_it": "", "disagreements": []}) \
                if "## Assessments" in prompt else json.dumps({"view": "v", "risks": "r", "recommendation": "rec"})
            return AiResult(text=text, provider="f", model="m", input_tokens=1, output_tokens=1, duration_seconds=0)

    def test_run_board_attaches_kpi_data_to_the_right_member_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            folder = make_roles(Path(tmp) / "r", ("Hardware", "Finance", "Software"))
            config = {"provider": {"models": {"board": "f/m"}},
                      "knowledge": {"vault_path": str(vault), "roles_folder": str(folder)}}
            provider = self.Recorder()
            board.run_board(config, provider, topic="t")
        by_member = {p.split("Member: ", 1)[1].splitlines()[0]: p for p in provider.prompts if "Member: " in p}
        self.assertIn("| cBOM cost | 100 | 95 | 104 |", by_member["Hardware"])
        self.assertIn("## KPI data from the knowledge network", by_member["Hardware"])
        self.assertNotIn("cBOM cost | 100", by_member["Finance"])
        self.assertIn("Milestone slip: 2 weeks.", by_member["Finance"])
        self.assertIn("No KPI note for this member is in the knowledge network yet", by_member["Software"])
        self.assertNotIn("cBOM cost | 100", by_member["Software"])
        synthesis = [p for p in provider.prompts if "## Assessments" in p][0]
        self.assertNotIn("cBOM cost | 100", synthesis)


if __name__ == "__main__":
    unittest.main()
