#!/usr/bin/env python3
"""End-to-end tests for the browser interface's API (spec section 10): a
real ``http.server`` on a free local port, the model mocked at the
``AiProvider`` boundary, the flow driven exactly as the page drives it."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

sys.path.insert(0, str(REPO_ROOT / "tests"))
from programmind import __version__  # noqa: E402
from programmind.ai.provider import AiProvider, AiResult  # noqa: E402
from programmind.shell.server import Session, _ranking_query, create_http_server, site_host, site_name  # noqa: E402
from programmind.memory import history as history_mod  # noqa: E402
from programmind.knowledge import knowledge as knowledge_mod  # noqa: E402
from programmind.agents.board import clarify as clarify_mod  # noqa: E402
from _roles_fixture import CLASSIC, make_roles  # noqa: E402

MEMBER_COUNT = len(CLASSIC)

CLARIFIER = json.dumps({"topic": "Rework or switch", "context": "SOP is fixed.", "options": ["Rework", "Switch"],
                        "constraints": ["SOP cannot move"], "questions": ["What is the budget?"]})
SYNTHESIS = json.dumps({"overall_recommendation": "Rework", "decisive_criterion": "Time", "counter_arguments": ["Cost"],
                        "what_would_change_it": "A quote", "disagreements": ["Finance vs Manufacturing"]})
PROPOSAL = json.dumps({"path": "Decisions/Rework.md", "title": "Rework decision", "tags": ["tooling"], "body": "Decided: rework."})
CLEAR = json.dumps({"topic": "Rework or switch", "context": "SOP is fixed. Budget 200k.", "options": ["Rework", "Switch"],
                    "constraints": ["SOP cannot move"], "questions": [], "clear": True})
FOLLOW_UP = json.dumps({"answer": "- Because time is the decisive criterion.", "reasons": ["Hardware: DV date"],
                        "recommendation_now": "Rework, unchanged.", "disagreements": []})
# Ask the vault (spec 10): one source that was sent, one that was not - Python must drop the second.
ASK = json.dumps({"answer": "- Tooling is late.",
                  "sources": [{"path": "Tooling.md", "heading": "", "why": "says so"},
                              {"path": "Invented.md", "heading": "", "why": "made up"}],
                  "gaps": ["the new date"], "decision_question": False})


def fake_choice(prompt: str) -> str:
    """What a model would choose from the table of contents (spec 5.3), as
    far as a fake can: the sections whose line shares a word of five letters
    or more with the question, the first section when none does. Every
    member listed gets the same choice, with a reason each."""
    lines = prompt.splitlines()
    members = [line[4:].strip() for line in lines if line.startswith("### ")]
    question = prompt.split("## Question", 1)[1].split("##", 1)[0] if "## Question" in prompt else prompt
    words = {w for w in re.findall(r"[a-zA-Z]{5,}", question.lower())}
    rows = [line.split("- id: ", 1)[1] for line in lines if line.startswith("- id: ")]
    ids = [row.split(" | ", 1)[0] for row in rows]
    hits = [row.split(" | ", 1)[0] for row in rows if any(w in row.lower() for w in words)]
    chosen = hits or ids[:1]
    return json.dumps({"members": [{"member": m, "read": chosen, "reasons": {i: f"{m} needs it" for i in chosen}}
                                   for m in members]})


def ask_and_read(test, thread_id, question, port=None, **read_body):
    """Spec 5.3: a question stops at the picks screen; reading is a second
    step. Returns the thread once the answer is in."""
    call = (lambda m, path, body=None: test.call(m, path, body, port=port)) if port else test.call
    status, state = call("POST", f"/api/ask/{thread_id}/question", {"question": question})
    if status != 200:
        return status, state
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, state = call("GET", f"/api/ask/{thread_id}")
        if state["phase"] in ("picks", "idle") and not state["busy"]:
            break
        time.sleep(0.02)
    if state["phase"] == "picks":
        status, state = call("POST", f"/api/ask/{thread_id}/read", read_body)
        if status != 200:
            return status, state
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _, state = call("GET", f"/api/ask/{thread_id}")
        if not state["busy"] and state["phase"] not in ("asking", "choosing", "checking", "reading_more", "proposing"):
            return 200, state
        time.sleep(0.02)
    raise AssertionError(f"thread still busy in {state['phase']}")


class RoutingFakeProvider(AiProvider):
    """Answers by what the prompt is for - the markers each prompt builder
    puts in - so one provider serves the whole flow."""

    def __init__(self):
        self.prompts: list[str] = []
        self.lock = threading.Lock()
        self.pick_answer: str | None = None     # a fixed answer to the pick prompt, for the fallback test
        self.ask_answer: str | None = None      # a fixed first answer of Ask the vault
        self.second_answer: str | None = None   # ... and the answer of every later round (spec 5.5)
        self.check_answers: list[str] = []      # the checker's answers in order; content ("complete") when they run out
        self.delay = 0.0                        # seconds every call sleeps, for the "busy" tests

    def complete(self, task: str, prompt: str, on_text=None) -> AiResult:
        with self.lock:
            self.prompts.append(prompt)
        if on_text is not None and "## Question to the vault" in prompt:
            later = "## Your answer so far" in prompt          # spec 4.1: the answer as it is written
            on_text(((self.second_answer if later else None) or self.ask_answer or ASK)[:20])
        if on_text is not None and "Member: " in prompt and "## Your earlier assessment" not in prompt:
            member = prompt.split("Member: ", 1)[1].splitlines()[0]         # spec 5.8: the member writes on the screen
            on_text('{"view": "' + member + ' is thinking')
        if self.delay:
            time.sleep(self.delay)
        if "## Answer to check" in prompt:
            with self.lock:
                text = self.check_answers.pop(0) if self.check_answers else json.dumps({"complete": True, "read": [], "note": ""})
        elif "## Your answer so far" in prompt:
            text = self.second_answer or self.ask_answer or ASK
        elif "## Question to the vault" in prompt:
            text = self.ask_answer or ASK
        elif "## Candidate sections" in prompt:
            if self.pick_answer is not None:
                text = self.pick_answer
            else:
                text = fake_choice(prompt)
        elif "## Question from Alex" in prompt:
            text = CLEAR if "## Clarification so far" in prompt else CLARIFIER
        elif "## Members to assess" in prompt or "## Members to ask again" in prompt:
            names = [line.split("### Member: ", 1)[1].strip() for line in prompt.splitlines() if line.startswith("### Member: ")]
            entries = [{"member": n, "applies": True, "view": f"- {n} combined view", "risks": ["r"], "recommendation": "- Rework",
                        "facts_from_network": [{"fact": "Tooling is late", "source": "Tooling.md"}]} for n in names]
            text = json.dumps({"members": entries, "synthesis": json.loads(SYNTHESIS),
                               "follow_up": {"answer": "- Combined follow-up.", "reasons": [], "recommendation_now": "Rework.", "disagreements": []}})
        elif "## Vault outline" in prompt:
            text = PROPOSAL
        elif "## Your earlier assessment" in prompt:
            member = prompt.split("Member: ", 1)[1].splitlines()[0]
            text = json.dumps({"applies": member != "Finance", "view": f"- {member} again",
                               "risks": ["r2"], "recommendation": "- Rework, still"})
        elif "## New question" in prompt:
            text = FOLLOW_UP
        elif "## Assessments" in prompt:
            text = SYNTHESIS
        elif "Member: " in prompt:
            member = prompt.split("Member: ", 1)[1].splitlines()[0]
            text = json.dumps({"view": f"{member} view", "risks": ["r1"], "recommendation": "Rework",
                               "facts_from_network": [{"fact": "Tooling is late", "source": "Tooling.md"},
                                                      {"fact": "MG3 is in March", "source": "Gates.md"}],
                               "own_judgement": ["a new supplier needs 10 weeks"]})
        else:
            text = "{}"
        return AiResult(text=text, provider="fake", model="m", input_tokens=1, output_tokens=1, duration_seconds=0)


class TestServerFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.vault = Path(cls.tmp.name) / "vault"
        cls.vault.mkdir()
        (cls.vault / "Tooling.md").write_text("---\ntitle: Tooling\ntags: [tooling]\n---\nTooling is late.\n", encoding="utf-8")
        make_roles(cls.vault / "Roles&Responsibilities")
        cls.config_path = Path(cls.tmp.name) / "config.local.json"
        cls.config = {"provider": {"models": {"board": "fake/m"}},
                      # A ceiling below this vault keeps the per-member ranking of 5.1 in force here;
                      # the whole-vault board of 5.8 has tests of its own.
                      "knowledge": {"vault_path": str(cls.vault), "token_budget": 6000, "selection": "python",
                                    "max_read_tokens": 15060}}   # 60 of pages under the reserve of 5.9
        cls.config_path.write_text(json.dumps(cls.config), encoding="utf-8")
        cls.provider = RoutingFakeProvider()
        cls.httpd, cls.board_server = create_http_server(cls.config, cls.config_path, port=0, provider=cls.provider)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def wait_for(self, session_id, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, state = self.call("GET", f"/api/sessions/{session_id}")
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail(f"timed out waiting; last phase {state['phase']} error {state['error']}")

    def test_index_and_static_files_are_served(self):
        for path in ("/", "/board", "/board/abc123", "/ask", "/ask/abc123", "/archive", "/privacy", "/about"):
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertIn(b"Program Mind", response.read())
        for name in ("shell.js", "style.css", "agents/board/board.js", "agents/ask/ask.js"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/{name}", timeout=10) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/agents/board/../../shell/server.py", timeout=10)
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/static/../server.py")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(request, timeout=10)

    def test_the_menu_pages_have_what_they_name(self):
        """Spec 11.1, decision 12: the version, the site address and the
        specification, which is served from this machine so About works
        without the internet."""
        _, view = self.call("GET", "/api/config")
        self.assertEqual(view["version"], __version__)
        self.assertTrue(view["repository"].startswith("https://github.com/"))
        self.assertTrue(view["spec"])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/spec", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["Content-Type"].startswith("text/plain"))
            self.assertIn(b"## 11. Program Mind: the app shell", response.read())

    def test_project_round_trips_and_the_vault_projects_are_listed(self):
        (self.vault / "Dual DCDC.md").write_text("---\nkind: project\n---\nThe project.\n", encoding="utf-8")
        try:
            status, view = self.call("POST", "/api/config", {"project": "Dual DCDC"})
            self.assertEqual(status, 200)
            self.assertEqual(view["project"], "Dual DCDC")
            self.assertIn("Dual DCDC", view["projects"])
            self.assertEqual(self.config["knowledge"]["project"], "Dual DCDC")
        finally:
            self.call("POST", "/api/config", {"project": ""})
            (self.vault / "Dual DCDC.md").unlink()

    def test_config_is_read_and_written(self):
        status, view = self.call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(view["model"], "fake/m")
        self.assertTrue(view["knowledge_status"]["ok"])
        self.assertEqual(view["knowledge_status"]["notes"], 1)
        status, view = self.call("POST", "/api/config", {"token_budget": 3000, "theme": "dark"})
        self.assertEqual(status, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["knowledge"]["token_budget"], 3000)
        self.assertEqual(saved["ui"]["theme"], "dark")
        self.call("POST", "/api/config", {"token_budget": 6000})

    def test_blank_question_is_rejected(self):
        status, body = self.call("POST", "/api/sessions", {"question": "  "})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_full_flow_from_question_to_written_note(self):
        first_prompt = len(self.provider.prompts)   # other tests share the provider
        status, state = self.call("POST", "/api/sessions", {"question": "Rework the tooling or switch supplier?"})
        self.assertEqual(status, 201)
        sid = state["id"]

        state = self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.assertEqual(state["clarification"]["questions"], ["What is the budget?"])
        self.assertEqual(state["knowledge"]["selected"], 1)
        self.assertEqual(state["knowledge"]["notes"], ["Tooling.md"])
        clarifier_prompt = [p for p in self.provider.prompts[first_prompt:] if "## Question from Alex" in p][0]
        self.assertIn("Tooling is late.", clarifier_prompt)

        status, state = self.call("POST", f"/api/sessions/{sid}/answers", {"answers": ["200k"]})
        self.assertEqual(status, 200)
        self.assertIn(state["phase"], ("clarifying", "confirm"))   # the clarifier looks again (and may already be done)
        state = self.wait_for(sid, lambda s: s["phase"] == "confirm")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(state["llm_calls"], 2)                  # two clarifier calls
        self.assertIn("Q: What is the budget?\nA: 200k", state["inputs"]["context"])
        self.assertIn("Budget 200k.", state["inputs"]["context"])   # the second round's context won
        self.assertEqual(sorted(state["roles"]["members"]), sorted(CLASSIC))

        chosen = CLASSIC[:3]
        status, state = self.call("POST", f"/api/sessions/{sid}/run", {
            "topic": "Rework or switch (edited)", "context": state["inputs"]["context"],
            "options": ["Rework", "Switch"], "constraints": ["SOP cannot move"], "members": chosen})
        self.assertEqual(status, 200)
        state = self.wait_for(sid, lambda s: s["phase"] == "result")
        self.assertEqual(state["result"]["topic"], "Rework or switch (edited)")
        self.assertEqual(sorted(state["selected_members"]), sorted(chosen))
        self.assertEqual(sorted(a["member"] for a in state["result"]["assessments"]), sorted(chosen))
        self.assertEqual(state["result"]["synthesis_data"]["overall_recommendation"], "Rework")
        self.assertEqual(state["result"]["sources"]["network"], ["Tooling.md"])   # the note the board sent
        self.assertEqual(len(state["result"]["sources"]["unverified"]), len(chosen))   # Gates.md was never sent
        self.assertEqual(state["result"]["assessments"][0]["sources"][0]["verified"], True)
        self.assertEqual(state["result"]["assessments"][0]["sources"][1]["verified"], False)
        self.assertEqual(sorted(state["members"]), sorted(chosen))
        self.assertTrue(all(v == "done" for v in state["members"].values()))
        self.assertEqual(sorted(state["partial"]), sorted(chosen))     # the early answers, one per member
        self.assertEqual(state["partial"][chosen[0]]["view"], f"{chosen[0]} view")
        self.assertEqual(state["llm_calls"], 2 + len(chosen) + 1)   # clarifier x2 + members + synthesis

        # Members received the knowledge and the clarification, never the clarifier's questions as a task.
        member_prompts = [p for p in self.provider.prompts[first_prompt:] if "Member: " in p]
        self.assertEqual(len(member_prompts), len(chosen))
        self.assertTrue(all("Tooling is late." in p for p in member_prompts))
        self.assertTrue(all("A: 200k" in p for p in member_prompts))
        self.assertFalse(any("## Question from Alex" in p for p in member_prompts))

        calls_before = state["llm_calls"]
        status, state = self.call("POST", f"/api/sessions/{sid}/follow-up", {"question": "Why?"})
        self.assertEqual(status, 200)
        state = self.wait_for(sid, lambda s: not s["busy"])
        self.assertIn("Because time is the decisive criterion.", state["turns"][0]["answer"])
        self.assertEqual(state["turns"][0]["data"]["recommendation_now"], "Rework, unchanged.")
        self.assertEqual(state["turns"][0]["assessments"], [])
        self.assertEqual(state["llm_calls"], calls_before + 1)     # one call: nobody asked again

        asked_again = chosen[:2]
        status, state = self.call("POST", f"/api/sessions/{sid}/follow-up",
                                  {"question": "And if tooling is free?", "members": asked_again})
        self.assertEqual(status, 200)
        state = self.wait_for(sid, lambda s: not s["busy"])
        turn = state["turns"][1]
        self.assertEqual(sorted(a["member"] for a in turn["assessments"]), sorted(asked_again))
        self.assertEqual(state["llm_calls"], calls_before + 1 + len(asked_again) + 1)
        again_prompts = [p for p in self.provider.prompts[first_prompt:] if "## Your earlier assessment" in p]
        self.assertEqual(len(again_prompts), len(asked_again))
        self.assertTrue(all("## Role profile" in p and "And if tooling is free?" in p for p in again_prompts))
        finance = [a for a in turn["assessments"] if a["member"] == "Finance"]
        if finance:
            self.assertFalse(finance[0]["applies"])

        status, state = self.call("POST", f"/api/sessions/{sid}/close", {"remember": True})
        self.assertEqual(status, 200)
        state = self.wait_for(sid, lambda s: s["phase"] == "proposal")
        self.assertEqual(state["proposal"]["path"], "Decisions/Rework.md")
        self.assertEqual(state["proposal"]["mode"], "create")
        self.assertIn("Decided: rework.", state["proposal"]["preview"])
        self.assertFalse((self.vault / "Decisions" / "Rework.md").exists())   # nothing written yet

        status, state = self.call("POST", f"/api/sessions/{sid}/memory", {
            "path": "Decisions/Rework.md", "title": "Rework decision", "tags": ["tooling"], "body": "Decided: rework (edited)."})
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "written")
        written = (self.vault / "Decisions" / "Rework.md").read_text(encoding="utf-8")
        self.assertIn("Decided: rework (edited).", written)
        self.assertIn("tags: [tooling, decision-board]", written)

    def test_close_without_remembering_writes_nothing(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})   # straight to the board
        state = self.wait_for(sid, lambda s: s["phase"] == "confirm")
        self.assertEqual(state["llm_calls"], 1)
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Anything", "context": "", "options": [], "constraints": []})
        self.wait_for(sid, lambda s: s["phase"] == "result")
        status, state = self.call("POST", f"/api/sessions/{sid}/close", {"remember": False})
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "closed")

    def test_unreachable_vault_is_an_error_not_a_silent_run(self):
        original = self.config["knowledge"]["vault_path"]
        self.config["knowledge"]["vault_path"] = str(Path(self.tmp.name) / "missing")
        try:
            _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
            state = self.wait_for(state["id"], lambda s: s["phase"] == "error")
            self.assertIn("knowledge source not found", state["error"])
            self.assertIn("Options", state["error"])
        finally:
            self.config["knowledge"]["vault_path"] = original

    def test_the_round_limit_sends_the_question_to_the_board(self):
        keep = self.provider.complete
        # A clarifier that never finds the question clear.
        self.provider.complete = lambda task, prompt, on_text=None: (
            AiResult(text=CLARIFIER, provider="fake", model="m", input_tokens=1, output_tokens=1, duration_seconds=0)
            if "## Question from Alex" in prompt else keep(task, prompt))
        try:
            _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
            sid = state["id"]
            for round_number in range(1, clarify_mod.MAX_ROUNDS + 1):
                state = self.wait_for(sid, lambda s: s["phase"] in ("questions", "confirm"))
                if state["phase"] == "confirm":
                    break
                self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [f"answer {round_number}"]})
            state = self.wait_for(sid, lambda s: s["phase"] == "confirm")
            self.assertEqual(len(state["rounds"]), state["max_rounds"])
            self.assertIn("A: answer 3", state["inputs"]["context"])
        finally:
            self.provider.complete = keep

    def test_choosing_no_known_member_is_refused(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        status, body = self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Anything", "members": ["Nobody"]})
        self.assertEqual(status, 400)
        self.assertIn("at least one member", body["error"])

    def test_combined_mode_is_one_call_for_the_board_and_one_for_a_follow_up(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
        state = self.wait_for(sid, lambda s: s["phase"] == "confirm")
        before = state["llm_calls"]
        chosen = CLASSIC[:3]
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Anything", "members": chosen, "mode": "combined"})
        state = self.wait_for(sid, lambda s: s["phase"] == "result")
        self.assertEqual(state["mode"], "combined")
        self.assertEqual(state["result"]["mode"], "combined")
        self.assertEqual(state["llm_calls"], before + 1)
        self.assertEqual(sorted(a["member"] for a in state["result"]["assessments"]), sorted(chosen))
        self.assertEqual(state["result"]["sources"]["network"], ["Tooling.md"])
        self.call("POST", f"/api/sessions/{sid}/follow-up", {"question": "Why?", "members": chosen[:2], "mode": "combined"})
        state = self.wait_for(sid, lambda s: not s["busy"])
        self.assertEqual(state["llm_calls"], before + 2)
        self.assertEqual(state["turns"][0]["mode"], "combined")
        self.assertEqual(sorted(a["member"] for a in state["turns"][0]["assessments"]), sorted(chosen[:2]))
        self.assertIn("Combined follow-up.", state["turns"][0]["answer"])

    def test_the_estimate_counts_exact_calls_and_learns_the_overhead_from_real_calls(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Rework the tooling?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        chosen = CLASSIC[:3]
        status, est = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": chosen, "mode": "individual", "budget": 2000})
        self.assertEqual(status, 200)
        self.assertEqual(est["calls"], 4)                                  # three members plus the synthesis
        self.assertEqual(est["overhead_learned_from"], 1)                   # the clarifier call is the only real one so far
        self.assertEqual(est["overhead_per_call"], 0)                       # the fake reports 1 token in: overhead floored at 0
        self.assertEqual(sorted(est["members"]), sorted(chosen))
        self.assertIn("Tooling.md", est["members"][chosen[0]])
        self.assertGreater(est["tokens_in"], 4 * 1000)                      # four prompts of a few thousand characters
        status, combined = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": chosen, "mode": "combined", "budget": 2000})
        self.assertEqual(combined["calls"], 1)
        self.assertLess(combined["tokens_in"], est["tokens_in"])
        status, none = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": chosen, "budget": 0})
        self.assertEqual(none["members"][chosen[0]], [])
        # after a real run the overhead is learned from the recorded calls (the fake reports 1 token in)
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Rework?", "members": chosen, "budget": 2000})
        state = self.wait_for(sid, lambda s: s["phase"] == "result")
        self.assertEqual(state["budget"], 2000)
        self.assertEqual(sorted(state["member_knowledge_paths"]), sorted(chosen))
        self.call("POST", f"/api/sessions/{sid}/back")
        status, est2 = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": chosen, "budget": 2000})
        self.assertGreater(est2["overhead_learned_from"], 1)               # the members and the synthesis were recorded too
        self.assertEqual(est2["overhead_per_call"], 0)

    def test_projects_come_from_the_home_page_and_scope_the_notes(self):
        (self.vault / "Sister.md").write_text("---\nkind: project\nprojects: [Sister]\n---\nSister project tooling note.\n", encoding="utf-8")
        (self.vault / "Dual.md").write_text("---\nkind: project\nprojects: [Dual DCDC]\n---\nDual project tooling note.\n", encoding="utf-8")
        try:
            _, state = self.call("POST", "/api/sessions", {"question": "Tooling?", "projects": ["Dual DCDC"]})
            sid = state["id"]
            self.assertEqual(state["projects"], ["Dual DCDC"])
            state = self.wait_for(sid, lambda s: s["phase"] == "questions")
            self.assertIn("Dual.md", state["knowledge"]["notes"])
            self.assertNotIn("Sister.md", state["knowledge"]["notes"])
            _, both = self.call("POST", "/api/sessions", {"question": "Tooling?", "projects": ["Dual DCDC", "Sister"]})
            both = self.wait_for(both["id"], lambda s: s["phase"] == "questions")
            self.assertIn("Sister.md", both["knowledge"]["notes"])
            self.assertIn("Dual.md", both["knowledge"]["notes"])
        finally:
            (self.vault / "Sister.md").unlink(); (self.vault / "Dual.md").unlink()

    def test_manual_picks_are_sent_on_top_of_the_budget_and_exclusions_never(self):
        (self.vault / "Manual.md").write_text("# Manual\n\nA note nobody would rank for this question.\n", encoding="utf-8")
        try:
            _, state = self.call("POST", "/api/sessions", {"question": "Rework the tooling?"})
            sid = state["id"]
            self.wait_for(sid, lambda s: s["phase"] == "questions")
            self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
            self.wait_for(sid, lambda s: s["phase"] == "confirm")
            chosen = CLASSIC[:2]
            _, plain = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": chosen, "budget": 40, "outline": True})
            self.assertTrue(any(n["path"] == "Manual.md" for n in plain["outline"]))
            self.assertNotIn("Manual.md", plain["members"][chosen[0]])           # a 40-token budget: Tooling.md only
            _, picked = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": chosen, "budget": 40, "extra": ["Manual.md"], "exclude": ["Tooling.md"]})
            self.assertIn("Manual.md", picked["members"][chosen[0]])
            self.assertNotIn("Tooling.md", picked["members"][chosen[0]])
            self.assertGreater(picked["forced_tokens"], 0)
            self.assertTrue(any(s["forced"] for s in picked["sections"][chosen[0]]))
            self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Rework?", "members": chosen, "budget": 40, "extra": ["Manual.md"], "exclude": ["Tooling.md"]})
            state = self.wait_for(sid, lambda s: s["phase"] == "result")
            self.assertEqual(state["extra"], ["Manual.md"])
            self.assertEqual(state["member_knowledge_paths"][chosen[0]], ["Manual.md"])
        finally:
            (self.vault / "Manual.md").unlink()

    def test_a_member_not_asked_at_first_can_be_asked_in_a_follow_up(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Anything", "members": CLASSIC[:2]})
        self.wait_for(sid, lambda s: s["phase"] == "result")
        later = CLASSIC[2]
        first_prompt = len(self.provider.prompts)
        self.call("POST", f"/api/sessions/{sid}/follow-up", {"question": "And you?", "members": [later]})
        state = self.wait_for(sid, lambda s: not s["busy"])
        self.assertEqual([a["member"] for a in state["turns"][0]["assessments"]], [later])
        prompt = [p for p in self.provider.prompts[first_prompt:] if f"Member: {later}" in p][0]
        self.assertIn("you did not produce an assessment in the first round", prompt)
        self.assertIn("Tooling is late.", prompt)               # its knowledge block was built on demand

    def test_back_returns_to_the_questions_with_the_answers_kept(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": ["my answer"], "final": True})
        state = self.wait_for(sid, lambda s: s["phase"] == "confirm")
        status, state = self.call("POST", f"/api/sessions/{sid}/back")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "questions")
        self.assertEqual(state["clarification"]["questions"], ["What is the budget?"])
        self.assertEqual(state["answers"], ["my answer"])
        self.assertEqual(state["rounds"], [])

    def test_back_after_a_failed_run_returns_to_confirm_with_the_choice_kept(self):
        keep = self.provider.complete
        self.provider.complete = lambda task, prompt, on_text=None: (_ for _ in ()).throw(RuntimeError("gateway down")) \
            if "## Assessments" in prompt else keep(task, prompt)
        try:
            _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
            sid = state["id"]
            self.wait_for(sid, lambda s: s["phase"] == "questions")
            self.call("POST", f"/api/sessions/{sid}/answers", {"answers": ["x"], "final": True})
            self.wait_for(sid, lambda s: s["phase"] == "confirm")
            chosen = CLASSIC[:2]
            self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Edited topic", "context": "c", "options": [], "constraints": [], "members": chosen})
            state = self.wait_for(sid, lambda s: s["phase"] == "error")
            self.assertIn("gateway down", state["error"])
        finally:
            self.provider.complete = keep
        status, state = self.call("POST", f"/api/sessions/{sid}/back")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "confirm")
        self.assertIsNone(state["error"])
        self.assertEqual(state["inputs"]["topic"], "Edited topic")
        self.assertEqual(sorted(state["selected_members"]), sorted(chosen))
        self.assertEqual(sorted(state["members"]), sorted(chosen))
        # and the board can be run again from there
        status, _ = self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Edited topic", "members": chosen})
        self.assertEqual(status, 200)
        self.wait_for(sid, lambda s: s["phase"] == "result")

    def test_back_while_the_board_works_stops_it_and_returns_to_confirm(self):
        import time as _time
        keep = self.provider.complete

        def slow(task, prompt, on_text=None):
            if "## Member (FR-3.3a)" in prompt:
                _time.sleep(0.6)
            return keep(task, prompt)
        self.provider.complete = slow
        try:
            _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
            sid = state["id"]
            self.wait_for(sid, lambda s: s["phase"] == "questions")
            self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
            self.wait_for(sid, lambda s: s["phase"] == "confirm")
            baseline = threading.active_count()
            self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Anything", "members": CLASSIC[:2]})
            state = self.wait_for(sid, lambda s: s["phase"] == "running")
            self.assertTrue(state["nav"]["back"])
            status, state = self.call("POST", f"/api/sessions/{sid}/back")
            self.assertEqual(status, 200)
            self.assertEqual(state["phase"], "confirm")
            deadline = _time.monotonic() + 8
            while threading.active_count() > baseline and _time.monotonic() < deadline:
                _time.sleep(0.1)                               # the stopped worker threads finish in the background
            _, state = self.call("GET", f"/api/sessions/{sid}")
            self.assertEqual(state["phase"], "confirm")        # their late result was discarded
            self.assertIsNone(state["result"])
            # and the board runs again from there
            self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Anything", "members": CLASSIC[:2]})
            state = self.wait_for(sid, lambda s: s["phase"] == "result")
            self.assertEqual(len(state["result"]["assessments"]), 2)
        finally:
            self.provider.complete = keep

    def test_back_from_the_result_and_forward_again(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Anything", "members": CLASSIC[:2]})
        self.wait_for(sid, lambda s: s["phase"] == "result")
        status, state = self.call("POST", f"/api/sessions/{sid}/back")
        self.assertEqual(state["phase"], "confirm")
        self.assertTrue(state["nav"]["forward"])
        self.assertIsNotNone(state["result"])
        status, state = self.call("POST", f"/api/sessions/{sid}/forward")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "result")
        self.assertFalse(state["nav"]["forward"])
        status, _ = self.call("POST", f"/api/sessions/{sid}/forward")
        self.assertEqual(status, 409)
        status, state = self.call("POST", f"/api/sessions/{sid}/abandon")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "closed")

    def test_back_with_nothing_behind_it_is_refused(self):
        original = self.config["knowledge"]["vault_path"]
        self.config["knowledge"]["vault_path"] = str(Path(self.tmp.name) / "missing")
        try:
            _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
            sid = state["id"]
            self.wait_for(sid, lambda s: s["phase"] == "error")
        finally:
            self.config["knowledge"]["vault_path"] = original
        status, body = self.call("POST", f"/api/sessions/{sid}/back")
        self.assertEqual(status, 409)
        self.assertIn("your question is kept", body["error"])

    def test_only_this_machines_page_may_talk_to_the_server(self):
        def raw(method, path, headers, body=b""):
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body or None, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code
        self.assertEqual(raw("GET", "/api/config", {"Host": "evil.example.com"}), 403)                 # DNS rebinding
        self.assertEqual(raw("GET", "/api/config", {"Host": f"127.0.0.1:{self.port + 1}"}), 403)      # another port
        self.assertEqual(raw("POST", "/api/sessions", {"Host": f"127.0.0.1:{self.port}", "Origin": "http://evil.example.com",
                                                        "Content-Type": "application/json"}, b'{"question": "x"}'), 403)
        self.assertEqual(raw("POST", "/api/sessions", {"Host": f"127.0.0.1:{self.port}", "Content-Type": "text/plain"},
                             b'{"question": "x"}'), 415)                                                # a "simple" cross-origin POST
        self.assertEqual(raw("POST", "/api/sessions", {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json",
                                                        "Content-Length": "abc"}, b"{}"), 400)
        self.assertEqual(raw("GET", "/api/config", {"Host": f"localhost:{self.port}"}), 200)

    def test_actions_out_of_order_are_refused(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Anything?"})
        sid = state["id"]
        status, body = self.call("POST", f"/api/sessions/{sid}/run", {"topic": "x"})
        self.assertEqual(status, 409)
        status, body = self.call("POST", f"/api/sessions/{sid}/follow-up", {"question": "x"})
        self.assertEqual(status, 409)
        status, body = self.call("GET", "/api/sessions/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()


class TestKnowledgePick(TestServerFlow):
    """Spec 5.1: the AI-assisted pick on the confirm screen, its fallback,
    the shared core once in the combined form, the statistics row."""

    def _to_confirm(self, selection=None):
        _, state = self.call("POST", "/api/sessions", {"question": "Rework the tooling before MG4?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        if selection:
            self.call("POST", f"/api/sessions/{sid}/pick", {"selection": selection})
        return sid

    def test_the_pick_runs_on_the_confirm_screen_and_leads_each_members_block(self):
        sid = self._to_confirm("ai")
        state = self.wait_for(sid, lambda s: s["pick_state"] in ("done", "failed"))
        self.assertEqual(state["pick_state"], "done", state["pick_error"])
        self.assertEqual(state["selection"], "ai")
        first = state["picks"][CLASSIC[0]]["full"][0]           # the fake picks the first candidate
        status, est = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": list(CLASSIC[:2]), "budget": 2000})
        self.assertEqual(est["calls"], 3)                        # the pick is made: two members plus the synthesis
        self.assertEqual(est["picked_by"][CLASSIC[0]], "model")
        self.assertEqual(est["reasons"][CLASSIC[0]][first], f"{CLASSIC[0]} needs it")
        self.assertTrue(any(s["reason"] for s in est["sections"][CLASSIC[0]]))
        self.assertIn("split", est)
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Rework?", "members": list(CLASSIC[:2]), "budget": 2000, "mode": "combined"})
        state = self.wait_for(sid, lambda s: s["phase"] == "result")
        combined = [p for p in self.provider.prompts if "## Members to assess" in p][-1]
        self.assertEqual(combined.count("shared by every member"), 1 if "shared by every member" in combined else 0)
        self.assertIn(CLASSIC[0], state["knowledge_split"])
        steps = [c["step"] for c in state["stats"]["calls"]]
        self.assertIn("knowledge pick", steps)

    def test_a_bad_pick_keeps_the_python_ranking_and_says_so(self):
        self.provider.pick_answer = "{}"
        try:
            sid = self._to_confirm("ai")
            state = self.wait_for(sid, lambda s: s["pick_state"] in ("done", "failed"))
            self.assertEqual(state["pick_state"], "failed")
            self.assertIn("members", state["pick_error"])
            _, est = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": list(CLASSIC[:2]), "budget": 2000})
            self.assertEqual(est["picked_by"][CLASSIC[0]], "python")
            self.assertEqual(est["calls"], 3)                    # a failed pick is not redone: two members, synthesis
            self.assertIn("Tooling.md", est["members"][CLASSIC[0]])
        finally:
            self.provider.pick_answer = None

    def test_new_inputs_make_a_new_pick_and_a_running_pick_is_counted_once(self):
        sid = self._to_confirm("ai")
        state = self.wait_for(sid, lambda s: s["pick_state"] in ("done", "failed"))
        self.assertEqual(state["pick_state"], "done")
        picks_before = len([p for p in self.provider.prompts if "## Candidate sections" in p])
        _, est = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": list(CLASSIC[:2]), "budget": 2000})
        self.assertEqual(est["calls"], 3)                        # the pick is done: not counted again
        # Back to the questions, answer again: the confirm screen has new inputs and picks again.
        self.call("POST", f"/api/sessions/{sid}/back")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": ["changed answer"], "final": True})
        state = self.wait_for(sid, lambda s: s["phase"] == "confirm" and s["pick_state"] in ("done", "failed"))
        self.assertEqual(state["pick_state"], "done")
        self.assertEqual(len([p for p in self.provider.prompts if "## Candidate sections" in p]), picks_before + 1)
        self.assertIn("changed answer", [p for p in self.provider.prompts if "## Candidate sections" in p][-1])

    def test_python_only_makes_no_pick_call(self):
        sid = self._to_confirm("python")
        _, state = self.call("GET", f"/api/sessions/{sid}")
        self.assertEqual(state["pick_state"], "idle")
        _, est = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": list(CLASSIC[:2]), "budget": 2000})
        self.assertEqual(est["calls"], 3)
        self.assertNotIn("knowledge pick", [p["label"] for p in est["per_call"]])


class TestSiteName(unittest.TestCase):
    """Spec 9.5, decision 21: the site is named under .localhost, and only
    that one name is accepted next to localhost and 127.0.0.1."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = {"provider": {"models": {"board": "fake/m"}}, "knowledge": {"vault_path": ""},
                      "server": {"site_name": "ai", "history_folder": str(Path(cls.tmp.name) / "history")}}
        cls.httpd, cls.board_server = create_http_server(cls.config, None, port=0, provider=RoutingFakeProvider())
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def raw(self, method, path, headers, body=b""):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body or None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_the_name_is_cleaned_and_a_plain_one_goes_under_localhost(self):
        self.assertEqual(site_name({}), "program-mind")                       # spec 11.1, decision 1
        self.assertEqual(site_host({}), "program-mind.localhost")
        self.assertEqual(site_name({"server": {"site_name": "My Site!"}}), "mysite")
        self.assertEqual(site_name({"server": {"site_name": "!!"}}), "program-mind")
        # A name that carries a dot is the whole host: it is used as it stands.
        self.assertEqual(site_host({"server": {"site_name": "Program-Mind.local"}}), "program-mind.local")
        self.assertEqual(site_host({"server": {"site_name": ".mind."}}), "mind.localhost")

    def test_only_the_named_host_under_localhost_is_accepted(self):
        self.assertEqual(self.raw("GET", "/api/config", {"Host": f"ai.localhost:{self.port}"}), 200)
        self.assertEqual(self.raw("GET", "/api/config", {"Host": f"evil.localhost:{self.port}"}), 403)
        self.assertEqual(self.raw("GET", "/api/config", {"Host": f"ai.localhost:{self.port + 1}"}), 403)

    def test_only_the_named_origin_under_localhost_is_accepted(self):
        json_headers = {"Host": f"ai.localhost:{self.port}", "Content-Type": "application/json"}
        self.assertEqual(self.raw("POST", "/api/sessions", {**json_headers, "Origin": f"http://ai.localhost:{self.port}"},
                                  b'{"question": "x"}'), 201)
        self.assertEqual(self.raw("POST", "/api/sessions", {**json_headers, "Origin": f"http://evil.localhost:{self.port}"},
                                  b'{"question": "x"}'), 403)


