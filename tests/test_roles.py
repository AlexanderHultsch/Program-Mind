#!/usr/bin/env python3
"""Tests for the role profiles (spec section 3.4): the board is whoever has
a profile in one folder, one file per role or one file with several, read
fresh on every run; each member sees only its own; the synthesis sees one
line per member; the shipped examples are the only fallback."""

from __future__ import annotations

import io
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
from programmind.agents.board import roles
from programmind import setup_wizard  # noqa: E402
from programmind.ai.provider import AiProvider, AiResult  # noqa: E402
from _roles_fixture import CLASSIC, make_roles  # noqa: E402


def _single(folder: Path, member: str, body: str = "", **meta) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    front = "\n".join(f"{key}: {value}" for key, value in {"member": member, **meta}.items())
    path = folder / f"{member}.md"
    content = body or f"Profile of {member}: what it is responsible for, what it measures, how it talks."
    path.write_text(f"---\n{front}\n---\n# {member}\n\n## Character\n{content}\n", encoding="utf-8")
    return path


class TestNothingShipped(unittest.TestCase):
    def test_the_repository_ships_no_members_only_support_files(self):
        names = sorted(p.name for p in roles.SUPPORT_DIR.glob("*.md"))
        self.assertEqual(names, sorted(["README.md", roles.CONDUCT_NAME, roles.TEMPLATE_NAME,
                                        roles.KPI_TEMPLATE_NAME]))
        self.assertEqual(roles.members_in(roles.SUPPORT_DIR), [])

    def test_no_folder_means_no_board(self):
        with self.assertRaises(roles.RolesUnavailable) as raised:
            roles.load_board({})
        self.assertIn("no roles folder", str(raised.exception))

    def test_no_member_list_survives_in_the_code(self):
        self.assertFalse(hasattr(board, "BOARD_MEMBERS"))
        self.assertFalse(hasattr(roles, "MEMBERS"))
        prompt = (REPO_ROOT / "src/programmind/agents/board/prompts/board_members.md").read_text(encoding="utf-8")
        for name in CLASSIC:
            self.assertNotIn(name, prompt)

    def test_a_fixture_board_loads_in_declared_order_with_the_conduct_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertEqual(tuple(b.profiles), CLASSIC)
        self.assertIn("Speak for every role", b.conduct)
        self.assertEqual([p.name for p in b.conduct_paths], [roles.CONDUCT_NAME])
        self.assertEqual(b.skipped, [])


class TestParsing(unittest.TestCase):
    def test_one_file_per_role_with_front_matter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _single(Path(tmp), "Legal", "Contracts.", title="Legal counsel", perspective="Liability", icon="scale",
                           color="#123456", short="Law", order=2)
            profile = roles.parse_file(path, "configured")[0]
        self.assertEqual((profile.member, profile.title, profile.perspective, profile.icon, profile.color, profile.short, profile.order),
                         ("Legal", "Legal counsel", "Liability", "scale", "#123456", "Law", 2))
        self.assertTrue(profile.body.startswith("# Legal"))

    def test_a_file_without_front_matter_is_one_role_named_after_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Purchasing.md"
            path.write_text("# Purchasing\n\nBuys things.\n", encoding="utf-8")
            profile = roles.parse_file(path, "configured")[0]
        self.assertEqual(profile.member, "Purchasing")
        self.assertEqual(profile.perspective, "Buys things.")
        self.assertEqual(profile.icon, "truck")

    def test_one_file_is_one_swim_lane_with_ranked_roles_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "R&R Hardware.md"
            path.write_text(
                "# HW Swim Lane Leader\nlevel: 3\n\nLeads the hardware engineers day to day.\n\n"
                "# Project Manager HW\nlevel: 2\nicon: chip\n\nDevelop and maintain control over the HW swim lane.\n\n"
                "## Responsibilities\nOwns the HW budget.\n\n"
                "# HW Engineer\nlevel: 4\n\nDesigns the boards.\n", encoding="utf-8")
            profiles = roles.parse_file(path, "configured")
        self.assertEqual(len(profiles), 1)
        hw = profiles[0]
        self.assertEqual(hw.member, "Hardware")
        self.assertEqual(hw.title, "Project Manager HW")           # the highest-ranked role
        self.assertEqual(hw.level, 2)
        self.assertEqual([(r.name, r.level) for r in hw.roles],
                         [("Project Manager HW", 2), ("HW Swim Lane Leader", 3), ("HW Engineer", 4)])
        self.assertEqual(hw.perspective, "Develop and maintain control over the HW swim lane.")
        self.assertEqual(hw.icon, "chip")
        self.assertNotIn("level:", hw.body)
        self.assertTrue(hw.body.startswith("# Project Manager HW"))   # rank order in what the member reads
        self.assertIn("# HW Engineer", hw.body)

    def test_roles_without_levels_rank_in_file_order_from_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Software.md"
            path.write_text("# PM\n\nRuns the software swim lane end to end.\n\n# Lead\n\nLeads the developers day to day.\n", encoding="utf-8")
            sw = roles.parse_file(path, "configured")[0]
        self.assertEqual([(r.name, r.level) for r in sw.roles], [("PM", 2), ("Lead", 3)])

    def test_front_matter_member_wins_over_headings_and_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Money person.md"
            path.write_text("---\nmember: Finance\n---\n# One\n\n# Two\n", encoding="utf-8")
            profiles = roles.parse_file(path, "configured")
        self.assertEqual([p.member for p in profiles], ["Finance"])
        self.assertEqual([r.name for r in profiles[0].roles], ["One", "Two"])


