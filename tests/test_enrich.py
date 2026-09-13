"""The vault enrichment and the knowledge evaluation (spec section 5.1):
phases from the task table, aliases from the title, summaries from the
model and marked as such, property writes that keep the rest of the front
matter, and a hit rate that counts pages reaching a member."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from programmind.knowledge import enrich

from programmind.knowledge import evaluate

from programmind.knowledge import knowledge  # noqa: E402
from programmind.ai.provider import AiProvider, AiResult  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _roles_fixture import make_roles  # noqa: E402

OVERVIEW = """---
kind: process
---
# VPDS

## Tasks

| Task | Pursuit | MP0 | MP1 | MP2 | MP3 | MP4 | MP5 | MP6 | MP7 | MP8 | MP9 | MP10 |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| [[VPDS_Design Freeze\\|Design Freeze]] |  |  |  |  | x | x |  |  |  |  |  |  |
| [[VPDS_Customer Part Approval (PPAP)\\|Customer Part Approval (PPAP)]] |  |  |  |  |  | x | x | x |  |  |  |  |
| Engineering Delivery Resources | x (and Expenses) | x |  |  |  |  |  |  |  |  |  |  |
| Maturity Gate Review |  | x | x | x | x | x | x | x | x | x | x | x |
"""

FREEZE = """---
kind: process
task: Design Freeze
lead_swimlane:
affected_swimlanes: [Hardware, Mechanical]
updated: 2026-09-01
---
# VPDS task - Design Freeze

## Definition
The design is frozen at MG4: no change without a change request.
"""

PPAP = """---
kind: process
task: Customer Part Approval (PPAP)
updated: 2026-09-01
---
# VPDS task - Customer Part Approval (PPAP)

## Definition
The customer approves the production part.
"""

GATES = """---
kind: process
task: Maturity Gate Review
summary: "AI summary, not official. Already there."
---
# VPDS task - Maturity Gate Reviews (MP1 to MP7)