class TestAskThreads(unittest.TestCase):
    """Ask the vault (spec 10) through the API: threads, questions with
    sources checked by Python, the estimate, close with and without a
    note, delete, and a thread that survives a restart."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.vault = Path(cls.tmp.name) / "vault"
        cls.vault.mkdir()
        (cls.vault / "Tooling.md").write_text("---\ntitle: Tooling\ntags: [tooling]\n---\nTooling is late.\n", encoding="utf-8")
        make_roles(cls.vault / "Roles&Responsibilities")
        cls.threads = Path(cls.tmp.name) / "threads"
        cls.config_path = Path(cls.tmp.name) / "config.local.json"
        cls.config = {"provider": {"models": {"board": "fake/m"}},
                      "knowledge": {"vault_path": str(cls.vault), "token_budget": 6000, "selection": "python"},
                      "server": {"threads_folder": str(cls.threads)}, "ask": {"token_budget": 3000}}
        cls.config_path.write_text(json.dumps(cls.config), encoding="utf-8")
        cls.provider = RoutingFakeProvider()
        cls.httpd, cls.board_server = create_http_server(cls.config, cls.config_path, port=0, provider=cls.provider)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    call = TestServerFlow.call
    wait_for = TestServerFlow.wait_for

    def _wait(self, thread_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, state = self.call("GET", f"/api/ask/{thread_id}")
            if not state["busy"] and state["phase"] not in ("asking", "checking", "reading_more", "proposing"):
                return state
            time.sleep(0.05)
        raise AssertionError("thread still busy")

    def test_a_new_thread_is_listed_and_answers_with_checked_sources(self):
        first_prompt = len(self.provider.prompts)
        status, created = self.call("POST", "/api/ask", {"projects": []})
        self.assertEqual(status, 201)
        self.assertEqual(created["kind"], "ask")
        self.assertEqual(created["status"], "open")
        self.assertEqual(created["budget"], 3000)
        self.assertEqual(created["turns"], [])
        _, listed = self.call("GET", "/api/ask")
        row = next(r for r in listed["threads"] if r["id"] == created["id"])
        self.assertEqual((row["questions"], row["title"]), (0, "New thread"))
        status, state = ask_and_read(self, created["id"], "Is the tooling late?")
        self.assertEqual(status, 200)
        self.assertIn(state["phase"], ("asking", "idle"))
        state = self._wait(created["id"])
        self.assertEqual(len(state["turns"]), 1)
        turn = state["turns"][0]
        self.assertEqual(turn["answer"], "- Tooling is late.")
        self.assertEqual([s["path"] for s in turn["sources"]], ["Tooling.md"])   # the invented one is dropped
        self.assertEqual(turn["dropped"], 1)
        self.assertEqual(turn["gaps"], ["the new date"])
        self.assertIn("Tooling.md", turn["paths"])                       # 5.6: the whole vault, role pages included
        self.assertEqual([c["step"] for c in state["stats"]["calls"]], ["ask the vault"])   # 5.6: the whole vault, one call
        self.assertEqual(state["llm_calls"], 1)
        self.assertEqual(len(turn["rounds"]), 1)
        self.assertEqual(turn["chosen_by"], "vault")
        prompt = [p for p in self.provider.prompts[first_prompt:] if "## Question to the vault" in p][0]
        self.assertIn("Tooling is late.", prompt)
        self.assertNotIn("## Earlier in this thread", prompt)
        # The second question carries the first turn.
        _, state = ask_and_read(self, created["id"], "And who fixes it?")
        self.assertEqual(len(state["turns"]), 2)
        prompt = [p for p in self.provider.prompts[first_prompt:] if "## Question to the vault" in p][-1]
        self.assertIn("## Earlier in this thread", prompt)
        self.assertIn("Q: Is the tooling late?\nA: - Tooling is late.", prompt)
        _, listed = self.call("GET", "/api/ask")
        row = next(r for r in listed["threads"] if r["id"] == created["id"])
        self.assertEqual((row["questions"], row["title"]), (2, "Is the tooling late?"))
        # The estimate: the pages the first read would take, whole.
        _, est = self.call("POST", f"/api/ask/{created['id']}/estimate", {"question": "x"})
        self.assertEqual(est["calls"], 1)
        self.assertEqual(est["per_call"][0]["label"], "ask the vault")
        self.assertIn("Tooling.md", [s["path"] for s in est["pages"]])
        self.assertGreater(est["tokens_in"], 0)
        self.assertEqual(est["max_reads"], 2)
        self.assertTrue(est["whole_vault"])

    def test_the_pages_are_shown_before_the_thread_exists(self):
        """Spec 5.7, decision 3: the first question sees its pages too."""
        status, est = self.call("POST", "/api/ask/estimate", {"question": "Is the tooling late?"})
        self.assertEqual(status, 200)
        self.assertEqual(est["picked_by"], "vault")
        self.assertTrue(est["whole_vault"])
        self.assertIn("Tooling.md", [s["path"] for s in est["pages"]])
        self.assertEqual(est["calls"], 1)
        _, listed = self.call("GET", "/api/ask")
        self.assertEqual([r for r in listed["threads"] if r["questions"] == 0 and r["title"] == "New thread"], [])

    def test_a_question_that_fits_reads_and_answers_at_once_and_a_page_can_be_unticked(self):
        first_prompt = len(self.provider.prompts)
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        _, est = self.call("POST", f"/api/ask/{tid}/estimate", {"question": "Is the tooling late?"})
        self.assertEqual(est["picked_by"], "vault")
        self.assertEqual(est["beyond"], [])
        role_page = next(s["path"] for s in est["pages"] if s["path"].startswith("Roles"))
        _, est = self.call("POST", f"/api/ask/{tid}/estimate", {"question": "Is the tooling late?", "exclude": [role_page]})
        self.assertNotIn(role_page, [s["path"] for s in est["pages"]])
        # Spec 5.7, decision 1: asking reads and answers; there is no picks screen to pass.
        status, state = self.call("POST", f"/api/ask/{tid}/question", {"question": "Is the tooling late?", "exclude": [role_page]})
        self.assertEqual((status, state["phase"], state["busy"], state["whole_vault"]), (200, "asking", True, True))
        self.assertEqual([s["key"] for s in state["steps"]], ["picks", "read1", "ask1"])   # no choosing, no check
        state = self._wait(tid)
        turn = state["turns"][0]
        self.assertEqual(len(turn["paths"]), len(est["pages"]))
        self.assertNotIn(role_page, turn["paths"])
        self.assertIn("Tooling.md", turn["paths"])
        self.assertFalse(turn["read_all"])                                 # a page was unticked (5.7, decision 5)
        self.assertEqual([c["step"] for c in state["stats"]["calls"]], ["ask the vault"])
        prompt = [p for p in self.provider.prompts[first_prompt:] if "## Question to the vault" in p][-1]
        self.assertNotIn("## Table of contents", prompt)                   # 5.6, decision 5: noise when everything is read
        self.assertNotIn("## Pages read earlier", prompt)
        # The tick sticks for the thread until it is put back.
        self.call("POST", f"/api/ask/{tid}/question", {"question": "And who fixes it?", "exclude": []})
        state = self._wait(tid)
        self.assertTrue(state["turns"][-1]["read_all"])                    # nothing left out: the whole vault
        self.assertIn(role_page, state["turns"][-1]["paths"])

    def test_the_page_is_given_the_answer_as_it_is_written(self):
        """Spec 4.1: the live text is the answer so far, read out of the
        half-written JSON, and it is gone when the turn stands."""
        self.provider.delay = 0.8
        try:
            _, created = self.call("POST", "/api/ask", {})
            tid = created["id"]
            self.call("POST", f"/api/ask/{tid}/question", {"question": "Is the tooling late?"})
            live = ""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, state = self.call("GET", f"/api/ask/{tid}")
                live = state.get("live") or live
                if not state["busy"]:
                    break
                time.sleep(0.05)
            self.assertEqual(live, "- Toolin")                # ASK's answer, as far as it was written
        finally:
            self.provider.delay = 0.0
        state = self._wait(tid)
        self.assertEqual(state["live"], "")                   # gone once the turn stands
        self.assertEqual(state["turns"][-1]["answer"], "- Tooling is late.")

    def test_a_busy_thread_refuses_a_second_question(self):
        self.provider.delay = 0.6
        try:
            _, created = self.call("POST", "/api/ask", {})
            self.call("POST", f"/api/ask/{created['id']}/question", {"question": "One?"})
            status, body = self.call("POST", f"/api/ask/{created['id']}/question", {"question": "Two?"})
            self.assertEqual(status, 409)                                  # spec 5.7: the first question is already running
            self.assertIn("Wait", body["error"])
            status, _ = self.call("POST", f"/api/ask/{created['id']}/close", {"remember": False})
            self.assertEqual(status, 409)
        finally:
            self.provider.delay = 0.0
        self._wait(created["id"])

    def test_close_without_a_note_and_a_closed_thread_takes_no_question(self):
        _, created = self.call("POST", "/api/ask", {})
        status, _ = self.call("POST", f"/api/ask/{created['id']}/close", {"remember": False})
        self.assertEqual(status, 409)                       # nothing asked yet
        ask_and_read(self, created["id"], "Late?")
        status, state = self.call("POST", f"/api/ask/{created['id']}/close", {"remember": False})
        self.assertEqual((status, state["status"]), (200, "closed"))
        status, body = self.call("POST", f"/api/ask/{created['id']}/question", {"question": "More?"})
        self.assertEqual(status, 409)
        self.assertIn("closed", body["error"])
        _, listed = self.call("GET", "/api/ask")
        self.assertEqual(next(r for r in listed["threads"] if r["id"] == created["id"])["status"], "closed")

    def test_close_with_a_note_proposes_and_writes_through_the_memory_step(self):
        _, created = self.call("POST", "/api/ask", {})
        ask_and_read(self, created["id"], "Late?")
        status, state = self.call("POST", f"/api/ask/{created['id']}/close", {"remember": True})
        self.assertEqual(status, 200)
        self.assertIn(state["phase"], ("proposing", "proposal"))
        state = self._wait(created["id"])
        self.assertEqual(state["phase"], "proposal")
        self.assertTrue(state["proposal"]["path"])
        self.assertEqual(state["status"], "closed")
        status, state = self.call("POST", f"/api/ask/{created['id']}/memory", {"body": "# Note\n\nkept"})
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "written")
        self.assertTrue(Path(state["written_path"]).exists())
        self.assertTrue(Path(state["written_path"]).resolve().is_relative_to(self.vault.resolve()))
        on_disk = json.loads((self.threads / f"{created['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["written_path"], state["written_path"])

    def test_delete_removes_the_file_and_a_thread_survives_a_restart(self):
        _, created = self.call("POST", "/api/ask", {})
        ask_and_read(self, created["id"], "Late?")
        # A second server on the same config reads the thread from disk.
        httpd, _server = create_http_server(self.config, self.config_path, port=0, provider=self.provider)
        try:
            other = threading.Thread(target=httpd.serve_forever, daemon=True)
            other.start()
            request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/ask/{created['id']}")
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(len(data["turns"]), 1)
        finally:
            httpd.shutdown()
            httpd.server_close()
        status, body = self.call("DELETE", f"/api/ask/{created['id']}")
        self.assertEqual((status, body["deleted"]), (200, True))
        self.assertFalse((self.threads / f"{created['id']}.json").exists())
        status, _ = self.call("GET", f"/api/ask/{created['id']}")
        self.assertEqual(status, 404)
        status, _ = self.call("DELETE", f"/api/ask/{created['id']}")
        self.assertEqual(status, 404)

    def test_config_carries_the_site_name_the_ask_budget_and_the_vault_name(self):
        _, cfg = self.call("GET", "/api/config")
        self.assertEqual(cfg["site_name"], "program-mind")
        self.assertEqual(cfg["site_host"], "program-mind.localhost")
        self.assertEqual(cfg["ask_budget"], 3000)
        self.assertEqual(cfg["vault_name"], "vault")


class TestShellStatus(unittest.TestCase):
    """Spec 11.1, decision 7: the three status checks, cheap; the test call
    only on request. Decision 5: the recent open work of every agent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        (self.vault / "Tooling.md").write_text("---\ntitle: Tooling\n---\nTooling is late.\n", encoding="utf-8")
        self.config_path = Path(self.tmp.name) / "config.local.json"
        self.config = {"provider": {"models": {"board": "fake/m"}},
                       "knowledge": {"vault_path": str(self.vault), "selection": "python"},
                       "server": {"threads_folder": str(Path(self.tmp.name) / "threads")}}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.provider = RoutingFakeProvider()
        self.httpd, self.board_server = create_http_server(self.config, self.config_path, port=0, provider=self.provider)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_the_vault_is_amber_without_project_pages_and_green_with_them_and_a_roles_folder(self):
        status, s = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(s["vault"]["state"], "amber")
        self.assertEqual(s["vault"]["notes"], 1)
        self.assertIn("no project pages", s["vault"]["detail"])
        self.assertIn("no roles folder", s["vault"]["detail"])
        self.assertEqual(s["project"]["state"], "red")            # no project pages at all
        (self.vault / "Dual DCDC.md").write_text(
            "---\nkind: project\nsummary: The Gen6 project.\n---\n# Dual DCDC\n\n| Gate | Date |\n|---|---|\n| MG3 | 12 March 2027 |\n| MG7 | SOP Aug 2028 |\n",
            encoding="utf-8")
        make_roles(self.vault / "Roles&Responsibilities")
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["vault"]["state"], "green")
        self.assertEqual(s["vault"]["projects"], 1)
        self.assertTrue(s["vault"]["roles_folder"].endswith("Roles&Responsibilities"))
        self.assertIsNotNone(s["vault"]["last_read"])
        self.assertEqual(s["project"]["state"], "amber")          # nothing chosen: all projects
        _, s = self.call("GET", "/api/status?projects=Dual%20DCDC")
        self.assertEqual(s["project"]["state"], "green")
        page = s["project"]["pages"][0]
        self.assertEqual(page["title"], "Dual DCDC")
        self.assertEqual(page["gates"], ["MG3 · 12 March 2027", "MG7 · SOP Aug 2028"])   # the gate baseline, from the page
        _, s = self.call("GET", "/api/status?projects=Nowhere")
        self.assertEqual(s["project"]["state"], "amber")
        self.assertIn("No project page for: Nowhere", s["project"]["detail"])

    def test_the_vault_is_red_when_unset_or_unreachable(self):
        self.config["knowledge"]["vault_path"] = ""
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["vault"]["state"], "red")
        self.config["knowledge"]["vault_path"] = str(self.vault / "gone")
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["vault"]["state"], "red")
        self.assertIn("not found", s["vault"]["detail"])

    def test_the_ai_check_is_cheap_and_the_test_call_settles_it(self):
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["ai"]["state"], "amber")               # a model, no gateway file: unverified
        self.assertIsNone(s["ai"]["last_call"])
        self.config["provider"]["models"]["board"] = ""
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["ai"]["state"], "red")
        self.config["provider"]["models"]["board"] = "fake/m"
        gateway = Path(self.tmp.name) / "opencode.json"
        gateway.write_text(json.dumps({"provider": {"azure": {"options": {"apiKey": "secret"}}}, "model": "azure/x"}), encoding="utf-8")
        self.config["provider"]["opencode"] = {"config_file": str(gateway)}
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["ai"]["state"], "green")
        self.assertNotIn("secret", json.dumps(s))                 # the key never reaches the page
        gateway.write_text(json.dumps({"provider": {"azure": {"options": {}}}, "model": "azure/x"}), encoding="utf-8")
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["ai"]["state"], "amber")
        before = len(self.provider.prompts)
        status, ai = self.call("POST", "/api/status/ai")             # the confirmed test call
        self.assertEqual(status, 200)
        self.assertEqual(len(self.provider.prompts), before + 1)      # exactly one call, never on a timer
        self.assertEqual(ai["state"], "green")
        self.assertTrue(ai["last_call"]["ok"])
        self.assertIn("answered in", ai["detail"])
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["ai"]["state"], "green")                   # the settings are unchanged: the call still counts
        self.assertEqual(len(self.provider.prompts), before + 1)
        self.config["provider"]["models"]["board"] = "fake/other"
        _, s = self.call("GET", "/api/status")
        self.assertEqual(s["ai"]["state"], "amber")                   # new settings: back to the cheap look

    def test_a_failed_test_call_turns_the_icon_red(self):
        class Broken(AiProvider):
            def complete(self, task, prompt, on_text=None):
                raise RuntimeError("gateway said no")
        self.board_server._provider_override = Broken()
        _, ai = self.call("POST", "/api/status/ai")
        self.assertEqual(ai["state"], "red")
        self.assertIn("gateway said no", ai["detail"])

    def test_the_recent_work_lists_open_threads_and_topics_newest_first(self):
        _, created = self.call("POST", "/api/ask", {"projects": ["Dual DCDC"]})
        ask_and_read(self, created["id"], "What is late?")
        time.sleep(0.05)
        _, session = self.call("POST", "/api/sessions", {"question": "Rework or switch?"})
        _, listed = self.call("GET", "/api/history")
        items = listed["items"]
        self.assertEqual([i["kind"] for i in items], ["board", "ask"])
        self.assertEqual(items[0]["title"], "Rework or switch?")
        self.assertEqual(items[0]["unit"], "call")
        self.assertEqual(items[1]["title"], "What is late?")
        self.assertEqual(items[1]["projects"], ["Dual DCDC"])
        self.assertEqual(items[1]["unit"], "question")
        self.call("POST", f"/api/ask/{created['id']}/close", {"remember": False})
        _, listed = self.call("GET", "/api/history?state=open")
        self.assertEqual([i["kind"] for i in listed["items"]], ["board"])
        _, listed = self.call("GET", "/api/history?state=closed")
        self.assertEqual([i["kind"] for i in listed["items"]], ["ask"])
        _, listed = self.call("GET", "/api/history?state=all")
        self.assertEqual(len(listed["items"]), 2)
        self.call("POST", f"/api/sessions/{session['id']}/abandon")
        _, listed = self.call("GET", "/api/history")
        self.assertEqual(listed["items"], [])