class TestLoadRoles(unittest.TestCase):
    def test_roles_folder_is_detected_by_name_inside_the_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            make_roles(vault / "Roles&Responsibilities", ("Finance", "Legal"))
            b = roles.load_board({"knowledge": {"vault_path": str(vault)}})
            self.assertEqual(b.folder.name, "Roles&Responsibilities")
            self.assertEqual(b.source, "vault")
            self.assertEqual(knowledge.detect_roles_folder(vault).name, "Roles&Responsibilities")

    def test_empty_profiles_are_left_off_the_board_and_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r", ("Legal", "Customer"))
            (folder / "Finance.md").write_text("---\nmember: Finance\n---\n# Finance\n", encoding="utf-8")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertEqual(sorted(b.profiles), ["Customer", "Legal"])
        self.assertEqual(b.skipped, [("Finance", "not filled yet (Finance.md)")])
        self.assertEqual(roles.summary(b)["skipped"], [{"member": "Finance", "reason": "not filled yet (Finance.md)"}])

    def test_conduct_and_template_and_underscore_files_are_never_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r", ("Legal", "Customer"))
            roles.install_support_files(folder)
            (folder / "_notes.md").write_text("# Whatever\n\nLong enough text to count as content here.\n", encoding="utf-8")
            (folder / "Old.md").write_text("---\nkind: template\n---\n# Old\n\nLong enough text to count as content.\n", encoding="utf-8")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertEqual(sorted(b.profiles), ["Customer", "Legal"])

    def test_install_support_files_never_overwrites_and_creates_no_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "r"
            written = roles.install_support_files(folder)
            names = sorted(p.name for p in written)
            (folder / roles.CONDUCT_NAME).write_text("mine", encoding="utf-8")
            again = roles.install_support_files(folder)
            kept = (folder / roles.CONDUCT_NAME).read_text(encoding="utf-8")
            members = roles.members_in(folder)
        self.assertEqual(names, sorted([roles.CONDUCT_NAME, roles.TEMPLATE_NAME, roles.KPI_TEMPLATE_NAME]))
        self.assertEqual(again, [])
        self.assertEqual(kept, "mine")
        self.assertEqual(members, [])

    def test_configured_folder_defines_the_board_whatever_the_vault_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _single(vault / "Roles", "Finance")
            _single(vault / "Roles", "Legal")
            own = Path(tmp) / "MyBoard"
            _single(own, "Customer", order=2)
            _single(own, "Engineering", order=1)
            b = roles.load_board({"knowledge": {"vault_path": str(vault), "roles_folder": str(own)}})
            profiles = b.profiles
        self.assertEqual(list(profiles), ["Engineering", "Customer"])
        self.assertEqual(profiles["Customer"].source, "configured")
        self.assertEqual(roles.summary(b)["folder"], str(own))

    def test_fewer_than_two_members_is_an_error_not_a_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "one"
            _single(own, "Finance")
            with self.assertRaises(roles.RolesUnavailable) as raised:
                roles.load_roles({"knowledge": {"roles_folder": str(own)}})
            self.assertIn("at least 2", str(raised.exception))
            (own / "Finance.md").write_text("---\nmember: Finance\n---\n# Finance\n", encoding="utf-8")
            _single(own, "Legal")
            with self.assertRaises(roles.RolesUnavailable) as raised:
                roles.load_roles({"knowledge": {"roles_folder": str(own)}})
            self.assertIn("not filled yet: Finance", str(raised.exception))
            with self.assertRaises(roles.RolesUnavailable):
                roles.load_roles({"knowledge": {"roles_folder": str(Path(tmp) / "missing")}})

    def test_an_edit_is_read_on_the_next_load_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "r"
            path = _single(own, "Finance")
            _single(own, "Legal")
            config = {"knowledge": {"roles_folder": str(own)}}
            first = roles.load_roles(config)["Finance"].body
            path.write_text("---\nmember: Finance\n---\nChanged: now responsible for everything money-related in the programme.\n", encoding="utf-8")
            second = roles.load_roles(config)["Finance"].body
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("Changed:"))

    def test_colors_and_short_names_are_filled_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "r"
            _single(own, "Quality Assurance")
            _single(own, "Sales")
            profiles = roles.load_roles({"knowledge": {"roles_folder": str(own)}})
        self.assertTrue(all(p.color.startswith("#") for p in profiles.values()))
        self.assertEqual(profiles["Quality Assurance"].short, "Quality")
        self.assertEqual(profiles["Quality Assurance"].icon, "target")


