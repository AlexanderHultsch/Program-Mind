"""A demo server for the browser walk: a temporary vault, a fake model that
answers by the markers of each prompt, the real server on port 8765.
Run from anywhere: python tools/browser_walk/demo_server.py
"""
"""Starts the real server with a slow fake provider for a browser walk-through."""
import json, re, sys, tempfile, time, threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _roles_fixture import make_roles
from programmind.ai.provider import AiProvider, AiResult
from programmind.shell.server import create_http_server

tmp = Path(tempfile.mkdtemp())
vault = tmp / "vault"; (vault / "Suppliers").mkdir(parents=True)
(vault / "Suppliers" / "Housing tooling.md").write_text("---\ntitle: Housing tooling\ntags: [tooling]\nsummary: AI summary, not official. Supplier X's housing tooling runs six weeks late.\n---\nSupplier X tooling is 6 weeks late.\n")
(vault / "VPDS_Design Freeze.md").write_text("---\nkind: process\nphases: [MP3, MP4]\nlead_swimlane: Program Lead\naffected_swimlanes: [Program Lead, Mechanical Engineering, Manufacturing]\naliases: [DF]\nsummary: AI summary, not official. The design inputs for the DV build are frozen two weeks before tool kick-off.\n---\n# VPDS task - Design Freeze\n\n## Definition\n\nAll design inputs for the DV build are reviewed and frozen two weeks before tool kick-off.\n\n## Coaching\n\n" + ("The freeze date is baselined at MG0 and held even when the project slips. " * 40) + "\n")
make_roles(vault / "Roles&Responsibilities")
(vault / "Budget 2026.md").write_text("---\ntitle: Budget 2026\ntags: [finance]\nsummary: AI summary, not official. 180k EUR of rework budget is left for 2026.\n---\nRework budget left: 180k EUR.\n")
config_path = tmp / "config.local.json"
(vault / "Projects").mkdir()
(vault / "Projects" / "Dual DCDC.md").write_text("---\nkind: project\nprojects: [Dual DCDC]\n---\n# Dual DCDC\n\nSOP Aug 2028, customer Mercedes-Benz.\n")
(vault / "Projects" / "Sister.md").write_text("---\nkind: project\nprojects: [Sister]\n---\n# Sister\n\nA sister project with its own housing.\n")
(vault / "Lessons learned.md").write_text("# Lessons learned\n\n## Connector qualification\n\n" + ("The connector vendor change needed a second PPAP round. " * 30) + "\n\n## EMC chamber booking\n\n" + ("Book the EMC chamber twelve weeks ahead of DV. " * 30) + "\n\n## Firmware release notes\n\n" + ("Release notes must name the calibration set. " * 30) + "\n")
config = {"provider": {"models": {"board": "opencode/big-pickle"}}, "knowledge": {"vault_path": str(vault), "token_budget": 6000, "project": "Dual DCDC"}, "runtime": {"audit_folder": str(tmp / "audit")}, "server": {"history_folder": str(tmp / "history")}, "ask": {"token_budget": 12000}}
config_path.write_text(json.dumps(config))