class TestHistory(unittest.TestCase):
    """Spec 11.1, decisions 9 and 10: every board topic is kept next to the
    threads, comes back after a restart, and moves to the archive when it
    is closed. A topic left with Start over is dropped, not archived."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        (self.vault / "Tooling.md").write_text("---\ntitle: Tooling\n---\nTooling is late.\n", encoding="utf-8")
        make_roles(self.vault / "Roles&Responsibilities")
        self.folder = Path(self.tmp.name) / "history"
        self.config_path = Path(self.tmp.name) / "config.local.json"
        self.config = {"provider": {"models": {"board": "fake/m"}},
                       "knowledge": {"vault_path": str(self.vault), "token_budget": 6000, "selection": "python"},
                       "server": {"history_folder": str(self.folder)}}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.provider = RoutingFakeProvider()
        self.servers = []
        self.port = self.start()

    def tearDown(self):
        for httpd in self.servers:
            httpd.shutdown()
            httpd.server_close()
        self.tmp.cleanup()

    def start(self) -> int:
        """One more server over the same folder - a restart, as far as the
        work on disk is concerned."""
        httpd, _ = create_http_server(json.loads(self.config_path.read_text(encoding="utf-8")),
                                      self.config_path, port=0, provider=self.provider)
        self.servers.append(httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd.server_address[1]

    def call(self, method, path, body=None, port=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"http://127.0.0.1:{port or self.port}{path}", data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def wait_for(self, sid, predicate, port=None, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, state = self.call("GET", f"/api/sessions/{sid}", port=port)
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail(f"timed out waiting; last phase {state['phase']} error {state['error']}")

    def run_to_result(self, question="Rework or switch?"):
        _, state = self.call("POST", "/api/sessions", {"question": question, "projects": ["Dual DCDC"]})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": ["200k"], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Rework or switch", "context": "SOP is fixed.",
                                                       "options": ["Rework"], "constraints": [], "members": CLASSIC[:2]})
        return sid, self.wait_for(sid, lambda s: s["phase"] == "result")

    def test_a_topic_is_written_as_one_file_beside_the_threads_and_says_what_it_is(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Rework or switch?"})
        sid = state["id"]
        record = json.loads((self.folder / f"{sid}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["kind"], "board")
        self.assertEqual(record["status"], "open")
        self.assertEqual(record["question"], "Rework or switch?")
        _, created = self.call("POST", "/api/ask", {})
        thread = json.loads((self.folder / f"{created['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(thread["kind"], "ask")          # both agents, one folder, told apart by kind

    def test_a_topic_comes_back_after_a_restart_and_takes_a_follow_up(self):
        sid, state = self.run_to_result()
        self.assertEqual(len(self.provider.prompts) > 0, True)
        self.call("POST", f"/api/sessions/{sid}/follow-up", {"question": "Why?"})
        self.wait_for(sid, lambda s: not s["busy"])
        self.wait_for_record(sid, lambda r: r["turns"] and not r["turns"][-1].get("pending"))
        second = self.start()                            # a new server, the same folder
        status, state = self.call("GET", f"/api/sessions/{sid}", port=second)
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "result")
        self.assertEqual(state["result"]["synthesis_data"]["overall_recommendation"], "Rework")
        self.assertEqual(len(state["turns"]), 1)
        self.assertEqual(state["turns"][0]["data"]["recommendation_now"], "Rework, unchanged.")
        self.assertEqual(sorted(state["members"]), sorted(CLASSIC[:2]))
        self.assertEqual(state["projects"], ["Dual DCDC"])
        # The conversation is rebuilt from the record: the next follow-up works.
        status, _ = self.call("POST", f"/api/sessions/{sid}/follow-up", {"question": "And the cost?"}, port=second)
        self.assertEqual(status, 200)
        state = self.wait_for(sid, lambda s: not s["busy"], port=second)
        self.assertEqual(len(state["turns"]), 2)
        self.assertFalse(state["turns"][1].get("error", False))
        self.assertEqual(state["turns"][1]["data"]["recommendation_now"], "Rework, unchanged.")
        self.assertEqual(state["turns"][1]["question"], "And the cost?")

    def test_a_closed_topic_moves_to_the_archive_and_an_abandoned_one_is_dropped(self):
        sid, _ = self.run_to_result()
        _, listed = self.call("GET", "/api/history?state=open")
        self.assertEqual([r["id"] for r in listed["items"]], [sid])
        self.call("POST", f"/api/sessions/{sid}/close", {"remember": False})
        _, listed = self.call("GET", "/api/history?state=open")
        self.assertEqual(listed["items"], [])
        _, listed = self.call("GET", "/api/history?state=closed")
        self.assertEqual([r["id"] for r in listed["items"]], [sid])
        self.assertEqual(listed["items"][0]["title"], "Rework or switch")
        self.assertEqual(listed["items"][0]["kind"], "board")
        self.assertTrue((self.folder / f"{sid}.json").exists())

        _, state = self.call("POST", "/api/sessions", {"question": "Something else?"})
        other = state["id"]
        self.assertTrue((self.folder / f"{other}.json").exists())
        self.call("POST", f"/api/sessions/{other}/abandon")             # Start over
        self.assertFalse((self.folder / f"{other}.json").exists())      # not archived: dropped
        _, listed = self.call("GET", "/api/history?state=all")
        self.assertEqual([r["id"] for r in listed["items"]], [sid])

    def test_the_archive_deletes_a_topic_and_a_thread_and_keeps_the_vault_note(self):
        sid, _ = self.run_to_result()
        self.call("POST", f"/api/sessions/{sid}/close", {"remember": False})
        _, created = self.call("POST", "/api/ask", {})
        ask_and_read(self, created["id"], "What is late?")
        self.call("POST", f"/api/ask/{created['id']}/close", {"remember": False})
        _, listed = self.call("GET", "/api/history?state=closed")
        self.assertEqual(sorted(r["kind"] for r in listed["items"]), ["ask", "board"])

        status, _ = self.call("DELETE", f"/api/history/{sid}")
        self.assertEqual(status, 200)
        self.assertFalse((self.folder / f"{sid}.json").exists())
        status, _ = self.call("DELETE", f"/api/history/{created['id']}")
        self.assertEqual(status, 200)
        _, listed = self.call("GET", "/api/history?state=all")
        self.assertEqual(listed["items"], [])
        status, _ = self.call("DELETE", f"/api/history/{sid}")
        self.assertEqual(status, 404)
        self.assertEqual(self.call("GET", f"/api/sessions/{sid}")[0], 404)

    def wait_for_record(self, record_id, predicate, timeout=10):
        """The page reads the topic in memory; the disk catches up right
        after the step that changed it. A restart waits for that."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = json.loads((self.folder / f"{record_id}.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                record = None
            if record is not None and predicate(record):
                return record
            time.sleep(0.02)
        self.fail("the topic was never written")

    def wait_for_thread(self, thread_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, state = self.call("GET", f"/api/ask/{thread_id}")
            if not state["busy"]:
                return state
            time.sleep(0.02)
        self.fail("the thread never answered")

    def test_a_topic_caught_mid_call_comes_back_at_the_step_it_can_be_worked_from(self):
        record = {"id": "abcdef123456", "kind": "board", "status": "open", "phase": "running",
                  "question": "Rework or switch?", "inputs": {"topic": "Rework or switch", "context": "x"},
                  "clarification": {"topic": "Rework or switch", "context": "x", "questions": []},
                  "created": time.time(), "updated": time.time()}
        (self.folder).mkdir(parents=True, exist_ok=True)
        (self.folder / "abcdef123456.json").write_text(json.dumps(record), encoding="utf-8")
        status, state = self.call("GET", "/api/sessions/abcdef123456")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "confirm")       # the run cannot be resumed; its inputs can
        self.assertFalse(state["busy"])

    def test_the_folder_defaults_to_history_and_the_old_threads_folder_is_taken_over(self):
        base = Path(self.tmp.name) / "elsewhere"
        (base / "threads").mkdir(parents=True)
        (base / "threads" / "abcdef123456.json").write_text('{"id": "abcdef123456"}', encoding="utf-8")
        folder = history_mod.history_folder({}, base / "config.local.json")
        self.assertEqual(folder, base / "history")
        self.assertTrue((folder / "abcdef123456.json").exists())     # the threads written before the rename
        self.assertFalse((base / "threads").exists())
        # A folder named in the configuration is used as it stands, under either name.
        named = {"server": {"threads_folder": str(base / "kept")}}
        self.assertEqual(history_mod.history_folder(named, base / "config.local.json"), base / "kept")

    def test_the_record_carries_the_knowledge_paths_but_never_the_knowledge_text(self):
        sid, _ = self.run_to_result()
        record = json.loads((self.folder / f"{sid}.json").read_text(encoding="utf-8"))
        self.assertIn("Tooling.md", record["knowledge_paths"][CLASSIC[0]])      # 5.8: every page, this one among them
        self.assertNotIn("Tooling is late.", json.dumps(record))     # the vault's words stay in the vault
        restored = Session.from_record(record)
        self.assertEqual(restored.id, sid)
        self.assertEqual(restored.title, "Rework or switch")
        self.assertEqual(restored.status, "open")
        self.assertIn("Tooling.md", restored.member_paths[CLASSIC[0]])