class TestRolesInPrompts(unittest.TestCase):
    def _board(self, tmp):
        return roles.load_board({"knowledge": {"roles_folder": str(make_roles(Path(tmp) / "r"))}})

    def test_each_member_gets_the_conduct_note_and_only_its_own_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._board(tmp)
            finance = board._member_prompt("t", "c", (), (), "Finance", b.profiles["Finance"], b.conduct)
            manu = board._member_prompt("t", "c", (), (), "Manufacturing", b.profiles["Manufacturing"], b.conduct)
        self.assertIn("## Board member conduct", finance)
        self.assertIn("Speak for every role", finance)
        self.assertIn("## Role profile", finance)
        self.assertIn("speaks as its highest-ranked role, Finance (level 2)", finance)
        self.assertIn("- level 2: Finance", finance)
        self.assertIn("finance view of every decision", finance)
        self.assertNotIn("manufacturing view", finance)
        self.assertIn("manufacturing view of every decision", manu)

    def test_synthesis_gets_one_line_per_member_not_the_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self._board(tmp)
            prompt = board._synthesis_prompt([board.MemberAssessment("Finance", "v", "r", "rec")], b.profiles)
        self.assertIn("## Board members", prompt)
        self.assertIn("- Finance: Cost, budget vs forecast vs actuals, cBOM impact", prompt)
        self.assertNotIn("## Roles and responsibilities", prompt)

    def test_run_board_takes_its_members_from_the_roles_folder(self):
        class Recorder(AiProvider):
            def __init__(self): self.prompts = []
            def complete(self, task, prompt, on_text=None):
                self.prompts.append(prompt)
                text = json.dumps({"overall_recommendation": "x", "decisive_criterion": "y",
                                   "counter_arguments": [], "what_would_change_it": "", "disagreements": []}) \
                    if "## Assessments" in prompt else json.dumps({"view": "v", "risks": "r", "recommendation": "rec"})
                return AiResult(text=text, provider="f", model="m", input_tokens=1, output_tokens=1, duration_seconds=0)
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "r"
            _single(own, "Legal", order=1)
            _single(own, "Customer", order=2)
            _single(own, "Finance", order=3)
            config = {"provider": {"models": {"board": "f/m"}}, "knowledge": {"roles_folder": str(own)}}
            provider = Recorder()
            result = board.run_board(config, provider, topic="t")
        self.assertEqual([a.member for a in result.assessments], ["Legal", "Customer", "Finance"])
        self.assertEqual(result.llm_calls, 4)
        self.assertTrue(all("## Role profile" in p for p in provider.prompts if "Member: " in p))
        self.assertFalse(any("## Board member conduct" in p for p in provider.prompts))   # no conduct note written