class Slow(AiProvider):
    def complete(self, task, prompt, on_text=None):
        if "## Answer to check" in prompt:
            # The checker (spec 5.5): wants the lessons page once, then is content.
            time.sleep(0.8)
            wanted = [] if "Lessons learned.md" in prompt.split("## Pages read", 1)[1].split("## Table of contents", 1)[0] else ["Lessons learned.md"]
            text = json.dumps({"complete": not wanted, "read": wanted, "reasons": {p: "the EMC booking lead time bears on the freeze" for p in wanted},
                               "note": "The lessons page holds the EMC booking lead time." if wanted else "The pages read cover the question."})
        elif "## Question to the vault" in prompt:
            time.sleep(2.5)                       # long enough for the page to show the steps of the read
            later = "## Earlier in this thread" in prompt
            again = "Lessons learned.md" in prompt          # the whole vault is read (spec 5.6): the lessons page is there
            text = json.dumps({"answer": ("**Design freeze** is the milestone before the DV build.\n\n- The housing tooling at supplier X is six weeks late, so the freeze is at risk." + ("\n- Lessons learned: book the EMC chamber twelve weeks ahead of DV, or the freeze is followed by a test gap." if again else "\n- In general, not from the vault: a freeze slips when a supplier tool is not qualified.")) if not later else "The Program Lead approves it, with the swim lane project managers.\n\n- Nothing in the vault names a date for this project.",
                               "sources": [{"path": "Suppliers/Housing tooling.md", "heading": "", "why": "the tooling delay"}, {"path": "Projects/Dual DCDC.md", "heading": "", "why": "the project page"}, {"path": "Invented/Page.md", "heading": "", "why": "made up"}]
                                          + ([{"path": "Lessons learned.md", "heading": "EMC chamber booking", "why": "the booking lead time"}] if again else []),
                               "gaps": ["the date of the design freeze for this project"] if not later else [],
                               "missing": [], "decision_question": later})
        elif "## Candidate sections" in prompt:
            # The choice from the table of contents (spec 5.3): the pages whose line shares a
            # word with the question, the tooling page always, plus the summaries of two more.
            time.sleep(1.2)
            lines = prompt.splitlines()
            members = [line[4:].strip() for line in lines if line.startswith("### ")]
            question = prompt.split("## Question", 1)[1].split("##", 1)[0].lower()
            rows = [line.split("- id: ", 1)[1] for line in lines if line.startswith("- id: ")]
            ids = [row.split(" | ", 1)[0] for row in rows]
            words = [w for w in re.findall(r"[a-z]{5,}", question)]
            chosen = [row.split(" | ", 1)[0] for row in rows if any(w in row.lower() for w in words)]
            chosen += [i for i in ids if "tooling" in i.lower() and i not in chosen]
            chosen = chosen[:6] or ids[:2]
            text = json.dumps({"members": [{"member": m, "read": chosen,
                                            "reasons": {i: (f"{m} needs the tooling delay to judge the SOP risk" if "tooling" in i.lower()
                                                            else f"{m}: this page bears on the question") for i in chosen}}
                                           for m in members]})
        elif "## Question from Alex" in prompt and "## Clarification so far" in prompt:
            time.sleep(0.8)
            if "Round 2" in prompt:
                text = json.dumps({"topic": "Rework the Gen6 housing tooling or switch supplier", "context": "SOP is fixed for March. Supplier X tooling is six weeks late. Supplier Y needs 10 weeks. The customer accepts a cosmetic waiver.", "options": ["Rework at supplier X", "Switch to supplier Y"], "constraints": ["SOP date cannot move"], "questions": [], "clear": True})
            else:
                text = json.dumps({"topic": "Rework the Gen6 housing tooling or switch supplier", "context": "SOP is fixed for March. Supplier X tooling is six weeks late. Supplier Y needs 10 weeks.", "options": ["Rework at supplier X", "Switch to supplier Y"], "constraints": ["SOP date cannot move"], "questions": ["Is the 180k EUR rework budget already approved, or does it need a change request?"], "clear": False})
        elif "## Members to assess" in prompt or "## Members to ask again" in prompt:
            time.sleep(1.5)
            names = [line.split("### Member: ", 1)[1].strip() for line in prompt.splitlines() if line.startswith("### Member: ")]
            entries = [{"member": n, "applies": n != "SW Engineering", "view": f"- {n}: combined view, rework keeps SOP.", "impact": ["late tooling -> SOP at risk"], "risks": ["Supplier X slips again"], "recommendation": "- Rework at supplier X.", "facts_from_network": [{"fact": "Supplier X tooling is 6 weeks late", "source": "Suppliers/Housing tooling.md"}], "own_judgement": ["Supplier Y needs about 10 weeks"]} for n in names]
            text = json.dumps({"members": entries, "synthesis": {"overall_recommendation": "Rework at supplier X (combined).", "decisive_criterion": "- Time to SOP.", "counter_arguments": ["Unit cost"], "what_would_change_it": "- A firm quote from Y.", "disagreements": [], "not_affected": ["SW Engineering: no software content"], "rests_on_judgement": ["Manufacturing: Y needs 10 weeks"]}, "follow_up": {"answer": "- Combined follow-up answer.", "reasons": ["Finance: cost"], "recommendation_now": "Rework, unchanged.", "disagreements": []}})
        elif "## Your earlier assessment" in prompt:
            member = prompt.split("Member: ", 1)[1].splitlines()[0]
            time.sleep(1.0)
            text = json.dumps({"applies": True, "view": f"- {member}: free rework changes the money side, not the time side.\n- The 10-week lead time still lands after SOP.", "risks": ["Free today can mean a claim later"], "recommendation": "- Still rework at supplier X."})
        elif "## Question from Alex" in prompt:
            time.sleep(0.8); text = json.dumps({"topic": "Rework the Gen6 housing tooling or switch supplier", "context": "SOP is fixed for March. Supplier X tooling is six weeks late [Suppliers/Housing tooling.md]. Rework budget left is 180k EUR [Budget 2026.md].", "options": ["Rework the existing tooling at supplier X", "Switch to supplier Y with new tooling"], "constraints": ["SOP date cannot move", "No additional budget beyond 180k EUR"], "questions": ["Has supplier Y quoted a lead time for new tooling, and does it fit before SOP?", "Would the customer accept a first-batch waiver on the housing cosmetic spec?"]})
        elif "## Vault outline" in prompt:
            time.sleep(0.8); text = json.dumps({"path": "Suppliers/Housing tooling.md", "title": "Decision: rework at supplier X", "tags": ["tooling", "gen6"], "body": "## Decision\n\nRework the existing tooling at supplier X.\n\n**Decisive criterion:** time to SOP.\n\n**Left standing:** Finance prefers switching for the lower unit cost; Manufacturing rates the ramp-up risk of a new supplier as too high before SOP.\n\n**Would change it:** a firm quote from supplier Y with tooling ready 8 weeks before SOP.\n\nSee [[Budget 2026]]."})
        elif "## New question" in prompt:
            time.sleep(0.8); text = json.dumps({"answer": "- Time to SOP decides it, not cost.\n- A rework that is on time beats a switch that is cheaper but late.", "reasons": ["Manufacturing: ramp-up risk with a new supplier before SOP", "Finance: unit-cost saving of 4% does not cover a missed SOP"], "recommendation_now": "Rework at supplier X, unchanged.", "disagreements": ["Finance still prefers switching."]})
        elif "## Assessments" in prompt:
            time.sleep(1.2); text = json.dumps({"overall_recommendation": "Rework the existing tooling at supplier X, with a weekly gate on the rework plan.", "decisive_criterion": "- Time to SOP.\n- A new supplier cannot deliver qualified tooling before March.", "counter_arguments": ["Switching lowers unit cost by an estimated 4%.", "Supplier X has already missed one date."], "what_would_change_it": "- A firm quote from supplier Y with qualified tooling eight weeks before SOP.", "not_affected": ["SW Engineering: no software content in the housing"], "disagreements": ["Finance recommends switching for the unit-cost saving; Manufacturing and HW Engineering rate the ramp-up risk as unacceptable before SOP."], "rests_on_judgement": ["Manufacturing: supplier Y needs about 10 weeks for new tooling", "Finance: a second rework iteration costs a mid five-figure sum"]})
        elif "Member: " in prompt:
            member = prompt.split("Member: ", 1)[1].splitlines()[0]
            delays = {"Finance": 0.6, "HW Engineering": 1.4, "Mechanical Engineering": 1.0, "Manufacturing": 2.2, "SW Engineering": 0.4, "KPI Check": 1.8}
            time.sleep(delays.get(member, 1))
            if member == "SW Engineering":
                text = json.dumps({"applies": False, "view": "- No software content changes with the housing tooling.\n- None of my measures (release dates, defect counts) moves either way.", "risks": [], "recommendation": "- No position: not affected."})
            else:
                text = json.dumps({"applies": True, "view": f"- {member}: rework is the only option that keeps SOP.\n- The late tooling is a schedule problem, not a design problem.", "impact": ["Switching -> new tooling at supplier Y -> 10 weeks lead time lands after SOP", "Rework -> 180k budget used up -> no reserve for a second iteration"], "risks": ["Supplier X slips again", "Rework consumes the remaining budget"], "recommendation": "- Rework at supplier X.\n- Put a weekly gate on the rework plan.", "facts_from_network": [{"fact": "Supplier X tooling is 6 weeks late", "source": "Suppliers/Housing tooling.md"}, {"fact": "Rework budget left is 180k EUR", "source": "Budget 2026.md"}, {"fact": "MG3 is on 12 March", "source": "Timing plan.md"}], "own_judgement": ["A second rework iteration would cost a mid five-figure sum", "Supplier Y needs about 10 weeks for new tooling"]})
        else:
            text = "{}"
        if on_text is not None and "## Question to the vault" in prompt:
            for cut in range(10, len(text), max(1, len(text) // 12)):     # as the server mode streams (spec 4.1)
                time.sleep(0.12)
                on_text(text[:cut])
            on_text(text)
        return AiResult(text=text, provider="opencode", model="big-pickle", input_tokens=8025, output_tokens=200, duration_seconds=1)

httpd, _ = create_http_server(config, config_path, port=8765, provider=Slow())
print("ready", flush=True)
httpd.serve_forever()