class TestFreshKnowledge(unittest.TestCase):
    """Spec 5.2: every call that answers something new selects its own pages
    from the vault. The clarifier's later rounds and every follow-up read
    again; nothing runs on the first question's selection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        # Each page on its own fits the budget below; no two of them do, so
        # what reaches a prompt is what that call's question ranked first.
        (self.vault / "Tooling.md").write_text(
            "---\ntitle: Tooling\n---\n" + "The housing tooling at supplier X is late. " * 26, encoding="utf-8")
        (self.vault / "People.md").write_text(
            "---\ntitle: People\n---\n" + "The project managers are Ana Adler and Bo Baker. " * 26, encoding="utf-8")
        (self.vault / "Warranty.md").write_text(
            "---\ntitle: Warranty\n---\n" + "The warranty reserve covers field returns for three years. " * 24,
            encoding="utf-8")
        make_roles(self.vault / "Roles&Responsibilities")
        self.config_path = Path(self.tmp.name) / "config.local.json"
        self.config = {"provider": {"models": {"board": "fake/m"}},
                       # Spec 5.2 is about reading again for each new question, which is what the
                       # ranking does; a ceiling below this vault keeps the board on that path.
                       "knowledge": {"vault_path": str(self.vault), "token_budget": 300, "selection": "python",
                                     "max_read_tokens": 15120},   # 120 of pages under the reserve of 5.9
                       "server": {"history_folder": str(Path(self.tmp.name) / "history")}}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.provider = RoutingFakeProvider()
        self.httpd, self.board_server = create_http_server(self.config, self.config_path, port=0, provider=self.provider)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    call = TestHistory.call
    wait_for = TestHistory.wait_for

    def prompts_with(self, marker):
        with self.provider.lock:
            return [p for p in self.provider.prompts if marker in p]

    def test_a_later_clarifier_round_reads_the_pages_its_own_question_is_about(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Rework the housing tooling or switch supplier?"})
        sid = state["id"]
        state = self.wait_for(sid, lambda s: s["phase"] == "questions")
        first = self.prompts_with("## Question from Alex")[0]
        self.assertIn("housing tooling at supplier X is late", first)
        self.assertNotIn("Ana Adler", first)              # nobody asked about people yet
        self.assertIn("Tooling.md", state["knowledge"]["notes"])

        # The answer names the project managers: the next round must find them.
        self.call("POST", f"/api/sessions/{sid}/answers",
                  {"answers": ["The project managers Ana Adler and Bo Baker approved it."]})
        self.wait_for(sid, lambda s: s["phase"] in ("confirm", "questions"))
        second = self.prompts_with("## Clarification so far")[0]
        self.assertIn("Ana Adler", second)                # the page, not just the answer, is in the prompt
        self.assertIn("project managers are Ana Adler", second)

    def test_a_member_asked_again_gets_pages_for_the_new_question_and_the_turn_names_them(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Rework the housing tooling or switch supplier?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": ["No budget question."], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        self.call("POST", f"/api/sessions/{sid}/run", {
            "topic": "Rework the housing tooling or switch supplier", "context": "The tooling is late.",
            "options": ["Rework"], "constraints": [], "members": ["Finance"], "budget": 300})
        state = self.wait_for(sid, lambda s: s["phase"] == "result")
        self.assertIn("Tooling.md", state["member_knowledge_paths"]["Finance"])

        self.call("POST", f"/api/sessions/{sid}/follow-up",
                  {"question": "What does the warranty reserve cover?", "members": ["Finance"]})
        state = self.wait_for(sid, lambda s: not s["busy"])
        again = self.prompts_with("## Your earlier assessment")[0]
        self.assertIn("warranty reserve covers field returns", again)   # selected for the follow-up
        turn = state["turns"][-1]
        self.assertIn("Warranty.md", turn["new_pages"])                 # named on the turn, as new
        self.assertIn("Warranty.md", state["member_knowledge_paths"]["Finance"])
        self.assertIn("Tooling.md", state["member_knowledge_paths"]["Finance"])   # what was sent stays sent

    def test_a_follow_up_without_members_still_reads_for_its_own_question(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Rework the housing tooling or switch supplier?"})
        sid = state["id"]
        self.wait_for(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": ["x"], "final": True})
        self.wait_for(sid, lambda s: s["phase"] == "confirm")
        self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Rework or switch", "context": "c", "options": [],
                                                       "constraints": [], "members": ["Finance"], "budget": 300})
        self.wait_for(sid, lambda s: s["phase"] == "result")
        before = len(self.provider.prompts)
        self.call("POST", f"/api/sessions/{sid}/follow-up", {"question": "Who are the project managers?"})
        state = self.wait_for(sid, lambda s: not s["busy"])
        board_answer = self.prompts_with("## New question")[-1]
        self.assertIn("project managers are Ana Adler", board_answer)
        self.assertIn("People.md", state["turns"][-1]["new_pages"])
        self.assertEqual(len(self.provider.prompts) - before, 1)   # one call: the ranking is Python's

    def test_every_question_of_a_thread_reads_again(self):
        # Ask the vault reads the whole vault (5.6); the class's low ceiling is
        # for the board tests beside this one.
        self.board_server.config["knowledge"]["max_read_tokens"] = 100000
        self.addCleanup(self.board_server.config["knowledge"].__setitem__, "max_read_tokens", 15120)
        _, created = self.call("POST", "/api/ask", {"budget": 300})
        tid = created["id"]
        ask_and_read(self, tid, "Is the housing tooling late?")
        _, state = ask_and_read(self, tid, "Who are the project managers?")
        asked = self.prompts_with("## Question to the vault")
        self.assertIn("housing tooling at supplier X is late", asked[0])
        self.assertIn("project managers are Ana Adler", asked[1])
        self.assertIn("Tooling.md", state["turns"][0]["paths"])
        self.assertIn("People.md", state["turns"][1]["paths"])           # 5.6: the whole vault, again

    def wait_for_thread(self, thread_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, state = self.call("GET", f"/api/ask/{thread_id}")
            if not state["busy"]:
                return state
            time.sleep(0.02)
        self.fail("the thread never answered")


class TestRankingQuery(unittest.TestCase):
    """Spec 5.2: the question being answered now decides what is read; the
    earlier one only fills in when the new question has no word of its own."""

    def test_a_question_of_its_own_stands_alone(self):
        self.assertEqual(_ranking_query("Who are the project managers?", ["Is the tooling late?"]),
                         "Who are the project managers?")

    def test_a_question_that_only_points_back_takes_the_context_with_it(self):
        self.assertEqual(_ranking_query("And that?", ["Is the tooling late?"]),
                         "And that?\nIs the tooling late?")
        self.assertEqual(_ranking_query("", ["Is the tooling late?"]), "\nIs the tooling late?")


class TestChoosing(unittest.TestCase):
    """Spec 5.3: the model chooses from the whole table of contents, Alex
    sees the choice before anything is read, and a rule reads every page of
    a kind. The named-person case reads the role page and that role's tasks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "Tasks").mkdir(parents=True)
        (self.vault / "Roles").mkdir()
        (self.vault / "Org chart.md").write_text(
            "---\nkind: guide\nsummary: Who is in the project team.\n---\n# Org chart\n\n| Name | Role |\n|---|---|\n| Cleder Gomes | HW Engineering |\n",
            encoding="utf-8")
        (self.vault / "Roles" / "HW Engineering.md").write_text(
            "---\nkind: role\naliases: [Cleder Gomes]\n---\n# HW Engineering\n\nHardware lead.\n", encoding="utf-8")
        (self.vault / "Tasks" / "DV testing.md").write_text(
            "---\nkind: process\nlead_swimlane: HW Engineering\nsummary: The DV test task.\n---\n# DV testing\n\nThe hardware lead runs DV testing.\n", encoding="utf-8")
        (self.vault / "Tasks" / "EMC.md").write_text(
            "---\nkind: process\nlead_swimlane: HW Engineering\n---\n# EMC\n\nThe hardware lead books the EMC chamber.\n", encoding="utf-8")
        (self.vault / "Tasks" / "Budget review.md").write_text(
            "---\nkind: process\nlead_swimlane: Finance\n---\n# Budget review\n\nFinance reviews the budget.\n", encoding="utf-8")
        # Two long manuals (spec 5.6): the vault does not fit the ceiling, so the model ranks and the checker runs.
        (self.vault / "Tasks" / "Manual A.md").write_text(
            "---\nkind: guide\n---\n# Manual A\n\n" + "Alpha manual line. " * 400 + "\n", encoding="utf-8")
        (self.vault / "Tasks" / "Manual B.md").write_text(
            "---\nkind: guide\n---\n# Manual B\n\n" + "Bravo manual line. " * 400 + "\n", encoding="utf-8")
        self.config_path = Path(self.tmp.name) / "config.local.json"
        self.config = {"provider": {"models": {"board": "fake/m"}},
                       "knowledge": {"vault_path": str(self.vault), "roles_folder": str(self.vault / "Roles"), "token_budget": 3000},
                       "ask": {"max_read_tokens": 17400},      # 2,400 of pages under the reserve of 5.9
                       "server": {"history_folder": str(Path(self.tmp.name) / "history")}}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.provider = RoutingFakeProvider()
        self.httpd, self.board_server = create_http_server(self.config, self.config_path, port=0, provider=self.provider)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    call = TestHistory.call

    def until(self, tid, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, state = self.call("GET", f"/api/ask/{tid}")
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail(f"waited in vain; phase {state['phase']}")

    def test_the_table_of_contents_has_every_page_with_its_properties_and_the_role_pages(self):
        table = knowledge_mod.contents(self.config)
        paths = [p["path"] for p in table["pages"]]
        self.assertIn("Roles/HW Engineering.md", paths)              # 5.3: "who is responsible" lives there
        self.assertIn("Tasks/DV testing.md", paths)
        lines = "\n".join(knowledge_mod.contents_lines(table["pages"]))
        self.assertIn("aliases: Cleder Gomes", lines)
        self.assertIn("lead: HW Engineering", lines)
        self.assertIn("summary: The DV test task.", lines)
        self.assertGreater(table["tokens"], 0)
        self.assertFalse(table["trimmed"])
        self.assertEqual(knowledge_mod.expand_rules([{"kind": "process", "lead": "hw engineering"}], table["pages"]),
                         ["Tasks/DV testing.md", "Tasks/EMC.md"])
        self.assertEqual(knowledge_mod.expand_rules([{"colour": "blue"}], table["pages"]), [])

    def test_a_question_waits_on_the_picks_screen_and_the_rule_reads_every_task_of_the_role(self):
        self.provider.pick_answer = json.dumps({"members": [{
            "member": "Ask the vault",
            "read": ["Roles/HW Engineering.md", {"kind": "process", "lead": "HW Engineering", "why": "every task that role leads"}, "Nowhere.md"],
            "reasons": {"Roles/HW Engineering.md": "names the holder of the role"}}]})
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        status, state = self.call("POST", f"/api/ask/{tid}/question", {"question": "Which VPDS tasks is Cleder Gomes responsible for?"})
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "choosing")
        state = self.until(tid, lambda s: s["phase"] == "picks")
        self.assertFalse(state["busy"])
        self.assertEqual(state["turns"], [])                          # nothing read yet
        self.assertEqual(state["picks"]["full"], ["Roles/HW Engineering.md", "Tasks/DV testing.md", "Tasks/EMC.md"])
        self.assertEqual(state["picks"]["reasons"]["Tasks/EMC.md"], "every task that role leads")
        self.assertEqual(state["pick_dropped"], 1)                     # Nowhere.md
        self.assertGreater(state["contents_tokens"], 0)
        # The estimate shows the choice with its reasons, and a follow-up question would be refused.
        self.assertFalse(state["whole_vault"])
        _, est = self.call("POST", f"/api/ask/{tid}/estimate", {"question": state["pending_question"]})
        self.assertEqual(est["picked_by"], "model")
        paths = [s["path"] for s in est["pages"]]
        self.assertEqual(paths[:3], ["Roles/HW Engineering.md", "Tasks/DV testing.md", "Tasks/EMC.md"])   # the ranking leads
        self.assertIn("Tasks/Budget review.md", paths)                     # 5.6: then the rest of the vault, up to the ceiling
        self.assertEqual([o["path"] for o in est["beyond"]], ["Tasks/Manual B.md"])   # what the ceiling cut
        self.assertEqual(est["pages"][0]["reason"], "names the holder of the role")
        ranking = [p for p in self.provider.prompts if "## Candidate sections" in p][-1]
        self.assertIn("## The vault is larger than one read", ranking)
        status, _ = self.call("POST", f"/api/ask/{tid}/question", {"question": "Another?"})
        self.assertEqual(status, 409)
        # Read: the answer call carries the role page, both of that role's tasks and the rest that fits.
        status, state = self.call("POST", f"/api/ask/{tid}/read", {})
        self.assertEqual(status, 200)
        state = self.until(tid, lambda s: s["phase"] == "idle" and not s["busy"])
        prompt = [p for p in self.provider.prompts if "## Question to the vault" in p][-1]
        self.assertIn("aliases: [Cleder Gomes]", prompt)
        self.assertIn("The hardware lead runs DV testing.", prompt)
        self.assertIn("books the EMC chamber", prompt)
        self.assertIn("Finance reviews the budget", prompt)
        self.assertNotIn("Bravo manual line", prompt)
        turn = state["turns"][0]
        self.assertEqual(turn["chosen_by"], "model")
        self.assertEqual(turn["left"], ["Tasks/Manual B.md"])
        self.assertEqual(turn["pick_reasons"]["Roles/HW Engineering.md"], "names the holder of the role")
        self.assertEqual([c["step"] for c in state["stats"]["calls"]], ["knowledge pick", "ask the vault", "answer check"])
        self.assertIn("## Table of contents of the vault", prompt)      # 5.5, decision 3: only on a partial read
        self.assertIn("- page: Tasks/Manual B.md", prompt)

    def test_the_ceiling_is_the_only_cap_and_what_it_cuts_is_listed(self):
        self.provider.pick_answer = json.dumps({"members": [{"member": "Ask the vault",
            "read": ["Tasks/DV testing.md", "Tasks/EMC.md", "Tasks/Budget review.md"], "reasons": {}}]})
        self.board_server.config.setdefault("ask", {})["max_read_tokens"] = 15090
        try:
            _, created = self.call("POST", "/api/ask", {})
            tid = created["id"]
            self.call("POST", f"/api/ask/{tid}/question", {"question": "What tasks are there?"})
            state = self.until(tid, lambda s: s["phase"] == "picks")
            _, est = self.call("POST", f"/api/ask/{tid}/estimate", {"question": state["pending_question"]})
            sent = [s["path"] for s in est["pages"]]
            self.assertLess(len(sent), 3)                                  # the ceiling cut the choice
            beyond = [o["path"] for o in est["beyond"]]
            self.assertEqual(len(sent + beyond), 7)                        # every page of the vault is in the order
            self.assertEqual(est["ceiling"], 90)      # the pages' share of it
            self.call("POST", f"/api/ask/{tid}/read", {})
            state = self.until(tid, lambda s: s["phase"] == "idle" and not s["busy"])
            self.assertEqual(sorted(state["turns"][0]["left"]), sorted(beyond))
        finally:
            self.board_server.config["ask"].pop("max_read_tokens", None)

    def test_cancel_on_the_picks_screen_keeps_the_thread_as_it_was(self):
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        self.call("POST", f"/api/ask/{tid}/question", {"question": "What tasks are there?"})
        self.until(tid, lambda s: s["phase"] == "picks")
        _, state = self.call("POST", f"/api/ask/{tid}/stop")
        self.assertEqual((state["phase"], state["busy"], state["picks"], state["turns"]), ("idle", False, None, []))

    def test_a_choice_that_names_nothing_leaves_the_ranking_in_force_and_says_so(self):
        self.provider.pick_answer = "{}"
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        self.call("POST", f"/api/ask/{tid}/question", {"question": "Who books the EMC chamber?"})
        state = self.until(tid, lambda s: s["phase"] == "picks")
        self.assertIsNone(state["picks"])
        self.assertIn("members", state["pick_error"])
        _, est = self.call("POST", f"/api/ask/{tid}/estimate", {"question": state["pending_question"]})
        self.assertEqual(est["picked_by"], "python")
        self.assertEqual([s["path"] for s in est["pages"]][0], "Tasks/EMC.md")   # the word ranking still puts it first

    # -- spec 5.4 to 5.6: follow-ups, kept pages and the loop on a partial read --

    SMALL = ["Org chart.md", "Roles/HW Engineering.md", "Tasks/Budget review.md", "Tasks/DV testing.md", "Tasks/EMC.md"]

    def _first_turn(self, tid, pick):
        self.provider.pick_answer = json.dumps({"members": [{"member": "Ask the vault", "read": pick, "reasons": {}}]})
        self.call("POST", f"/api/ask/{tid}/question", {"question": "Which VPDS tasks is Cleder Gomes responsible for?"})
        self.until(tid, lambda s: s["phase"] == "picks")
        self.call("POST", f"/api/ask/{tid}/read", {})
        return self.until(tid, lambda s: s["phase"] == "idle" and not s["busy"])

    def test_the_chooser_sees_the_whole_earlier_answer_and_the_pages_read_and_may_drop_a_kept_one(self):
        self.provider.ask_answer = json.dumps({"answer": "DV testing and EMC. " + "x" * 900, "sources": [], "gaps": [], "decision_question": False})
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        state = self._first_turn(tid, ["Roles/HW Engineering.md", "Tasks/DV testing.md", "Tasks/EMC.md"])
        self.assertEqual(state["turns"][0]["paths"], self.SMALL + ["Tasks/Manual A.md"])   # the ranking, then the rest; Manual B beyond
        self.assertEqual(state["turns"][0]["kept"], [])
        # The follow-up: the chooser drops the role page and names one task first.
        self.provider.pick_answer = json.dumps({"members": [{"member": "Ask the vault", "read": ["Tasks/EMC.md"], "reasons": {"Tasks/EMC.md": "named again"},
                                                             "drop": [{"path": "Roles/HW Engineering.md", "why": "the role is settled"}, "Nowhere.md"]}]})
        self.call("POST", f"/api/ask/{tid}/question", {"question": "List the exact wording of the tasks you listed."})
        state = self.until(tid, lambda s: s["phase"] == "picks")
        choosing = [p for p in self.provider.prompts if "## Candidate sections" in p][-1]
        self.assertIn("## Earlier in this thread", choosing)
        self.assertIn("x" * 900, choosing)                                     # 5.4, decision 1: no 600-character cut
        self.assertIn("## Pages already read in this thread\n\n", choosing)
        self.assertIn("- Tasks/DV testing.md", choosing)
        self.assertEqual(state["kept"], [p for p in self.SMALL if p != "Roles/HW Engineering.md"] + ["Tasks/Manual A.md"])   # 5.4: kept unless dropped
        self.assertEqual(state["dropped_kept"], {"Roles/HW Engineering.md": "the role is settled"})
        _, est = self.call("POST", f"/api/ask/{tid}/estimate", {"question": state["pending_question"]})
        paths = [s["path"] for s in est["pages"]]
        rows = {s["path"]: s for s in est["pages"]}
        self.assertEqual(paths[0], "Tasks/EMC.md")                               # chosen first
        self.assertEqual(paths[1:5], ["Org chart.md", "Tasks/Budget review.md", "Tasks/DV testing.md", "Tasks/Manual A.md"])   # then the kept ones
        self.assertEqual(paths[5], "Roles/HW Engineering.md")                    # the dropped one is read last, as it still fits
        self.assertFalse(rows["Tasks/EMC.md"]["kept"])
        self.assertTrue(rows["Tasks/DV testing.md"]["kept"])
        self.assertEqual([o["path"] for o in est["beyond"]], ["Tasks/Manual B.md"])
        self.call("POST", f"/api/ask/{tid}/read", {})
        state = self.until(tid, lambda s: s["phase"] == "idle" and not s["busy"])
        prompt = [p for p in self.provider.prompts if "## Question to the vault" in p][-1]
        self.assertIn("## Pages read earlier in this thread\n\n- Org chart.md\n- Roles/HW Engineering.md\n", prompt)
        self.assertIn("The hardware lead runs DV testing.", prompt)             # the kept page is read again
        self.assertEqual(state["turns"][1]["kept"], [p for p in self.SMALL if p != "Roles/HW Engineering.md" and p != "Tasks/EMC.md"] + ["Tasks/Manual A.md"])

    def test_a_failed_choice_keeps_every_earlier_page(self):
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        self._first_turn(tid, ["Tasks/EMC.md"])
        self.provider.pick_answer = "{}"
        self.call("POST", f"/api/ask/{tid}/question", {"question": "And the budget review?"})
        state = self.until(tid, lambda s: s["phase"] == "picks")
        self.assertEqual(state["kept"], self.SMALL + ["Tasks/Manual A.md"])
        _, est = self.call("POST", f"/api/ask/{tid}/estimate", {"question": state["pending_question"]})
        paths = [s["path"] for s in est["pages"]]
        self.assertEqual(paths[0], "Tasks/Budget review.md")                    # the word ranking stands in for the choice
        self.assertIn("Tasks/EMC.md", paths)
        self.assertEqual(est["picked_by"], "python")

    def test_the_check_swaps_in_a_page_the_ceiling_left_out_and_stops_when_content(self):
        first = json.dumps({"answer": "Alpha only.", "sources": [{"path": "Tasks/Manual A.md", "heading": "", "why": "alpha"}],
                            "gaps": ["bravo"], "decision_question": False, "missing": ["Roles/HW Engineering.md"]})
        later = json.dumps({"answer": "Alpha and bravo.", "sources": [{"path": "Tasks/Manual B.md", "heading": "", "why": "bravo"},
                                                                      {"path": "Roles/HW Engineering.md", "heading": "", "why": "the role"}],
                            "gaps": [], "decision_question": False, "missing": []})
        self.provider.ask_answer, self.provider.second_answer = first, later
        self.provider.check_answers = [
            json.dumps({"complete": False, "read": ["Tasks/Manual B.md", "Roles/HW Engineering.md", "Nowhere.md"],
                        "reasons": {"Tasks/Manual B.md": "the bravo manual was not read"}, "note": "Manual B was left unread."}),
            json.dumps({"complete": True, "read": [], "note": "The pages read cover the question."}),
        ]
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        state = self._first_turn(tid, ["Roles/HW Engineering.md"])
        turn = state["turns"][0]
        self.assertEqual(turn["answer"], "Alpha and bravo.")
        self.assertEqual(len(turn["rounds"]), 2)
        self.assertEqual(turn["rounds"][0]["answer"], "Alpha only.")
        self.assertEqual(turn["rounds"][0]["check"]["wanted"], ["Tasks/Manual B.md"])   # the role page was read; Nowhere dropped
        self.assertEqual(turn["rounds"][0]["check"]["dropped"], 1)
        self.assertEqual(turn["rounds"][0]["check"]["note"], "Manual B was left unread.")
        self.assertEqual(turn["rounds"][1]["read"], ["Tasks/Manual B.md"])            # 5.6, decision 4: swapped in ...
        self.assertEqual(turn["left"], ["Tasks/Manual A.md"])                          # ... and the lowest-ranked fell out
        self.assertTrue(turn["rounds"][1]["check"]["complete"])
        self.assertIn("Tasks/Manual B.md", turn["paths"])
        self.assertEqual([s["path"] for s in turn["sources"]], ["Tasks/Manual B.md", "Roles/HW Engineering.md"])
        self.assertFalse(turn["cap_hit"])
        self.assertEqual([c["step"] for c in state["stats"]["calls"]],
                         ["knowledge pick", "ask the vault", "answer check", "ask the vault", "answer check"])
        prompts = [p for p in self.provider.prompts if "## Question to the vault" in p]
        self.assertIn("## Your answer so far (round 1)\n\nAlpha only.", prompts[-1])
        self.assertIn("What the check said: Manual B was left unread.", prompts[-1])
        self.assertIn("Bravo manual line", prompts[-1])
        self.assertNotIn("Alpha manual line", prompts[-1].split("## Table of contents", 1)[0])
        check = [p for p in self.provider.prompts if "## Answer to check" in p][0]
        self.assertIn("## Pages read\n\n- Org chart.md\n", check)
        self.assertIn("- page: Tasks/Manual B.md", check)

    def test_the_loop_stops_at_the_round_cap_and_says_what_was_still_wanted(self):
        self.provider.check_answers = [json.dumps({"complete": False, "read": ["Tasks/Manual B.md"], "note": "wants B"}),
                                       json.dumps({"complete": False, "read": ["Tasks/Manual A.md"], "note": "wants A"})]
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        state = self._first_turn(tid, ["Roles/HW Engineering.md"])
        turn = state["turns"][0]
        self.assertEqual(len(turn["rounds"]), 2)                                       # ask.max_reads defaults to 2
        self.assertTrue(turn["cap_hit"])
        self.assertEqual(turn["still_wanted"], ["Tasks/Manual A.md"])
        self.assertEqual([c["step"] for c in state["stats"]["calls"]][-4:], ["ask the vault", "answer check", "ask the vault", "answer check"])
        # The next question keeps Manual B (read in round 2 of the first turn); a check that wants it has nothing to swap.
        self.provider.check_answers = [json.dumps({"complete": False, "read": ["Tasks/Manual B.md"], "note": "wants B, which is kept"})]
        state = self._first_turn(tid, ["Tasks/EMC.md"])
        self.assertIn("Tasks/Manual B.md", state["turns"][1]["kept"])
        self.assertEqual(state["turns"][1]["rounds"][0]["check"]["wanted"], [])         # already read: nothing to swap
        self.assertEqual(len(state["turns"][1]["rounds"]), 1)

    def test_a_check_that_does_not_parse_ends_the_loop_with_the_answer_as_it_stands(self):
        self.provider.check_answers = ["not json"]
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        state = self._first_turn(tid, ["Tasks/EMC.md"])
        turn = state["turns"][0]
        self.assertEqual(len(turn["rounds"]), 1)
        self.assertIn("JSON", turn["rounds"][0]["check"]["error"])
        self.assertEqual(turn["answer"], "- Tooling is late.")

    def test_the_steps_carry_the_rounds_with_numbers(self):
        self.provider.pick_answer = json.dumps({"members": [{"member": "Ask the vault", "read": ["Tasks/DV testing.md"], "reasons": {}}]})
        _, created = self.call("POST", "/api/ask", {})
        tid = created["id"]
        _, state = self.call("POST", f"/api/ask/{tid}/question", {"question": "What tasks are there?"})
        self.assertEqual([s["state"] for s in state["steps"]], ["current", "todo", "todo", "todo", "todo"])
        state = self.until(tid, lambda s: s["phase"] == "picks")
        self.assertEqual([(s["key"], s["state"]) for s in state["steps"]][:2], [("choosing", "done"), ("picks", "current")])
        self.assertEqual([s["key"] for s in state["steps"]], ["choosing", "picks", "read1", "ask1", "check1"])
        self.call("POST", f"/api/ask/{tid}/read", {})
        state = self.until(tid, lambda s: s["phase"] == "idle" and not s["busy"])
        self.assertEqual(state["steps"], [])
        self.assertEqual(state["turns"][0]["rounds"][0]["read"][0], "Tasks/DV testing.md")