class TestRolesAndKnowledge(unittest.TestCase):
    def test_a_roles_folder_inside_the_vault_is_not_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            _single(vault / "Roles", "Finance", "character tooling")
            _single(vault / "Roles", "Legal")
            (vault / "Some note.md").write_text("A note about tooling.\n", encoding="utf-8")
            selection = knowledge.gather({"knowledge": {"vault_path": str(vault)}}, "Finance character tooling")
        self.assertEqual(selection.relative_paths, ["Some note.md"])

    def test_a_configured_roles_folder_nested_deeper_in_the_vault_is_skipped_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            own = vault / "Meta" / "Board"
            _single(own, "Finance", "tooling")
            _single(own, "Legal")
            (vault / "Note.md").write_text("tooling\n", encoding="utf-8")
            selection = knowledge.gather({"knowledge": {"vault_path": str(vault), "roles_folder": str(own)}}, "tooling")
        self.assertEqual(selection.relative_paths, ["Note.md"])


class TestWizardRoles(unittest.TestCase):
    def test_wizard_creates_the_default_folder_with_support_files_and_says_the_board_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            out = io.StringIO()
            wizard = setup_wizard.Wizard(interactive=False, vault=str(vault), model="x/y", run_test=False, out=out)
            wizard.check_vault(str(vault))
            names = sorted(p.name for p in (vault / "Roles").glob("*.md"))
            members = roles.members_in(vault / "Roles")
        # non-interactive: support files plus the example board, so the board can run
        self.assertIn(roles.CONDUCT_NAME, names)
        self.assertIn(roles.TEMPLATE_NAME, names)
        self.assertIn("R&R Hardware.md", names)
        self.assertEqual(len(members), 8)
        self.assertEqual(wizard.roles_folder, str(vault / "Roles"))
        self.assertEqual(wizard.failures, [])
        self.assertIn("board of 8", out.getvalue())

    def test_wizard_finds_a_renamed_roles_folder_in_the_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            make_roles(vault / "Roles&Responsibilities", ("Legal", "Customer"))
            out = io.StringIO()
            wizard = setup_wizard.Wizard(interactive=False, vault=str(vault), model="x/y", run_test=False, out=out)
            wizard.check_vault(str(vault))
        self.assertEqual(wizard.roles_folder, str(vault / "Roles&Responsibilities"))
        self.assertIn("board of 2", out.getvalue())
        self.assertEqual(wizard.failures, [])

    def test_wizard_accepts_another_folder_and_writes_nothing_when_it_has_a_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"; vault.mkdir()
            own = Path(tmp) / "MyBoard"
            (own).mkdir()
            (own / "A.md").write_text("# A\n\nAlpha is responsible for the first half of everything that matters.\n", encoding="utf-8")
            (own / "B.md").write_text("# B\n\nBeta is responsible for the second half of everything that matters.\n", encoding="utf-8")
            before = (own / "A.md").read_text(encoding="utf-8")
            out = io.StringIO()
            wizard = setup_wizard.Wizard(interactive=False, vault=str(vault), model="x/y", run_test=False, out=out, roles=str(own))
            wizard.check_vault(str(vault))
            after = (own / "A.md").read_text(encoding="utf-8")
            files = sorted(p.name for p in own.glob("*.md"))
        self.assertEqual(before, after)
        self.assertEqual(files, ["A.md", "B.md"])
        self.assertEqual(wizard.roles_folder, str(own))
        self.assertIn("board of 2", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestCommonNotesAndAddenda(unittest.TestCase):
    """Common ground may be split across several notes, and an official role
    description may be extended without being edited (9 September 2026)."""

    def test_every_conduct_note_is_prepended_in_name_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r", ("Legal", "Customer"))
            (folder / "_Second note.md").write_text(
                "---\nkind: conduct\n---\n# More\n\nStart of production is March.\n", encoding="utf-8")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertEqual([p.name for p in b.conduct_paths],
                         ["_Board member conduct.md", "_Second note.md"])
        self.assertIn("Sceptical by default", b.conduct)
        self.assertIn("Start of production is March.", b.conduct)
        self.assertEqual(roles.summary(b)["conduct"], ["_Board member conduct.md", "_Second note.md"])

    def test_a_member_defined_twice_is_reported_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "r"
            folder.mkdir(parents=True)
            (folder / "A R&R Hardware.md").write_text(
                "---\nmember: Hardware\n---\n# Project Manager HW\nicon: chip\n\n"
                "Owns the hardware swim lane end to end, from concept to PPAP.\n", encoding="utf-8")
            (folder / "Z second hardware.md").write_text(
                "---\nmember: Hardware\n---\n# Something else\n\n"
                "A second file for the same member, which must not be merged in.\n", encoding="utf-8")
            _single(folder, "Legal")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertEqual(sorted(b.profiles), ["Hardware", "Legal"])
        self.assertEqual(b.profiles["Hardware"].title, "Project Manager HW")
        self.assertNotIn("must not be merged", b.profiles["Hardware"].body)
        self.assertEqual(b.skipped, [("Hardware", "defined twice - Z second hardware.md ignored, "
                                                  "A R&R Hardware.md used")])

    def test_the_shipped_conduct_note_covers_kpis_and_the_knowledge_network(self):
        text = (roles.SUPPORT_DIR / roles.CONDUCT_NAME).read_text(encoding="utf-8")
        self.assertIn("Align with the KPIs", text)
        self.assertIn("MG0", text)
        self.assertIn("Maturity Gate Zero", text)
        self.assertIn("Say when a number was recorded", text)
        self.assertIn("What you may decide", text)
        self.assertNotIn("cBOM", text)   # a member's own measures are in its own profile
        self.assertIn("Check the knowledge network first", text)
        self.assertIn("Obsidian", text)
        for name in ("Software", "Finance", "Systems", "Program Lead"):
            self.assertNotIn(name, text)   # no member's own material in a common note


class TestOffBoardAndLeadSwimlane(unittest.TestCase):
    def test_board_false_keeps_a_role_page_off_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r", ("Hardware", "Finance"))
            (folder / "R&R Account Management.md").write_text(
                "---\nkind: role\nlead_swimlane: Account Management\nboard: false\n---\n# Account Manager\n\n"
                "Receives change requests from the customer and negotiates price and tooling orders.\n", encoding="utf-8")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertEqual(sorted(b.profiles), ["Finance", "Hardware"])
        self.assertEqual(b.skipped, [])

    def test_the_checkbox_property_decides_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r", ("Hardware", "Finance"))
            (folder / "R&R Account Management.md").write_text(
                "---\nkind: role\nlead_swimlane: Account Management\nPart of Decision Board AI: false\n---\n"
                "# Account Manager\n\nReceives change requests from the customer and negotiates price and tooling orders.\n",
                encoding="utf-8")
            (folder / "R&R Quality.md").write_text(
                "---\nkind: role\nlead_swimlane: Quality\nPart of Decision Board AI: true\n---\n"
                "# Customer Group Quality\n\nOwns the customer quality indicators and the PPAP status of every part.\n",
                encoding="utf-8")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertEqual(sorted(b.profiles), ["Finance", "Hardware", "Quality"])

    def test_lead_swimlane_names_the_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r", ("Finance",))
            (folder / "Whatever.md").write_text(
                "---\nkind: role\nlead_swimlane: Hardware\nupdated: 2026-09-09\n---\n# Project Manager HW\nlevel: 2\n\n"
                "Owns the hardware swim lane end to end, from concept to PPAP.\n", encoding="utf-8")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        self.assertIn("Hardware", b.profiles)
        self.assertEqual(b.profiles["Hardware"].title, "Project Manager HW")


class TestReviewFixes(unittest.TestCase):
    def test_the_top_ranked_roles_metadata_counts_even_when_it_is_not_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_roles(Path(tmp) / "r", ("Finance",))
            (folder / "R&R Hardware.md").write_text(
                "---\nkind: role\nlead_swimlane: Hardware\n---\n# Junior\nlevel: 4\nperspective: junior view\n\n"
                "Junior responsibilities that fill the profile with enough text to count as a member.\n\n"
                "# Lead\nlevel: 1\nperspective: lead view\n\nLead responsibilities, also long enough to count.\n",
                encoding="utf-8")
            b = roles.load_board({"knowledge": {"roles_folder": str(folder)}})
        hw = b.profiles["Hardware"]
        self.assertEqual(hw.title, "Lead")
        self.assertEqual(hw.perspective, "lead view")

    def test_crlf_front_matter_is_stripped_cleanly(self):
        self.assertEqual(roles._strip_front_matter("---\r\nkind: role\r\n---\r\n# T\r\nbody"), "# T\r\nbody")
        self.assertEqual(roles._strip_front_matter("---\nkind: role\n---\n# T\nbody"), "# T\nbody")