## Definition
The gate review closes each phase.
"""


class SummaryProvider(AiProvider):
    def __init__(self, answer=None):
        self.prompts: list[str] = []
        self.answer = answer

    def complete(self, task: str, prompt: str, on_text=None) -> AiResult:
        self.prompts.append(prompt)
        if self.answer is not None:
            text = self.answer
        else:
            paths = [line[4:].strip() for line in prompt.splitlines() if line.startswith("### ")]
            text = json.dumps({p: f"Summary of {Path(p).stem}.  With  spaces." for p in paths})
        return AiResult(text=text, provider="fake", model="m", input_tokens=1, output_tokens=1, duration_seconds=0)


def _vault(tmp: Path) -> Path:
    tasks = tmp / "Process" / "VPDS" / "VPDS_Tasks"
    tasks.mkdir(parents=True)
    (tmp / "Process" / "VPDS" / "VPDS_Overview.md").write_text(OVERVIEW, encoding="utf-8")
    (tasks / "VPDS_Design Freeze.md").write_text(FREEZE, encoding="utf-8")
    (tasks / "VPDS_Customer Part Approval (PPAP).md").write_text(PPAP, encoding="utf-8")
    (tasks / "VPDS_Maturity Gate Reviews (MP1 to MP7).md").write_text(GATES, encoding="utf-8")
    (tmp / "Plain.md").write_text("No front matter at all.\n", encoding="utf-8")
    return tmp


class TestTaskTable(unittest.TestCase):
    def test_phases_come_from_the_table_and_links_with_escaped_pipes_keep_their_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = knowledge.load_vault(_vault(Path(tmp)))
            overview = next(n for n in notes if n.path.stem == "VPDS_Overview")
            phases = enrich.task_phases(overview)
        self.assertEqual(phases["Design Freeze"], ("MP3", "MP4"))
        self.assertEqual(phases["Customer Part Approval (PPAP)"], ("MP4", "MP5", "MP6"))
        self.assertEqual(phases["Engineering Delivery Resources"], ("PURSUIT", "MP0"))   # any text marks a cell
        self.assertEqual(phases["Maturity Gate Review"][0], "MP0")
        self.assertNotIn("Task", phases)

    def test_aliases_are_the_table_name_only_and_never_an_abbreviation(self):
        def note(title, **extra):
            return knowledge.Note(path=Path("x"), relative="x", title=title, tags=(), body="", kind="process", **extra)
        self.assertEqual(enrich.task_aliases(note("VPDS_Customer Part Approval (PPAP)"), "Customer Part Approval (PPAP)"), ())
        gates = note("VPDS_Maturity Gate Reviews (MP1 to MP7)")
        self.assertEqual(enrich.task_aliases(gates, "Maturity Gate Review"), ("Maturity Gate Review",))
        kept = note("VPDS_Design Freeze", aliases=("Freeze",))
        self.assertEqual(enrich.task_aliases(kept, "Design Freeze"), ("Freeze",))


class TestPropose(unittest.TestCase):
    def test_proposal_carries_phases_aliases_and_marked_summaries_for_pages_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = knowledge.load_vault(_vault(Path(tmp)))
            provider = SummaryProvider()
            proposal = enrich.propose(notes, provider)
        by_key = {(c.path, c.key): c for c in proposal.changes}
        freeze = "Process/VPDS/VPDS_Tasks/VPDS_Design Freeze.md"
        self.assertEqual(by_key[(freeze, "phases")].value, ["MP3", "MP4"])
        self.assertNotIn(("Process/VPDS/VPDS_Tasks/VPDS_Customer Part Approval (PPAP).md", "aliases"), by_key)   # PPAP is the table's job
        self.assertEqual(by_key[("Process/VPDS/VPDS_Tasks/VPDS_Maturity Gate Reviews (MP1 to MP7).md", "aliases")].value, ["Maturity Gate Review"])
        self.assertNotIn((freeze, "aliases"), by_key)                       # nothing to add for a plain title
        summary = by_key[(freeze, "summary")].value
        self.assertTrue(summary.startswith(enrich.SUMMARY_PREFIX + " "))
        self.assertIn("Summary of VPDS_Design Freeze. With spaces.", summary)   # whitespace folded
        gates = "Process/VPDS/VPDS_Tasks/VPDS_Maturity Gate Reviews (MP1 to MP7).md"
        self.assertNotIn((gates, "summary"), by_key)                        # already has one
        self.assertIn(("Plain.md", "summary"), by_key)
        self.assertEqual(proposal.calls, 1)
        self.assertEqual(proposal.problems, [])
        self.assertIn("## Pages", provider.prompts[0])

    def test_refresh_rewrites_and_no_model_is_a_problem_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = knowledge.load_vault(_vault(Path(tmp)))
            refreshed = enrich.propose(notes, SummaryProvider(), refresh=True)
            gates = "Process/VPDS/VPDS_Tasks/VPDS_Maturity Gate Reviews (MP1 to MP7).md"
            self.assertIn(gates, [c.path for c in refreshed.changes if c.key == "summary"])
            without = enrich.propose(notes, None)
            self.assertTrue(any("no model is configured" in p for p in without.problems))
            self.assertFalse(any(c.key == "summary" for c in without.changes))
            bad = enrich.propose(notes, SummaryProvider(answer="not json"))
            self.assertTrue(any("returned no summary" in p for p in bad.problems))
            unknown = enrich.propose(notes, None, summaries=False)
        self.assertFalse(any("Maturity" in p for p in unknown.problems))
        self.assertIn("Nothing to change.", enrich.describe(enrich.Proposal()))
        self.assertIn("- Plain.md: summary:", enrich.describe(bad) + enrich.describe(refreshed))


class TestSetProperty(unittest.TestCase):
    def test_inline_block_missing_and_no_front_matter(self):
        text = "---\nkind: process\naffected_swimlanes:\n  - Hardware\n  - Mechanical\nupdated: 2026-09-01\n---\n# Page\n"
        out = enrich.set_property(text, "affected_swimlanes", ["Hardware"])
        self.assertEqual(out, "---\nkind: process\naffected_swimlanes: [Hardware]\nupdated: 2026-09-01\n---\n# Page\n")
        out = enrich.set_property(text, "phases", ["MP3", "MP4"])
        self.assertIn("  - Mechanical\nphases: [MP3, MP4]\nupdated: 2026-09-01\n---\n# Page\n", out)   # updated stays last
        out = enrich.set_property("# Page\n", "summary", "AI summary, not official. Two: things.")
        self.assertEqual(out, '---\nsummary: "AI summary, not official. Two: things."\n---\n# Page\n')
        self.assertEqual(knowledge._front_matter(out)["summary"], "AI summary, not official. Two: things.")
        out = enrich.set_property("---\nkind: note\n---\n# Page\n", "updated", "2026-09-10")
        self.assertEqual(out, "---\nkind: note\nupdated: 2026-09-10\n---\n# Page\n")
        out = enrich.set_property(text, "aliases", ["Mech, Displays, Optical Dev", "MDO"])
        self.assertIn("aliases:\n  - Mech, Displays, Optical Dev\n  - MDO\nupdated:", out)
        self.assertEqual(knowledge._front_matter(out)["aliases"], ["Mech, Displays, Optical Dev", "MDO"])
        self.assertEqual(enrich.set_property(text, "Kind", "kpi").count("kind"), 0)   # case-insensitive replace

    def test_apply_writes_every_change_and_sets_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            notes = knowledge.load_vault(vault)
            proposal = enrich.propose(notes, SummaryProvider())
            written = enrich.apply(vault, proposal, "2026-09-10")
            self.assertEqual(len(written), 5)      # overview, three task pages, the plain page
            freeze = (vault / "Process/VPDS/VPDS_Tasks/VPDS_Design Freeze.md").read_text(encoding="utf-8")
            meta = knowledge._front_matter(freeze)
            self.assertEqual(meta["phases"], ["MP3", "MP4"])
            self.assertEqual(meta["updated"], "2026-09-10")
            self.assertEqual(meta["affected_swimlanes"], ["Hardware", "Mechanical"])
            self.assertIn("## Definition\nThe design is frozen at MG4", freeze)
            reloaded = knowledge.load_vault(vault)
            note = next(n for n in reloaded if n.path.stem == "VPDS_Design Freeze")
            self.assertEqual(note.phases, ("MP3", "MP4"))
            self.assertTrue(note.summary.startswith(enrich.SUMMARY_PREFIX))
            self.assertEqual(enrich.propose(reloaded, SummaryProvider()).changes, [])   # idempotent


class TestReviewFixes(unittest.TestCase):
    """Review of 10 September 2026: KPI dates untouched, the marker once,
    the old summary not shown to the model."""

    def test_a_kpi_page_keeps_its_date_and_an_echoed_marker_is_not_doubled(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "Gates.md").write_text("---\nkind: kpi\nupdated: 2026-07-01\n---\n# Gates\n\nMG4 on 2026-11-01.\n", encoding="utf-8")
            (vault / "Note.md").write_text('---\nkind: note\nsummary: "AI summary, not official. Old text."\nupdated: 2026-07-01\n---\n# Note\n\nNew text.\n', encoding="utf-8")
            notes = knowledge.load_vault(vault)
            provider = SummaryProvider(answer=json.dumps({"Gates.md": "AI summary, not official. Gate dates.",
                                                          "Note.md": "AI summary, not official. AI summary, not official. New."}))
            proposal = enrich.propose(notes, provider, refresh=True)
            self.assertNotIn("Old text", provider.prompts[0])          # the page, not its previous summary
            values = {c.path: c.value for c in proposal.changes if c.key == "summary"}
            self.assertEqual(values["Gates.md"], "AI summary, not official. Gate dates.")
            self.assertEqual(values["Note.md"], "AI summary, not official. New.")
            enrich.apply(vault, proposal, "2026-09-10")
            self.assertEqual(knowledge._front_matter((vault / "Gates.md").read_text(encoding="utf-8"))["updated"], "2026-07-01")
            self.assertEqual(knowledge._front_matter((vault / "Note.md").read_text(encoding="utf-8"))["updated"], "2026-09-10")


class TestEvaluate(unittest.TestCase):
    def test_hit_rate_counts_pages_that_reach_a_member_in_full_or_as_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _vault(Path(tmp))
            make_roles(vault / "Roles")
            config = {"knowledge": {"vault_path": str(vault), "token_budget": 6000, "selection": "python"}}
            questions = [
                {"question": "Can we freeze the design before MG4 with the PPAP still open?",
                 "expected": ["Process/VPDS/VPDS_Tasks/VPDS_Design Freeze", "Process/VPDS/VPDS_Tasks/VPDS_Customer Part Approval (PPAP).md",
                              "Missing/Page.md"],
                 "members": {"Manufacturing": ["Process/VPDS/VPDS_Tasks/VPDS_Design Freeze.md"]}},
                {"_comment": "skipped, no question"},
            ]
            set_path = Path(tmp) / "questions.json"
            set_path.write_text(json.dumps(questions), encoding="utf-8")
            loaded = evaluate.load_set(set_path)
            self.assertEqual(len(loaded), 1)
            outcomes = evaluate.evaluate(config, loaded, selection="python")
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertIn("Process/VPDS/VPDS_Tasks/VPDS_Design Freeze.md", outcome.hits)
        self.assertIn("Process/VPDS/VPDS_Tasks/VPDS_Customer Part Approval (PPAP).md", outcome.hits)
        self.assertEqual(outcome.misses, ["Missing/Page.md"])
        self.assertIn("Manufacturing: Process/VPDS/VPDS_Tasks/VPDS_Design Freeze.md", outcome.hits)
        self.assertAlmostEqual(outcome.rate, 3 / 4)
        text = evaluate.report(outcomes, "python")
        self.assertIn("Hit rate: 3/4 = 75%", text)
        self.assertIn("missed: Missing/Page.md", text)
        self.assertEqual(evaluate.report([], "ai").splitlines()[-1], "Hit rate: 0/0")


if __name__ == "__main__":
    unittest.main()