class TestWholeVaultBoard(unittest.TestCase):
    """Spec 5.8: every member receives the whole vault, the confirm screen
    says so, no call is spent choosing, and the page follows each member as
    it writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        (self.vault / "Tooling.md").write_text("---\ntitle: Tooling\n---\nTooling is late.\n", encoding="utf-8")
        (self.vault / "Budget.md").write_text("---\ntitle: Budget\n---\n180k left.\n", encoding="utf-8")
        (self.vault / "Gates.md").write_text("---\ntitle: Gates\n---\nMG3 in March.\n", encoding="utf-8")
        make_roles(self.vault / "Roles&Responsibilities")
        self.config_path = Path(self.tmp.name) / "config.local.json"
        self.config = {"provider": {"models": {"board": "fake/m"}},
                       "knowledge": {"vault_path": str(self.vault), "token_budget": 40, "selection": "ai"},
                       "server": {"history_folder": str(Path(self.tmp.name) / "history")}}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.provider = RoutingFakeProvider()
        self.httpd, self.board_server = create_http_server(self.config, self.config_path, port=0, provider=self.provider)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    call = TestHistory.call

    def until(self, sid, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, state = self.call("GET", f"/api/sessions/{sid}")
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail(f"waited in vain; phase {state['phase']}")

    def _to_confirm(self):
        _, state = self.call("POST", "/api/sessions", {"question": "Rework the tooling before MG4?"})
        sid = state["id"]
        self.until(sid, lambda s: s["phase"] == "questions")
        self.call("POST", f"/api/sessions/{sid}/answers", {"answers": [""], "final": True})
        return self.until(sid, lambda s: s["phase"] == "confirm")["id"]

    def test_the_confirm_screen_reads_the_whole_vault_and_spends_no_call_on_choosing(self):
        first = len(self.provider.prompts)
        sid = self._to_confirm()
        _, est = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": CLASSIC[:2], "budget": 40})
        self.assertTrue(est["whole_vault"])                                # 5.8, decision 1
        paths = [row["path"] for row in est["pages"]]
        for page in ("Tooling.md", "Budget.md", "Gates.md"):
            self.assertIn(page, paths)
        self.assertTrue(any(row["path"].startswith("Roles") for row in est["pages"]))
        self.assertNotIn("knowledge pick", [c["label"] for c in est["per_call"]])
        self.assertEqual(self.call("GET", f"/api/sessions/{sid}")[1]["pick_state"], "idle")
        self.assertNotIn("## Candidate sections", "".join(self.provider.prompts[first:]))
        # A page unticked is left out of every member's read.
        _, est = self.call("POST", f"/api/sessions/{sid}/estimate", {"members": CLASSIC[:2], "exclude": ["Budget.md"]})
        self.assertNotIn("Budget.md", [row["path"] for row in est["pages"]])

    def test_every_member_is_sent_every_page_and_writes_on_the_screen(self):
        sid = self._to_confirm()
        self.provider.delay = 0.4
        try:
            self.call("POST", f"/api/sessions/{sid}/run", {"topic": "Rework?", "members": CLASSIC[:2]})
            live = {}
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                _, state = self.call("GET", f"/api/sessions/{sid}")
                live.update(state.get("live") or {})
                if state["phase"] == "result":
                    break
                time.sleep(0.05)
        finally:
            self.provider.delay = 0.0
        state = self.until(sid, lambda s: s["phase"] == "result")
        for member in CLASSIC[:2]:
            self.assertEqual(live.get(member), f"{member} is thinking")     # 5.8, decision 5
            sent = state["member_knowledge_paths"][member]
            for page in ("Tooling.md", "Budget.md", "Gates.md"):
                self.assertIn(page, sent)                                  # 5.8, decision 1
        self.assertEqual(state["live"], {})                                # gone once the entries stand
        self.assertNotIn("knowledge pick", [c["step"] for c in state["stats"]["calls"]])
        prompts = [p for p in self.provider.prompts if "Member: " in p and "## Your earlier assessment" not in p]
        self.assertGreaterEqual(len(prompts), 2)
        for prompt in prompts[-2:]:
            self.assertIn("Tooling is late.", prompt)
            self.assertIn("180k left.", prompt)                            # the same block for each

