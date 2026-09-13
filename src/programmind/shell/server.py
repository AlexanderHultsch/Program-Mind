"""The local HTTP server behind the browser interface (spec section 10).

Standard library only: ``http.server`` bound to ``127.0.0.1``, one HTML
page with its script and stylesheet served from ``web/``, and a small JSON
API the page polls. No framework, no build step, no dependency - the
decision of 8 September 2026 allows dependencies that run on any company
machine, and none is needed for this.

Every model call runs in a background thread and the page polls the
session's state once a second, so the member avatars fill in as each
member returns. The board itself is ``board.run_board`` - the same function
the CLI calls; this module adds the clarifier in front of it, the
knowledge block from the vault, the follow-up loop, and the confirmed
memory write on close.

Sessions live in memory for the life of the server process. Nothing is
written to disk except the audit entry ``run_board`` already writes and
the one vault note Alex confirms on closing a topic.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from programmind import __version__
from programmind.ai.opencode_client import opencode_config_problem
from programmind.ai.opencode_server import stop_shared
from programmind.agents.board import clarify as clarify_mod
from programmind.knowledge import knowledge as knowledge_mod
from programmind.memory import history as history_mod
from programmind.memory import memory_writer
from programmind.agents.ask import ask as ask_mod
from programmind.knowledge import picker
from programmind.agents.board import roles as roles_mod
from programmind.ai.opencode_client import stop_call
from programmind.ai import provider as ai_provider
from programmind.ai.provider import TASK_BOARD, AiNotConfiguredError, AiProvider, AiResult, build_provider
from programmind.memory.audit import log_run
from programmind.agents.board.board import (
    BoardConversation, BoardResult, MemberAssessment, ask_follow_up_full, prompt_sizes, role_terms,
    run_board, run_board_combined,
)

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SPEC_FILE = PACKAGE_DIR.parents[1] / "docs" / "spec.md"   # served at /spec so About works without the internet
REPOSITORY = "https://github.com/AlexanderHultsch/program-mind"
WEB_DIR = PACKAGE_DIR / "web"                       # the shell: index.html, shell.js, style.css
AGENTS_DIR = PACKAGE_DIR / "agents"                 # each agent ships its script under agents/<name>/web/
DEFAULT_PORT = 8765
BOARD_KIND = "board"                              # the ``kind`` of a board topic in the history folder
DEFAULT_SITE_NAME = "program-mind"                 # http://program-mind.localhost:8765/ (spec 11.1, decision 1)
STATUS_CHECK_PROMPT = "Reply with the single word OK."   # the confirmed test call of the AI status icon (11.1, decision 7)
_GATE_LINE = re.compile(r"\bM[GP]\s?\d{1,2}\b", re.I)   # a line of the project page that names a gate or a phase
_TABLE_RULE = re.compile(r"^\|[\s:|-]+\|$")             # the ---|--- row under a table header
_GATE_LINES = 12                                        # at most this many rows in the hover card

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _get(config: dict, dotted: str, default: Any = None) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node not in ("", None) else default


def _set(config: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def _call_label(prompt: str, phase: str) -> tuple[str, str]:
    """What a model call was for, read from the markers each prompt builder
    puts in (the same markers the tests route on): a step name and, for a
    member call, the member. Deterministic, no model involved."""
    if picker.MARKER_CHECK in prompt:
        return ("answer check", "")                    # spec 5.5, decision 10
    if picker.MARKER in prompt:
        return ("knowledge pick", "")
    if ask_mod.MARKER in prompt:
        return ("ask the vault", "")
    if "## Question from Alex" in prompt:
        return ("clarifier", "")
    if "## Vault outline" in prompt:
        return ("memory proposal", "")
    if "## Members to ask again" in prompt:
        return ("follow-up, combined", "")
    if "## Members to assess" in prompt:
        return ("board, combined", "")
    if "## Your earlier assessment" in prompt:
        return ("follow-up, member", prompt.split("Member: ", 1)[1].splitlines()[0] if "Member: " in prompt else "")
    if "## New question" in prompt:
        return ("follow-up, board", "")
    if "## Assessments" in prompt:
        return ("synthesis", "")
    if "## Member (FR-3.3a)" in prompt or "Member: " in prompt:
        return ("member", prompt.split("Member: ", 1)[1].splitlines()[0] if "Member: " in prompt else "")
    return (phase or "call", "")


class RecordingProvider(AiProvider):
    """Wraps the real provider for one session and writes every call's
    tokens and duration into the session's statistics (decided
    9 September 2026: how much did this question cost, step by step)."""

    def __init__(self, inner: AiProvider, session: "Session") -> None:
        self._inner = inner
        self._session = session

    def complete(self, task: str, prompt: str, on_text=None) -> AiResult:
        started = time.monotonic()
        step, member = _call_label(prompt, self._session.phase)
        session = self._session
        estimated = knowledge_mod.estimate_tokens(prompt)
        if session.cancelled.is_set():
            raise RuntimeError("stopped by Alex")
        tid = threading.get_ident()
        with session.lock:
            session.active_threads.add(tid)
        try:
            result = self._inner.complete(task, prompt, on_text)
        except Exception as exc:
            if not session.cancelled.is_set():
                session.record_call(step, member, None, None, time.monotonic() - started, error=str(exc)[:120])
            raise
        finally:
            with session.lock:
                session.active_threads.discard(tid)
        duration = result.duration_seconds if result.duration_seconds else time.monotonic() - started
        session.record_call(step, member, result.input_tokens, result.output_tokens, duration, estimated=estimated)
        return result


MAX_BUDGET = 12000                # the slider's top: knowledge tokens per member
MAX_BODY_BYTES = 4 * 1024 * 1024  # a request body larger than this is refused
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_CALL_OVERHEAD = 6300      # tokens per call beyond the prompt, seen on the gateway on 9 September 2026


def site_name(config: dict) -> str:
    """``server.site_name``, lower-cased and stripped to letters, digits,
    hyphens and dots; ``"program-mind"`` by default and when nothing is
    left (spec 9.5 decision 21, spec 11.1 decision 1)."""
    raw = str(_get(config, "server.site_name", "") or "").lower()
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "-.").strip(".-")
    return cleaned or DEFAULT_SITE_NAME


def site_host(config: dict) -> str:
    """The host the browser is sent to.

    A plain name becomes a name under ``.localhost``
    (``program-mind.localhost``): every name under that suffix resolves to
    this machine in Chrome, Edge and Firefox, with no hosts file and no
    admin rights, which is what a locked-down company laptop allows.

    A name that already carries a dot is used as it stands
    (``program-mind.local``, ``mind.visteon.net``). Nothing here makes such
    a name resolve: that needs a hosts-file entry or an mDNS responder, and
    both need rights this program does not have. The server still binds to
    127.0.0.1 only, so a name that does not point here reaches nothing."""
    name = site_name(config)
    return name if "." in name else f"{name}.localhost"


def _selection_value(value: Any) -> str:
    """``"python"`` when asked for, else ``"ai"`` (the default, decided 10 September 2026)."""
    return "python" if str(value or "").strip().lower() == "python" else "ai"


def _settled_phase(phase: str, session: "Session") -> str:
    """The phase a topic read from disk can be worked from. A call that was
    running when the server stopped cannot be resumed, and a memory proposal
    was never written to disk, so both come back one step earlier."""
    if phase in ("closed", "written", "result", "confirm", "questions", "error"):
        return phase
    if session.result is not None:
        return "result"
    if session.inputs and session.clarification is not None:
        return "confirm"
    if session.clarification is not None:
        return "questions"
    return "error"


def _restore_last_round(session: "Session") -> None:
    """Under the session lock: back to the last round of questions with its
    answers. The clarifier may have replaced the questions before failing;
    the questions Alex answered are the ones to show again."""
    last = session.rounds.pop()
    session.clarification.questions = list(last["questions"])
    session.answers = list(last["answers"])
    session.phase = "questions"
    session.error = None


def _pending_turn(session: "Session", index: int) -> bool:
    """Under the session lock: whether the follow-up at ``index`` is still
    the pending one this worker was started for (Back may have removed it)."""
    return (not session.cancelled.is_set() and 0 <= index < len(session.turns)
            and bool(session.turns[index].get("pending")))


class Session:
    """One topic, from the typed question to the closed topic."""

    def __init__(self, question: str, projects: list[str] | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.Lock()
        self.question = question
        self.projects: list[str] = list(projects or [])   # the project(s) this question is about (home page picker)
        self.extra: list[str] = []                        # section ids sent to every member on top of the budget
        self.exclude: list[str] = []                      # section ids never sent
        self.started = time.time()
        self.cancelled = threading.Event()             # set when Alex goes back while the model works
        self.active_threads: set[int] = set()          # threads waiting on a model call right now
        self.forward: list[str] = []                   # phases to go forward to again, newest last
        self.run_id = 0                                # bumped per run: a stopped run's late writes are dropped
        self.calls: list[dict[str, Any]] = []          # every model call: step, member, tokens, seconds
        self.marks: list[dict[str, Any]] = [{"phase": "started", "at": self.started}]   # phase changes, for wall time
        self.phase = "clarifying"
        self.error: str | None = None
        self.busy = False
        self.knowledge: dict[str, Any] = {"vault_path": None, "selected": 0, "total": 0, "tokens": 0,
                                          "truncated": False, "notes": []}
        self.knowledge_text = ""
        self.roles: dict[str, Any] = {"members": [], "count": 0, "source": "", "folder": None, "files": []}
        self.member_meta: list[dict[str, str]] = []
        self.clarification: clarify_mod.Clarification | None = None
        self.answers: list[str] = []
        self.rounds: list[dict[str, list[str]]] = []      # every clarification round: questions and answers
        self.inputs: dict[str, Any] = {}
        self.selected_members: list[str] = []             # the members Alex chose to ask
        self.members: dict[str, str] = {}
        self.partial: dict[str, dict[str, Any]] = {}      # answers already in while the others think
        self.mode = "individual"                          # "individual" or "combined" (decided 9 September 2026)
        self.budget: int | None = None                    # knowledge tokens per member for this topic (the slider)
        self.member_notes: dict[str, dict[str, str]] = {} # member -> note path -> text sent, full pages and brief lines
        self.member_paths: dict[str, list[str]] = {}      # member -> the pages sent in full
        # Spec 5.1 (decided 10 September 2026): how the knowledge is chosen.
        self.selection = "ai"                             # "ai": the model picks from Python's candidates; "python": Python alone
        self.picks: dict[str, dict[str, Any]] | None = None   # the model's picks per member, once made
        self.pick_state = "idle"                          # idle, running, done, failed
        self.pick_error: str | None = None
        self.pick_dropped = 0                             # ids the model named that were not candidates
        self.pick_id = 0                                  # which pick is current; an older pick's late result is dropped
        self.pick_prompt_chars = 0                        # size of the running pick's prompt, for the estimate
        self.knowledge_split: dict[str, dict[str, int]] = {}   # member -> core/own/brief tokens of the last run
        self.result: dict[str, Any] | None = None
        self.conversation: BoardConversation | None = None
        self.turns: list[dict[str, str]] = []
        self.llm_calls = 0
        self.proposal: memory_writer.MemoryProposal | None = None
        self.written_path: str | None = None
        self.discarded = False                            # Start over: the topic is dropped, not archived
        self.saved_stamp = 0.0                            # when the snapshot now on disk was taken

    @property
    def title(self) -> str:
        """The topic as the recent-work list names it: the clarified topic, else the question."""
        topic = str(self.inputs.get("topic") or "").strip() if self.inputs else ""
        return (topic or self.question.strip())[:120] or "New topic"

    @property
    def status(self) -> str:
        """``closed`` once the topic is closed, with or without a note; the
        archive lists those and the home page the rest (11.1, decision 10)."""
        return "closed" if self.phase in ("closed", "written") else "open"

    def to_record(self) -> dict[str, Any]:
        """The topic as it is kept on disk (11.1, decision 9): the inputs,
        the answers, the synthesis, the follow-ups, the statistics and the
        paths of the knowledge sent - never the knowledge text itself, which
        is the vault's and is re-read from it when a member is asked again."""
        with self.lock:
            clarification = None
            if self.clarification is not None:
                clarification = {"topic": self.clarification.topic, "context": self.clarification.context,
                                 "options": list(self.clarification.options),
                                 "constraints": list(self.clarification.constraints),
                                 "questions": list(self.clarification.questions),
                                 "parse_error": self.clarification.parse_error}
            return {
                "id": self.id, "kind": BOARD_KIND, "status": self.status, "phase": self.phase,
                "created": self.started, "updated": time.time(),
                "question": self.question, "projects": list(self.projects),
                "inputs": dict(self.inputs), "clarification": clarification,
                "answers": list(self.answers), "rounds": deepcopy(self.rounds),
                "selected_members": list(self.selected_members), "members": dict(self.members),
                "member_meta": deepcopy(self.member_meta), "roles": deepcopy(self.roles),
                "mode": self.mode, "budget": self.budget, "selection": self.selection,
                "extra": list(self.extra), "exclude": list(self.exclude),
                "knowledge": deepcopy(self.knowledge),
                "knowledge_paths": {m: sorted(paths) for m, paths in self.member_paths.items()},
                "knowledge_split": deepcopy(self.knowledge_split),
                "result": deepcopy(self.result), "turns": deepcopy(self.turns),
                "calls": list(self.calls), "marks": list(self.marks),
                "llm_calls": self.llm_calls, "written_path": self.written_path,
            }

    @staticmethod
    def from_record(data: dict[str, Any]) -> "Session":
        """A topic read back from disk. Nothing that was running survives a
        restart, so a topic caught mid-call comes back at the last step it
        can be worked from; the follow-up conversation is rebuilt from the
        result when the first follow-up needs it."""
        session = Session(str(data.get("question") or ""), [str(p) for p in data.get("projects") or []])
        session.id = str(data.get("id") or session.id)
        session.started = float(data.get("created") or time.time())
        session.marks = [m for m in data.get("marks") or [] if isinstance(m, dict)] or session.marks
        session.calls = [c for c in data.get("calls") or [] if isinstance(c, dict)]
        session.inputs = dict(data.get("inputs") or {})
        raw = data.get("clarification")
        if isinstance(raw, dict):
            session.clarification = clarify_mod.Clarification(
                topic=str(raw.get("topic") or ""), context=str(raw.get("context") or ""),
                options=[str(o) for o in raw.get("options") or []],
                constraints=[str(c) for c in raw.get("constraints") or []],
                questions=[str(q) for q in raw.get("questions") or []],
                parse_error=raw.get("parse_error"))
        session.answers = [str(a) for a in data.get("answers") or []]
        session.rounds = [r for r in data.get("rounds") or [] if isinstance(r, dict)]
        session.selected_members = [str(m) for m in data.get("selected_members") or []]
        session.members = {str(k): str(v) for k, v in (data.get("members") or {}).items()}
        session.member_meta = [m for m in data.get("member_meta") or [] if isinstance(m, dict)]
        session.roles = dict(data.get("roles") or session.roles)
        session.mode = "combined" if data.get("mode") == "combined" else "individual"
        session.budget = data.get("budget")
        session.selection = _selection_value(data.get("selection"))
        session.extra = [str(x) for x in data.get("extra") or []]
        session.exclude = [str(x) for x in data.get("exclude") or []]
        session.knowledge = dict(data.get("knowledge") or session.knowledge)
        session.member_paths = {str(m): [str(p) for p in paths]
                                for m, paths in (data.get("knowledge_paths") or {}).items()}
        session.knowledge_split = dict(data.get("knowledge_split") or {})
        session.result = data.get("result") if isinstance(data.get("result"), dict) else None
        session.turns = [t for t in data.get("turns") or [] if isinstance(t, dict)]
        session.llm_calls = int(data.get("llm_calls") or 0)
        session.written_path = data.get("written_path")
        session.phase = _settled_phase(str(data.get("phase") or ""), session)
        session.busy = False
        session.pick_state = "done" if session.result is not None else "idle"
        for turn in session.turns:
            if turn.get("pending"):                      # a call the stopped server never finished
                turn["pending"] = False
                turn["error"] = True
                turn["answer"] = turn.get("answer") or "The server stopped before this answer came back."
        return session

    def snapshot(self) -> dict[str, Any]:
        """The session as the page sees it. Polled once a second: only the
        parts mutated in place are copied; ``result`` and each turn are
        replaced wholesale when written and shared as they are."""
        with self.lock:
            clarification = None
            if self.clarification is not None:
                clarification = {
                    "topic": self.clarification.topic,
                    "context": self.clarification.context,
                    "options": list(self.clarification.options),
                    "constraints": list(self.clarification.constraints),
                    "questions": list(self.clarification.questions),
                    "parse_error": self.clarification.parse_error,
                }
            proposal = None
            if self.proposal is not None:
                proposal = {
                    "path": self.proposal.path,
                    "title": self.proposal.title,
                    "tags": list(self.proposal.tags),
                    "body": self.proposal.body,
                    "mode": self.proposal.mode,
                    "parse_error": self.proposal.parse_error,
                    "preview": memory_writer.preview(self.proposal),
                }
            return {
                "id": self.id,
                "question": self.question,
                "phase": self.phase,
                "busy": self.busy,
                "error": self.error,
                "knowledge": deepcopy(self.knowledge),
                "roles": deepcopy(self.roles),
                "member_meta": self.member_meta,
                "clarification": clarification,
                "answers": list(self.answers),
                "rounds": deepcopy(self.rounds),
                "max_rounds": clarify_mod.MAX_ROUNDS,
                "inputs": dict(self.inputs),
                "selected_members": self.selected_members,
                "members": deepcopy(self.members),
                "partial": deepcopy(self.partial),
                "mode": self.mode,
                "budget": self.budget,
                "projects": self.projects,
                "extra": self.extra,
                "exclude": self.exclude,
                "member_knowledge_paths": {m: sorted(paths) for m, paths in self.member_paths.items()},
                "selection": self.selection,
                "pick_state": self.pick_state,
                "pick_error": self.pick_error,
                "pick_dropped": self.pick_dropped,
                "picks": deepcopy(self.picks),
                "knowledge_split": deepcopy(self.knowledge_split),
                "stats": {"calls": list(self.calls), "marks": list(self.marks), "started": self.started},
                "nav": self._nav(),
                "result": self.result,
                "turns": list(self.turns),
                "llm_calls": self.llm_calls,
                "proposal": proposal,
                "written_path": self.written_path,
            }

    def _nav(self) -> dict[str, bool]:
        """What the Back and Forward buttons may do in this state (under the lock)."""
        working = self.phase in ("clarifying", "running", "synthesising", "proposing") or self.busy
        back = working or self.phase in ("confirm", "result", "proposal", "error") or \
            (self.phase == "questions" and len(self.rounds) > 0)
        return {"back": back, "forward": bool(self.forward) and not working}

    def stop_work(self) -> int:
        """Stop every model call this session is waiting on. Returns how many
        were running. The threads end on their own with an error the
        cancelled flag tells them to ignore."""
        self.cancelled.set()
        with self.lock:
            threads = list(self.active_threads)
        return sum(1 for tid in threads if stop_call(tid))

    def fail(self, message: str) -> None:
        if self.cancelled.is_set():
            return          # Alex went back: the failure is the stop, not an error to show
        with self.lock:
            self.phase = "error"
            self.error = message
            self.busy = False
            self.marks.append({"phase": "error", "at": time.time()})

    def mark(self, phase: str) -> None:
        """Under the caller's lock or not - appending is atomic enough."""
        self.marks.append({"phase": phase, "at": time.time()})

    def record_call(self, step: str, member: str, input_tokens: int | None, output_tokens: int | None,
                    seconds: float, error: str | None = None, estimated: int | None = None) -> None:
        with self.lock:
            self.calls.append({
                "n": len(self.calls) + 1, "step": step, "member": member, "phase": self.phase,
                "input_tokens": input_tokens, "output_tokens": output_tokens, "seconds": round(seconds, 1),
                "estimated": estimated, "error": error, "at": time.time(),
            })

    def overhead_per_call(self) -> tuple[int, int]:
        """What a call costs beyond its prompt (OpenCode's own system prompt
        and tool definitions), learned from this topic's real calls: the
        median of real input minus estimated prompt tokens. Returns
        ``(overhead, calls it was learned from)``; the default is what the
        company gateway reported on 9 September 2026."""
        with self.lock:
            deltas = sorted(c["input_tokens"] - c["estimated"] for c in self.calls
                            if c.get("input_tokens") is not None and c.get("estimated") is not None)
        if not deltas:
            return DEFAULT_CALL_OVERHEAD, 0
        return max(0, deltas[len(deltas) // 2]), len(deltas)


class AskSession:
    """One open thread of Ask the vault (spec section 10) while the server
    runs: the thread itself (persisted after every change), the running
    call, the memory step. Duck-typed to what ``RecordingProvider`` needs
    from a ``Session``: ``lock``, ``phase``, ``cancelled``,
    ``active_threads``, ``record_call``."""

    def __init__(self, thread: "ask_mod.Thread") -> None:
        self.thread = thread
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.active_threads: set[int] = set()
        self.phase = "idle"                 # idle, choosing, picks, asking, checking, reading_more, proposing, proposal, written
        self.busy = False
        self.error: str | None = None
        self.pending_question: str | None = None
        # Spec 5.3: the model's choice from the table of contents, waiting on the picks screen.
        self.picks: dict[str, Any] | None = None    # {"full": [ids or paths], "reasons": {id: why}, "drop": [...]}
        self.pick_error: str | None = None          # why the word ranking is in force instead
        self.pick_dropped = 0
        self.contents_tokens = 0
        self.contents_trimmed = False
        self.page_tokens: dict[str, int] = {}       # page path or section id -> size, from the table of contents
        self.table: dict[str, Any] | None = None    # the table of contents of the question (spec 5.5: the checker and the answer see it)
        # Spec 5.4, decision 2: the pages the thread has read that stay with it for this question.
        self.kept: list[str] = []
        self.dropped_kept: dict[str, str] = {}      # pages the chooser let go, with its reason
        # Spec 5.5: the loop's rounds; the steps carry them with real numbers.
        self.round = 0                              # the answer round running, 1-based
        self.read_counts: list[int] = []            # pages read per round, 0 while a round's read is not done
        # Spec 4.1: what the model has written so far, for the page's eye only - never stored.
        self.live = ""
        # Spec 5.6: the whole vault is read unless it does not fit the ceiling; then the model ranks and the checker runs.
        self.overflow = False
        self.page_count = 0                         # pages of the vault for the question
        self.proposal: memory_writer.MemoryProposal | None = None
        self.marks: list[dict[str, Any]] = []
        self.started = time.time()

    def mark(self, phase: str) -> None:
        self.marks.append({"phase": phase, "at": time.time()})

    def set_live(self, text: str) -> None:
        """What the model has written so far (spec 4.1, decision 3): shown
        on the page, never parsed and never stored."""
        with self.lock:
            self.live = text

    def record_call(self, step: str, member: str, input_tokens: int | None, output_tokens: int | None,
                    seconds: float, error: str | None = None, estimated: int | None = None) -> None:
        with self.lock:
            self.thread.calls.append({
                "n": len(self.thread.calls) + 1, "step": step, "member": member, "phase": self.phase,
                "input_tokens": input_tokens, "output_tokens": output_tokens, "seconds": round(seconds, 1),
                "estimated": estimated, "error": error, "at": time.time(),
            })

    def overhead_per_call(self) -> tuple[int, int]:
        with self.lock:
            deltas = sorted(c["input_tokens"] - c["estimated"] for c in self.thread.calls
                            if c.get("input_tokens") is not None and c.get("estimated") is not None)
        if not deltas:
            return DEFAULT_CALL_OVERHEAD, 0
        return max(0, deltas[len(deltas) // 2]), len(deltas)

    def stop_work(self) -> int:
        self.cancelled.set()
        with self.lock:
            threads = list(self.active_threads)
        return sum(1 for tid in threads if stop_call(tid))

    def clear_question(self) -> None:
        """Back to rest: nothing pending, nothing chosen. Under the lock."""
        self.busy, self.phase, self.pending_question = False, "idle", None
        self.picks, self.pick_error = None, None
        self.kept, self.dropped_kept = [], {}
        self.round, self.read_counts = 0, []
        self.overflow, self.page_count = False, 0
        self.live = ""

    def steps(self) -> list[dict[str, str]]:
        """The steps of the running question and which one it is on (spec
        5.4, decision 9; 5.5, decision 7: the rounds). Reading is Python's
        step and is over the moment the call starts, so "writing" is the
        one that waits. Empty when nothing runs. Under the lock."""
        phase = self.phase
        if phase not in ("choosing", "picks", "asking", "checking", "reading_more"):
            return []
        rows = ([("choosing", "Looking through the table of contents")] if self.overflow else []) + [("picks", "Your picks")]
        rounds = max(self.round, 1)
        for r in range(1, rounds + 1):
            n = self.read_counts[r - 1] if len(self.read_counts) >= r else 0
            notes = f"{n} note{'s' if n != 1 else ''}"
            if r == 1:
                rows += [("read1", f"Reading {notes}" if n else "Reading the notes"), ("ask1", "Writing the answer")]
                rows += [("check1", "Checking the answer")] if self.overflow else []      # 5.6: the check only on a partial read
            else:
                rows += [(f"read{r}", f"Reading {notes} more" if n else "Reading more notes"),
                         (f"ask{r}", "Writing the answer again"), (f"check{r}", "Checking again")]
        current = {"choosing": "choosing", "picks": "picks",
                   "asking": f"read{rounds}" if not (self.read_counts and self.read_counts[-1]) else f"ask{rounds}",
                   "checking": f"check{rounds}", "reading_more": f"read{rounds}"}[phase]
        keys = [key for key, _label in rows]
        at = keys.index(current)
        return [{"key": key, "label": label, "state": "done" if i < at else "current" if i == at else "todo"}
                for i, (key, label) in enumerate(rows)]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            thread = self.thread
            proposal = None
            if self.proposal is not None:
                proposal = {"path": self.proposal.path, "title": self.proposal.title, "tags": list(self.proposal.tags),
                            "body": self.proposal.body, "mode": self.proposal.mode, "parse_error": self.proposal.parse_error,
                            "preview": memory_writer.preview(self.proposal)}
            return {
                "id": thread.id, "kind": "ask", "title": thread.title, "status": thread.status,
                "phase": self.phase, "busy": self.busy, "error": self.error,
                "pending_question": self.pending_question,
                "picks": deepcopy(self.picks), "pick_error": self.pick_error, "pick_dropped": self.pick_dropped,
                "contents_tokens": self.contents_tokens, "contents_trimmed": self.contents_trimmed,
                "kept": list(self.kept), "dropped_kept": dict(self.dropped_kept), "steps": self.steps(),
                "round": self.round, "whole_vault": not self.overflow, "page_count": self.page_count,
                "live": self.live,
                "projects": list(thread.projects), "budget": thread.budget,
                "extra": list(thread.extra), "exclude": list(thread.exclude),
                "turns": deepcopy(thread.turns),
                "llm_calls": len([c for c in thread.calls if not c.get("error")]),
                "stats": {"calls": list(thread.calls), "marks": list(self.marks), "started": self.started},
                "proposal": proposal, "written_path": thread.written_path,
                "created": thread.created, "updated": thread.updated,
            }


class BoardServer:
    """State and behaviour behind the API; the HTTP handler only routes."""

    def __init__(self, config: dict, config_path: Path | None, *, provider: AiProvider | None = None) -> None:
        self.config = config
        self.config_path = config_path
        self._provider_override = provider
        self.sessions: dict[str, Session] = {}
        self.asks: dict[str, AskSession] = {}          # open Ask the vault threads, by id
        self.lock = threading.Lock()
        self.ai_check: dict[str, Any] | None = None    # the last confirmed test call of the AI status icon (spec 11.1, decision 7)

    # -- provider and config ------------------------------------------------

    def provider(self) -> AiProvider:
        if self._provider_override is not None:
            return self._provider_override
        provider = build_provider(self.config)
        if provider is None:
            raise AiNotConfiguredError(
                "No model configured. Open Options and set the model string (provider.models.board)."
            )
        return provider

    def config_view(self) -> dict[str, Any]:
        """The configuration as Options shows it, with the state of the vault and the roles folder."""
        vault_path = _get(self.config, "knowledge.vault_path")
        status: dict[str, Any] = {"configured": bool(vault_path), "ok": False, "notes": 0, "error": None}
        if vault_path:
            try:
                vault_dir = Path(str(vault_path)).expanduser()
                status["notes"] = len(knowledge_mod.load_vault(       # cached after the first walk
                    vault_dir, skip_subfolders=knowledge_mod._roles_inside(self.config, vault_dir)))
                status["ok"] = True
            except knowledge_mod.KnowledgeUnavailable as exc:
                status["error"] = str(exc)
        return {
            "config_path": str(self.config_path) if self.config_path else None,
            "vault_path": vault_path or "",
            "project": knowledge_mod.active_project(self.config) or "",
            "projects": knowledge_mod.list_projects(self.config),
            "token_budget": _get(self.config, "knowledge.token_budget", knowledge_mod.DEFAULT_TOKEN_BUDGET),
            "model": _get(self.config, "provider.models.board", "") or "",
            "token_limit": _get(self.config, "provider.token_limits.board"),
            "audit_folder": _get(self.config, "runtime.audit_folder", "") or "",
            "auto_approve": bool(_get(self.config, "provider.opencode.auto_approve", True)),
            "opencode_config": _get(self.config, "provider.opencode.config_file", "") or "",
            "theme": _get(self.config, "ui.theme", "system") or "system",
            "selection": _selection_value(_get(self.config, "knowledge.selection")),
            "site_name": site_name(self.config),
            "site_host": site_host(self.config),
            "version": __version__,
            "repository": REPOSITORY,
            "spec": SPEC_FILE.is_file(),
            "ask_budget": int(_get(self.config, "ask.token_budget", ask_mod.DEFAULT_TOKEN_BUDGET) or ask_mod.DEFAULT_TOKEN_BUDGET),
            "ask_ceiling": self._ceiling(), "ask_max_reads": self._max_reads(),
            "vault_name": self.vault_name(),
            "knowledge_status": status,
            "roles_folder": _get(self.config, "knowledge.roles_folder", "") or "",
            "roles_status": self._roles_status(),
        }

    def _roles_status(self) -> dict[str, Any]:
        folder, origin = knowledge_mod.resolve_roles_folder(self.config)
        try:
            board = roles_mod.load_board(self.config)
        except roles_mod.RolesUnavailable as exc:
            return {"error": str(exc), "members": [], "count": 0, "source": origin,
                    "folder": str(folder) if folder else None, "files": [], "skipped": []}
        info = roles_mod.summary(board)
        info["error"] = None
        return info

    def _install_target(self) -> Path:
        configured = _get(self.config, "knowledge.roles_folder")
        if configured:
            return Path(str(configured)).expanduser()
        vault = _get(self.config, "knowledge.vault_path")
        if vault:
            vault_dir = Path(str(vault)).expanduser()
            return knowledge_mod.detect_roles_folder(vault_dir) or (vault_dir / knowledge_mod.DEFAULT_ROLES_SUBFOLDER)
        raise ApiError(400, "Choose a roles folder (or a knowledge source) first.")

    def install_roles(self) -> dict[str, Any]:
        folder = self._install_target()
        try:
            written = roles_mod.install_support_files(folder)
        except OSError as exc:
            raise ApiError(500, f"Could not write to the roles folder: {exc}")
        status = self._roles_status()
        status["written"] = [str(path) for path in written]
        status["target"] = str(folder)
        return status

    def update_config(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Options saved: only the listed keys, folders checked, the file rewritten under the lock."""
        mapping = {
            "vault_path": ("knowledge.vault_path", str),
            "project": ("knowledge.project", str),
            "token_budget": ("knowledge.token_budget", int),
            "model": ("provider.models.board", str),
            "token_limit": ("provider.token_limits.board", lambda v: None if v in ("", None) else int(v)),
            "audit_folder": ("runtime.audit_folder", str),
            "auto_approve": ("provider.opencode.auto_approve", bool),
            "opencode_config": ("provider.opencode.config_file", str),
            "roles_folder": ("knowledge.roles_folder", str),
            "theme": ("ui.theme", str),
            "selection": ("knowledge.selection", _selection_value),
        }
        with self.lock:
            for key, (dotted, cast) in mapping.items():
                if key in changes:
                    try:
                        value = cast(changes[key]) if changes[key] is not None else None
                    except (TypeError, ValueError):
                        raise ApiError(400, f"{key}: not a valid value")
                    if isinstance(value, str):
                        value = value.strip()
                    if key == "opencode_config" and value:
                        problem = opencode_config_problem(value)
                        if problem:
                            raise ApiError(400, f"OpenCode configuration file {problem}")
                    if key in ("vault_path", "roles_folder") and value and not Path(str(value)).expanduser().is_dir():
                        raise ApiError(400, f"{key}: not a folder: {value}")
                    _set(self.config, dotted, value)
            if self.config_path is not None:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.config_view()

    # -- sessions -------------------------------------------------------------

    def start_session(self, question: str, projects: Any = None) -> Session:
        """A new topic: the clarifier starts in the background at once."""
        question = (question or "").strip()
        if not question:
            raise ApiError(400, "Type a question first.")
        if isinstance(projects, list):
            chosen = [str(p).strip() for p in projects if str(p).strip()]
        else:
            chosen = knowledge_mod.active_projects(self.config)
        session = Session(question, chosen)
        session.selection = _selection_value(_get(self.config, "knowledge.selection"))
        with self.lock:
            self.sessions[session.id] = session
        self._spawn(session, self._clarify, session)
        return session

    def _spawn(self, session: Session, target, *args) -> None:
        """A background step whose crash must show up as the session's error,
        never as a page polling 'clarifying' forever. Whatever the step did,
        the topic on disk is brought up to date after it (11.1, decision 9);
        a thread saves itself as it always has."""
        def run() -> None:
            try:
                target(*args)
            except Exception as exc:   # noqa: BLE001 - the whole point is to surface anything
                session.fail(f"internal error: {exc!r}")
            finally:
                if isinstance(session, Session):
                    self.save_session(session)
        threading.Thread(target=run, daemon=True).start()

    def get_session(self, session_id: str) -> Session:
        """The open topic, from memory or from the history folder (11.1,
        decision 9): a reload, a restart or a click in the archive all reach
        the same topic."""
        with self.lock:
            session = self.sessions.get(session_id)
        if session is not None:
            return session
        try:
            data = self.topic_store().load(session_id)
        except ValueError:
            raise ApiError(404, "Unknown session - start a new topic.")
        if data is None or data.get("kind") != BOARD_KIND:
            raise ApiError(404, "Unknown session - start a new topic.")
        session = Session.from_record(data)
        with self.lock:
            session = self.sessions.setdefault(session.id, session)
        return session

    @staticmethod
    def _clarifier_query(session: Session) -> str:
        """What the clarifier's knowledge is ranked by (spec 5.2): the
        question, and everything the rounds so far asked and answered. The
        clarifier's own question is part of it - "who are the project
        managers" is what pulls the pages that name them."""
        parts = [session.question]
        for round_ in session.rounds:
            parts.extend(str(q) for q in round_.get("questions") or [])
            parts.extend(str(a) for a in round_.get("answers") or [] if str(a).strip())
        return "\n".join(parts)

    def _clarifier_knowledge(self, session: Session) -> bool:
        """Select the clarifier's block for what is known now. False when the
        vault cannot be read; the session is failed by then."""
        try:
            selection = knowledge_mod.gather(self.config, self._clarifier_query(session), projects=session.projects)
        except knowledge_mod.KnowledgeUnavailable as exc:
            session.fail(f"{exc}. Check the knowledge source in Options.")
            return False
        with session.lock:
            session.knowledge = {
                "vault_path": str(selection.vault_path) if selection.vault_path else None,
                "project": selection.project or "",
                "selected": len(selection.notes),
                "total": selection.total_notes,
                "tokens": selection.tokens,
                "truncated": selection.truncated,
                "notes": selection.relative_paths,
            }
            session.knowledge_text = selection.text
        return True

    def _clarify(self, session: Session) -> None:
        if not self._clarifier_knowledge(session):
            return
        try:
            board = roles_mod.load_board(self.config)
        except roles_mod.RolesUnavailable as exc:
            session.fail(f"{exc}. Choose the roles folder in Options.")
            return
        try:
            kpi = knowledge_mod.kpi_notes(self.config, list(board.profiles), projects=session.projects)
        except knowledge_mod.KnowledgeUnavailable:
            kpi = {}
        with session.lock:
            session.roles = roles_mod.summary(board)
            session.roles["kpi_members"] = sorted(kpi)
            session.member_meta = roles_mod.member_meta(board.profiles)
            session.members = {member: "pending" for member in board.profiles}
        try:
            provider = RecordingProvider(self.provider(), session)
            clarification = clarify_mod.clarify(provider, session.question, session.knowledge_text)
        except Exception as exc:   # any provider failure ends the session visibly
            session.fail(str(exc))
            return
        with session.lock:
            session.clarification = clarification
            session.llm_calls += 1
            session.phase = "questions"
            session.mark("questions")

    def answer(self, session: Session, answers: list[str], *, final: bool = False) -> None:
        """Alex's answers to the current round. Unless ``final`` (the "ask
        the board now" button) or the round limit is reached, the clarifier
        looks again and either asks more or declares the question clear
        (decided 9 September 2026: clarification loops until clear)."""
        with session.lock:
            # "confirm" is accepted too: the page's Back button returns to
            # the last round's questions and re-submits them.
            if session.phase not in ("questions", "confirm") or session.clarification is None:
                raise ApiError(409, "The board is not waiting for answers right now.")
            session.answers = [str(item) for item in answers]
            current = {"questions": list(session.clarification.questions), "answers": session.answers}
            if session.phase == "confirm" and session.rounds:
                session.rounds[-1] = current
            else:
                session.rounds.append(current)
            if final or len(session.rounds) >= clarify_mod.MAX_ROUNDS or session.phase == "confirm":
                self._to_confirm(session)
                return
            session.cancelled.clear()
            session.phase = "clarifying"
        self._spawn(session, self._clarify_more, session)

    def _to_confirm(self, session: Session) -> None:
        """Under the session lock: the FR-3.1 input from every round."""
        clarification = session.clarification
        rounds = [(r["questions"], r["answers"]) for r in session.rounds]
        session.inputs = {
            "topic": clarification.topic,
            "context": clarify_mod.merge_rounds(clarification.context, rounds),
            "options": list(clarification.options),
            "constraints": list(clarification.constraints),
        }
        session.phase = "confirm"
        session.mark("confirm")
        if session.selection == "ai":
            self._start_pick(session)      # the inputs are new: a pick made for earlier inputs is stale

    def _start_pick(self, session: Session) -> None:
        """Under the session lock: the AI-assisted pick runs in the background
        while Alex reads the confirm screen (spec 5.1). A pick still running
        for earlier inputs is superseded: its result is dropped when it
        lands (``pick_id``)."""
        session.pick_id += 1
        session.pick_state = "running"
        session.pick_error = None
        session.picks = None
        session.pick_prompt_chars = 0
        self._spawn(session, self._pick, session, session.pick_id)

    def pick(self, session: Session, body: dict[str, Any]) -> None:
        """The page asks for the pick: after switching the dropdown to AI
        assisted, or to pick again."""
        with session.lock:
            if session.phase != "confirm":
                raise ApiError(409, "The pick is made on the confirm screen.")
            if "selection" in body:
                session.selection = _selection_value(body.get("selection"))
            if session.selection != "ai":
                session.pick_state = "idle"
                return
            if session.pick_state == "running":
                return
            self._start_pick(session)

    def _pick(self, session: Session, pick_id: int) -> None:
        query = f"{session.question}\n{session.inputs.get('topic', '')}\n{session.inputs.get('context', '')}"
        try:
            board = roles_mod.load_board(self.config)
            table = knowledge_mod.contents(self.config, session.projects, question=query, include_roles=False)
            lines = {m: (role.perspective or role.title) for m, role in board.profiles.items()}
            with session.lock:
                if session.pick_id == pick_id:
                    session.pick_prompt_chars = len(picker.choose_prompt(query, lines, table["pages"], trimmed=table["trimmed"]))
            result = picker.choose(RecordingProvider(self.provider(), session), query, lines, table["pages"],
                                   trimmed=table["trimmed"])
        except Exception as exc:
            if session.cancelled.is_set():
                return
            with session.lock:
                if session.pick_id == pick_id:
                    session.pick_state = "failed"
                    session.pick_error = f"{exc}"[:200]
            return
        with session.lock:
            if session.pick_id != pick_id:
                return                    # a newer pick is current; this one answered stale inputs
            session.llm_calls += 1
            session.pick_dropped = result.dropped
            if result.ok:
                session.picks = result.picks
                session.pick_state = "done"
                session.pick_error = None
            else:
                session.picks = None
                session.pick_state = "failed"
                session.pick_error = result.error

    def _clarify_more(self, session: Session) -> None:
        rounds = [(r["questions"], r["answers"]) for r in session.rounds]
        if not self._clarifier_knowledge(session):    # spec 5.2: pages for what this round asked about
            return
        try:
            clarification = clarify_mod.clarify(RecordingProvider(self.provider(), session), session.question,
                                                session.knowledge_text, rounds)
        except Exception as exc:
            session.fail(str(exc))
            return
        with session.lock:
            session.llm_calls += 1
            # Keep what the earlier round extracted where the new one is thin.
            if not clarification.topic and session.clarification is not None:
                clarification.topic = session.clarification.topic
            if not clarification.context and session.clarification is not None:
                clarification.context = session.clarification.context
            if not clarification.options and session.clarification is not None:
                clarification.options = list(session.clarification.options)
            if not clarification.constraints and session.clarification is not None:
                clarification.constraints = list(session.clarification.constraints)
            session.clarification = clarification
            session.answers = []
            if clarification.questions:
                session.phase = "questions"
                session.mark("questions")
            else:
                self._to_confirm(session)

    def run(self, session: Session, inputs: dict[str, Any]) -> None:
        """Alex confirmed the input: the board runs in the background with the chosen members, mode and budget."""
        with session.lock:
            if session.phase != "confirm":
                raise ApiError(409, "The board is not ready to run.")
            topic = str(inputs.get("topic") or session.inputs.get("topic") or "").strip()
            if not topic:
                raise ApiError(400, "The topic must not be empty.")
            session.inputs = {
                "topic": topic,
                "context": str(inputs.get("context") or "").strip(),
                "options": [str(o).strip() for o in inputs.get("options") or [] if str(o).strip()],
                "constraints": [str(c).strip() for c in inputs.get("constraints") or [] if str(c).strip()],
            }
            session.selected_members = self._chosen_members(session, inputs.get("members"))
            session.mode = "combined" if str(inputs.get("mode") or "").lower() == "combined" else "individual"
            if "selection" in inputs:
                session.selection = _selection_value(inputs.get("selection"))
            session.budget = self._budget(inputs.get("budget"))
            session.extra = [str(x) for x in inputs.get("extra") or [] if str(x).strip()]
            session.exclude = [str(x) for x in inputs.get("exclude") or [] if str(x).strip()]
            session.cancelled.clear()
            session.run_id += 1
            session.forward = []
            session.result = None
            session.turns = []
            session.phase = "running"
            session.mark("running")
            session.members = {member: "pending" for member in session.selected_members}
        self._spawn(session, self._run_board, session)

    def _budget(self, value: Any) -> int:
        """The knowledge budget per member: the slider's value, else the
        configured default, clamped to what the slider allows."""
        default = int(_get(self.config, "knowledge.token_budget", knowledge_mod.DEFAULT_TOKEN_BUDGET) or 0)
        try:
            budget = int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            budget = default
        return max(0, min(budget, MAX_BUDGET))

    def _member_selections(self, session: Session, board, selected: list[str], budget: int,
                           extra: list[str] | None = None, exclude: list[str] | None = None) -> dict:
        """One knowledge selection per chosen member for this topic (decided
        9 September 2026): the sections ranked by the topic and by the
        member's own terms, within ``budget`` tokens each, plus the manual
        picks on top and minus the exclusions."""
        query = f"{session.question}\n{session.inputs.get('topic', '')}"
        terms = {m: role_terms(board.profiles.get(m)) for m in selected}
        with session.lock:
            picks = session.picks if session.selection == "ai" and session.pick_state == "done" else None
        return knowledge_mod.gather_for_members(
            self.config, query, terms, token_budget=max(budget, 1),
            projects=session.projects,
            extra=list(extra if extra is not None else session.extra),
            exclude=list(exclude if exclude is not None else session.exclude), picks=picks)

    @staticmethod
    def _blocks_from(selections: dict) -> dict[str, Any]:
        """What a run or an estimate needs from the selections: the whole
        block per member, the core once with each member's delta (spec
        5.1), the pages for citation checks and the token split."""
        return {
            "knowledge": {m: sel.text for m, sel in selections.items()},
            "shared": next((sel.core_text for sel in selections.values() if sel.core_text), ""),
            "delta": {m: "\n\n".join(t for t in (sel.own_text, sel.brief_text) if t) for m, sel in selections.items()},
            "notes": {m: {**sel.sent, **sel.brief_sent} for m, sel in selections.items()},
            "paths": {m: sorted(sel.sent) for m, sel in selections.items()},
            "split": {m: {"core": sel.core_tokens, "own": sel.own_tokens, "brief": sel.brief_tokens} for m, sel in selections.items()},
        }

    def _member_blocks(self, session: Session, board, selected: list[str], budget: int) -> dict[str, Any]:
        if budget <= 0 and not session.extra:
            return self._blocks_from({})  | {"knowledge": {m: "" for m in selected}, "delta": {m: "" for m in selected},
                                             "notes": {m: {} for m in selected}, "paths": {m: [] for m in selected}}
        return self._blocks_from(self._member_selections(session, board, selected, budget))

    @staticmethod
    def _chosen_members(session: Session, wanted: Any) -> list[str]:
        """The members Alex ticked, in board order; all of them when the
        request names none. At least one is required."""
        on_board = list(session.roles.get("members") or [])
        if not on_board:
            on_board = list(session.members)
        if not isinstance(wanted, list) or not wanted:
            return on_board
        chosen = {str(m).strip().lower() for m in wanted}
        selected = [m for m in on_board if m.lower() in chosen]
        if not selected:
            raise ApiError(400, "Choose at least one member to ask.")
        return selected

    def _run_board(self, session: Session) -> None:
        run_id = session.run_id

        def stale() -> bool:
            return session.cancelled.is_set() or session.run_id != run_id

        def on_member(member: str, state: str) -> None:
            if stale():
                return
            with session.lock:
                session.members[member] = state
                if all(s in ("done", "failed") for s in session.members.values()) and session.phase != "synthesising":
                    session.phase = "synthesising"
                    session.mark("synthesising")

        def on_assessment(assessment) -> None:
            if stale():
                return
            with session.lock:
                session.partial[assessment.member] = asdict(assessment)

        inputs = session.inputs
        context = inputs["context"]
        try:
            # Section 3.4: read fresh on every run, never cached - an edit in
            # Obsidian is in force on the next question.
            board = roles_mod.load_board(self.config)
            selected = [m for m in board.profiles if m in session.selected_members] or list(board.profiles)
            profiles = {m: board.profiles[m] for m in selected}
            try:
                member_data = knowledge_mod.kpi_notes(self.config, selected, projects=session.projects)
            except knowledge_mod.KnowledgeUnavailable:
                member_data = {}
            blocks = self._member_blocks(session, board, selected, session.budget or 0)
            member_knowledge, member_notes = blocks["knowledge"], blocks["notes"]
            with session.lock:
                session.member_notes = member_notes
                session.member_paths = blocks["paths"]
                session.knowledge_split = blocks["split"]
                session.roles = roles_mod.summary(board)
                session.roles["kpi_members"] = sorted(member_data)
                session.member_meta = roles_mod.member_meta(board.profiles)
                session.selected_members = selected
                session.members = {member: "pending" for member in selected}
                session.partial = {}
            runner = run_board_combined if session.mode == "combined" else run_board
            result = runner(
                self.config, RecordingProvider(self.provider(), session),
                topic=inputs["topic"], context=context,
                options=tuple(inputs["options"]), constraints=tuple(inputs["constraints"]),
                on_member=on_member, board=board, member_data=member_data, members=selected,
                sent_notes={}, on_assessment=on_assessment,
                member_knowledge=member_knowledge, member_notes=member_notes,
                **({"shared_knowledge": blocks["shared"], "member_delta": blocks["delta"]} if session.mode == "combined" else {}),
            )
        except Exception as exc:
            session.fail(str(exc))
            return
        if stale():
            return          # Alex went back while the board worked: the result is not wanted
        with session.lock:
            session.conversation = BoardConversation(
                result=result, turns=[], roles=dict(board.profiles),   # every member: a follow-up may bring one in
                inputs={"topic": inputs["topic"], "context": context,
                        "options": list(inputs["options"]), "constraints": list(inputs["constraints"])},
                conduct=board.conduct, member_data=member_data,
                project=", ".join(session.projects),
                sent_notes={}, member_knowledge=member_knowledge, member_notes=member_notes,
                shared_knowledge=blocks["shared"], member_delta=blocks["delta"],
            )
            session.result = {
                "topic": result.topic,
                "assessments": [asdict(a) for a in result.assessments],
                "synthesis": result.synthesis,
                "synthesis_data": result.synthesis_data,
                "failed_members": list(result.failed_members),
                "llm_calls": result.llm_calls,
                "sources": result.sources,
                "mode": result.mode,
            }
            session.llm_calls += result.llm_calls
            session.phase = "result"
            session.mark("result")

    def follow_up(self, session: Session, question: str, members: Any = None, mode: Any = None) -> None:
        """A follow-up. ``members`` names the members to ask again (decided
        9 September 2026); empty or missing means the one-call form over
        the original assessments."""
        question = (question or "").strip()
        with session.lock:
            resting = session.phase == "result"
        if resting and session.conversation is None and session.result is not None:
            self._rebuild_conversation(session)          # a topic read back from disk
        with session.lock:
            if session.phase != "result" or session.conversation is None:
                raise ApiError(409, "There is no open board result to ask about.")
            if session.busy:
                raise ApiError(409, "The board is still answering the previous question.")
            if not question:
                raise ApiError(400, "Type a question first.")
            chosen: list[str] = []
            if isinstance(members, list) and members:
                wanted = {str(m).strip().lower() for m in members}
                on_board = list(session.roles.get("members") or session.members)
                chosen = [m for m in on_board if m.lower() in wanted]
            follow_mode = "combined" if str(mode or "").lower() == "combined" else "individual"
            session.cancelled.clear()
            session.forward = []
            session.busy = True
            session.mark("follow-up")
            session.turns.append({"question": question, "answer": "", "pending": True, "members": chosen,
                                  "mode": follow_mode, "data": None, "assessments": [], "failed_members": []})
            index = len(session.turns) - 1
        self._spawn(session, self._follow_up, session, question, chosen, follow_mode, index)

    def _rebuild_conversation(self, session: Session) -> None:
        """The follow-up conversation of a topic read back from disk. The
        result, the turns and the input come from the record; the profiles,
        the conduct note and the KPI notes are read fresh, as a run reads
        them. The knowledge blocks are not kept on disk (only their paths
        are), so a member asked again gets its block selected anew from the
        vault - the same path ``_follow_up`` already takes for a member who
        was not asked the first time."""
        result = session.result or {}
        with session.lock:
            inputs = dict(session.inputs)
            turns = [(t["question"], t.get("answer") or "") for t in session.turns
                     if not t.get("pending") and not t.get("error")]
            projects = list(session.projects)
        try:
            board = roles_mod.load_board(self.config)
            profiles, conduct = dict(board.profiles), board.conduct
        except Exception:   # noqa: BLE001 - a missing roles folder must not block reading the topic
            profiles, conduct = {}, ""
        try:
            member_data = knowledge_mod.kpi_notes(self.config, list(profiles), projects=projects)
        except Exception:   # noqa: BLE001
            member_data = {}
        restored = BoardResult(
            topic=str(result.get("topic") or inputs.get("topic") or ""),
            assessments=[MemberAssessment(**a) for a in result.get("assessments") or [] if isinstance(a, dict)],
            synthesis=str(result.get("synthesis") or ""),
            failed_members=[str(f) for f in result.get("failed_members") or []],
            ai_result=None, llm_calls=int(result.get("llm_calls") or 0),
            synthesis_data=result.get("synthesis_data"), sources=dict(result.get("sources") or {}),
            mode="combined" if result.get("mode") == "combined" else "individual",
        )
        with session.lock:
            if session.conversation is not None:
                return
            session.conversation = BoardConversation(
                result=restored, turns=turns, roles=profiles,
                inputs={"topic": inputs.get("topic", ""), "context": inputs.get("context", ""),
                        "options": list(inputs.get("options") or []),
                        "constraints": list(inputs.get("constraints") or [])},
                conduct=conduct, member_data=member_data, project=", ".join(projects),
            )

    def _follow_up_knowledge(self, session: Session, question: str, chosen: list[str]) -> tuple[str, list[str]]:
        """Pages for the follow-up itself (spec 5.2), not the ones the first
        question chose. Every member asked again gets a block selected for
        the new question; the one-call form gets one block of its own. The
        pages sent earlier stay in ``member_notes``, so a citation from the
        first round still checks out. Returns the block for the one-call
        form and the pages this topic had not seen before.

        Python ranks it, never the model: a follow-up must not cost an extra
        call. A vault that cannot be read is not a reason to refuse the
        question - the follow-up then runs on what it already has."""
        conversation = session.conversation
        budget = session.budget if session.budget is not None else int(
            _get(self.config, "knowledge.token_budget", knowledge_mod.DEFAULT_TOKEN_BUDGET) or 0)
        if budget <= 0 and not session.extra:
            return "", []
        with session.lock:
            topic = str(session.inputs.get("topic") or session.question)
            known = {path for paths in session.member_paths.values() for path in paths}
            projects = list(session.projects)
            extra, exclude = list(session.extra), list(session.exclude)
        query = _ranking_query(question, [topic])
        fresh: set[str] = set()
        block = ""
        try:
            board = roles_mod.load_board(self.config)
        except Exception:   # noqa: BLE001 - no profiles: the ranking runs without a member's own words
            board = None
        try:
            if chosen:
                terms = {m: (role_terms(board.profiles.get(m)) if board is not None else ()) for m in chosen}
                selections = knowledge_mod.gather_for_members(
                    self.config, query, terms, token_budget=max(budget, 1), projects=projects,
                    extra=extra, exclude=exclude)
                for member, sel in selections.items():
                    sent = {**sel.sent, **sel.brief_sent}
                    conversation.member_knowledge[member] = sel.text
                    conversation.member_delta[member] = "\n\n".join(t for t in (sel.own_text, sel.brief_text) if t)
                    conversation.member_notes.setdefault(member, {}).update(sent)
                    if sel.core_text:
                        conversation.shared_knowledge = sel.core_text
                    fresh |= set(sel.sent) - known
                    with session.lock:
                        session.member_paths[member] = sorted(set(session.member_paths.get(member, [])) | set(sel.sent))
                conversation.member_data.update(
                    knowledge_mod.kpi_notes(self.config, chosen, projects=projects))
            else:
                sel = knowledge_mod.gather_for_members(
                    self.config, query, {"": []}, token_budget=max(budget, 1), projects=projects,
                    extra=extra, exclude=exclude)[""]
                block = sel.text
                fresh |= set(sel.sent) - known
        except Exception:   # noqa: BLE001 - a block that cannot be built is not a reason to refuse the question
            return block, sorted(fresh)
        return block, sorted(fresh)

    def _follow_up(self, session: Session, question: str, chosen: list[str], follow_mode: str = "individual",
                   index: int = -1) -> None:
        block, new_pages = self._follow_up_knowledge(session, question, chosen)
        try:
            turn = ask_follow_up_full(self.config, RecordingProvider(self.provider(), session), session.conversation,
                                      question, chosen, follow_mode, block)
        except Exception as exc:
            with session.lock:
                if not _pending_turn(session, index):
                    return
                session.turns[index] = {"question": question, "answer": f"Could not answer: {exc}", "pending": False,
                                        "error": True, "members": chosen, "mode": follow_mode, "data": None,
                                        "assessments": [], "failed_members": []}
                session.busy = False
            return
        with session.lock:
            if not _pending_turn(session, index):
                return
            session.turns[index] = {
                "question": question, "answer": turn.answer, "pending": False, "members": chosen, "mode": follow_mode,
                "new_pages": new_pages,
                "data": turn.data, "assessments": [asdict(a) for a in turn.assessments],
                "failed_members": list(turn.failed_members),
            }
            session.llm_calls += turn.llm_calls
            session.busy = False
            session.mark("follow-up answered")

    def estimate(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        """What a run would cost, before it runs (decided 9 September 2026):
        the exact call count, the prompts built by the run's own builders
        and counted with the same character rule, plus the per-call
        overhead learned from this topic's real calls. Tokens are an
        estimate and are labelled so; calls are exact."""
        with session.lock:
            if session.phase not in ("confirm", "result", "error"):
                raise ApiError(409, "The estimate is for the confirm screen.")
            topic = str(body.get("topic") or session.inputs.get("topic") or session.question)
            context = str(body.get("context") if body.get("context") is not None else session.inputs.get("context", ""))
            options = tuple(str(o) for o in (body.get("options") or session.inputs.get("options") or []))
            constraints = tuple(str(c) for c in (body.get("constraints") or session.inputs.get("constraints") or []))
            mode = "combined" if str(body.get("mode") or "").lower() == "combined" else "individual"
            budget = self._budget(body.get("budget"))
            extra = [str(x) for x in body.get("extra") or [] if str(x).strip()]
            exclude = [str(x) for x in body.get("exclude") or [] if str(x).strip()]
            if "selection" in body:
                session.selection = _selection_value(body.get("selection"))
            selection, pick_state, pick_error = session.selection, session.pick_state, session.pick_error
        selected = self._chosen_members(session, body.get("members"))
        try:
            board = roles_mod.load_board(self.config)
        except roles_mod.RolesUnavailable as exc:
            raise ApiError(409, str(exc))
        roles = {m: board.profiles[m] for m in selected if m in board.profiles}
        try:
            member_data = knowledge_mod.kpi_notes(self.config, list(roles), projects=session.projects)
        except knowledge_mod.KnowledgeUnavailable:
            member_data = {}
        if budget <= 0 and not extra:
            selections = {}
            blocks = self._blocks_from({}) | {"knowledge": {m: "" for m in roles}, "delta": {m: "" for m in roles},
                                              "paths": {m: [] for m in roles}}
        else:
            selections = self._member_selections(session, board, list(roles), budget, extra, exclude)
            blocks = self._blocks_from(selections)
        sizes = prompt_sizes(topic=topic, context=context, options=options, constraints=constraints,
                             roles=roles, conduct=board.conduct, member_data=member_data,
                             project=", ".join(session.projects),
                             member_knowledge=blocks["knowledge"], mode=mode,
                             shared_knowledge=blocks["shared"], member_delta=blocks["delta"])
        if selection == "ai" and pick_state == "running":
            # The pick call is in flight: count it at the size of its prompt
            # (stored when it started). A failed pick is not redone: the run
            # keeps the Python ranking, so no call is counted for it.
            with session.lock:
                sizes.insert(0, ("knowledge pick", session.pick_prompt_chars))
        overhead, learned_from = session.overhead_per_call()
        per_call = [{"label": label, "tokens": knowledge_mod.estimate_tokens_for(chars) + overhead} for label, chars in sizes]
        return {
            "calls": len(sizes),
            "tokens_in": sum(item["tokens"] for item in per_call),
            "per_call": per_call,
            "overhead_per_call": overhead,
            "overhead_learned_from": learned_from,
            "budget": budget,
            "selection": selection,
            "pick_state": pick_state,
            "pick_error": pick_error,
            "picked_by": {m: sel.picked_by for m, sel in selections.items()},
            "reasons": {m: dict(sel.reasons) for m, sel in selections.items()},
            "briefs": {m: [{"path": n.relative, "summary": n.summary, "reason": sel.reasons.get(n.relative, "")} for n in sel.briefs]
                       for m, sel in selections.items()},
            "split": blocks["split"],
            "members": blocks["paths"],
            "sections": {m: [{"id": knowledge_mod.section_id(sec), "path": sec.relative, "heading": sec.heading,
                              "tokens": knowledge_mod.estimate_tokens(sec.body),
                              "forced": knowledge_mod.section_id(sec) in extra or sec.relative in extra,
                              "core": knowledge_mod.section_id(sec) in sel.core_ids,
                              "reason": sel.reasons.get(knowledge_mod.section_id(sec), "")}
                             for sec in sel.sections]
                         for m, sel in selections.items()},
            "forced_tokens": sum(sel.forced_tokens for sel in selections.values()),
            "outline": knowledge_mod.outline(self.config, session.projects) if body.get("outline") else None,
        }

    def back(self, session: Session) -> None:
        """One step back from wherever the topic is, with everything typed
        kept (decided 9 September 2026, extended the same day to every
        stage). While the model works, Back stops the running calls first.
        409 with "your question is kept" when there is nothing behind."""
        with session.lock:
            phase, busy = session.phase, session.busy
        if phase in ("running", "synthesising"):
            session.stop_work()
            with session.lock:
                session.phase = "confirm"
                session.error = None
                session.members = {member: "pending" for member in session.selected_members}
                session.partial = {}
                session.forward = []
                session.mark("stopped")
            return
        if phase == "clarifying":
            session.stop_work()
            with session.lock:
                if session.pick_state == "running":
                    session.pick_state, session.pick_id = "idle", session.pick_id + 1   # the stopped pick never lands
                if session.rounds:
                    _restore_last_round(session)
                    session.forward = []
                    return
            raise ApiError(409, "Nothing to go back to - start over; your question is kept.")
        if phase == "result" and busy:
            session.stop_work()
            with session.lock:
                if session.turns and session.turns[-1].get("pending"):
                    session.turns.pop()
                session.busy = False
                session.mark("stopped")
            return
        if phase == "proposing":
            session.stop_work()
            with session.lock:
                session.phase = "result"
                session.proposal = None
                session.forward = []
            return
        with session.lock:
            if session.phase == "proposal":
                session.phase = "result"
                session.proposal = None
                session.forward = []
                return
            if session.phase == "result":
                session.forward.append("result")
                session.phase = "confirm"
                return
            if session.phase == "error" and session.inputs and session.clarification is not None:
                session.phase = "confirm"
                session.error = None
                session.members = {member: "pending" for member in session.selected_members}
                return
            if session.phase in ("error", "confirm", "questions") and session.rounds and session.clarification is not None:
                if session.phase == "confirm":
                    session.forward.append("confirm")
                _restore_last_round(session)
                return
            raise ApiError(409, "Nothing to go back to - start over; your question is kept.")

    def forward(self, session: Session) -> None:
        """Forward again after Back, as long as nothing new was done since."""
        with session.lock:
            if not session.forward:
                raise ApiError(409, "Nothing to go forward to.")
            target = session.forward.pop()
            if target == "result" and session.result is not None and session.phase == "confirm":
                session.phase = "result"
                return
            if target == "confirm" and session.inputs and session.phase == "questions" and session.clarification is not None:
                session.rounds.append({"questions": list(session.clarification.questions), "answers": list(session.answers)})
                session.phase = "confirm"
                return
            session.forward = []
            raise ApiError(409, "Nothing to go forward to.")

    # -- the shell (spec section 11): the three status checks, the recent work --

    def status(self, projects: Any = None) -> dict[str, Any]:
        """The three checks of the top bar (11.1, decision 7): each
        ``green``, ``amber`` or ``red`` with the detail the hover card shows.
        Cheap, no model call; the page asks on load and every five minutes."""
        return {"vault": self._vault_status(), "ai": self._ai_status(), "project": self._project_status(projects),
                "checked": time.time()}

    def _vault_status(self) -> dict[str, Any]:
        """Green: the folder is reachable and read. Amber: read, but without
        project pages or a roles folder. Red: unset or unreachable."""
        vault_path = _get(self.config, "knowledge.vault_path", "") or ""
        out: dict[str, Any] = {"state": "red", "path": str(vault_path), "vault_name": self.vault_name(), "notes": 0,
                               "projects": 0, "roles_folder": None, "last_read": None, "detail": ""}
        if not vault_path:
            out["detail"] = "No vault folder set. Open Options and choose the Obsidian vault."
            return out
        vault = Path(str(vault_path)).expanduser()
        try:
            notes = knowledge_mod.load_vault(vault, skip_subfolders=knowledge_mod._roles_inside(self.config, vault))
        except knowledge_mod.KnowledgeUnavailable as exc:
            out["detail"] = str(exc)
            return out
        out["notes"] = len(notes)
        out["last_read"] = time.time()
        out["projects"] = len(knowledge_mod.project_names(notes))
        try:
            table = knowledge_mod.contents(self.config)
            out["contents_tokens"] = table["tokens"]
            out["contents_limit"] = int(_get(self.config, "knowledge.contents_limit_tokens", knowledge_mod.CONTENTS_LIMIT_TOKENS)
                                        or knowledge_mod.CONTENTS_LIMIT_TOKENS)
        except knowledge_mod.KnowledgeUnavailable:
            pass
        folder, _origin = knowledge_mod.resolve_roles_folder(self.config)
        out["roles_folder"] = str(folder) if folder is not None else None
        missing = []
        if not out["projects"]:
            missing.append("no project pages (kind: project)")
        if folder is None or not folder.is_dir():
            missing.append("no roles folder")
        out["state"] = "amber" if missing else "green"
        out["detail"] = f"{len(notes)} notes read." + (f" But: {'; '.join(missing)}." if missing else "")
        return out

    def _ai_status(self) -> dict[str, Any]:
        """Green: OpenCode found, the model string set, the gateway config
        with a key found (or a private setup, where OpenCode holds the login).
        Amber: something is set but unverified. Red: missing. A confirmed test
        call (``ai_check``) settles it either way while the settings it was
        made with still hold."""
        model = str(_get(self.config, "provider.models.board", "") or "")
        config_file = str(_get(self.config, "provider.opencode.config_file", "") or "")
        profile = str(_get(self.config, "setup.profile", "") or "")
        binary = shutil.which("opencode") if self._provider_override is None else "(provider given in code)"
        out: dict[str, Any] = {"state": "red", "model": model, "config_file": config_file, "opencode": binary,
                               "profile": profile, "detail": "", "last_call": self.ai_check,
                               "streams": ai_provider.streams(self.config)}
        problems: list[str] = []
        unverified: list[str] = []
        if not model:
            problems.append("no model string set")
        if not binary:
            problems.append("opencode not found on PATH")
        if config_file:
            problem = opencode_config_problem(config_file)
            if problem:
                problems.append(f"the OpenCode configuration file {problem}")
            elif not _config_file_has_key(config_file):
                unverified.append("no key visible in the OpenCode configuration file (it may come from OpenCode's own store)")
        elif profile == "private":
            unverified.append("OpenCode holds the login itself; nothing here can verify it")
        else:
            unverified.append("no OpenCode configuration file set; OpenCode's own lookup decides the gateway")
        if problems:
            out["state"] = "red"
            out["detail"] = _sentence("; ".join(problems)) + ". Open Options."
        elif unverified:
            out["state"] = "amber"
            out["detail"] = _sentence("; ".join(unverified)) + ". Click the icon to run one test call."
        else:
            out["state"] = "green"
            out["detail"] = "OpenCode found, the model set, the gateway configuration with a key found."
        check = self.ai_check
        if check and check.get("model") == model and check.get("config_file") == config_file:
            # The last real call was made with these settings: it outranks the cheap look.
            out["state"] = "green" if check.get("ok") else "red"
            out["detail"] = (f"Test call answered in {check.get('seconds', 0):.1f} s." if check.get("ok")
                             else f"Test call failed: {check.get('error')}")
        return out

    def check_ai(self) -> dict[str, Any]:
        """The confirmed test call (11.1, decision 7): one tiny prompt through
        the provider, its outcome and time kept for the hover and the status
        page. Never run on a timer."""
        model = str(_get(self.config, "provider.models.board", "") or "")
        config_file = str(_get(self.config, "provider.opencode.config_file", "") or "")
        started = time.time()
        record: dict[str, Any] = {"at": started, "model": model, "config_file": config_file, "ok": False,
                                  "seconds": 0.0, "answer": "", "error": None}
        try:
            result = self.provider().complete(TASK_BOARD, STATUS_CHECK_PROMPT)
            record["ok"] = True
            record["answer"] = (result.text or "").strip()[:80]
            record["provider_model"] = f"{result.provider}/{result.model}"
            record["input_tokens"], record["output_tokens"] = result.input_tokens, result.output_tokens
        except Exception as exc:   # noqa: BLE001 - whatever failed, the icon must say so
            record["error"] = str(exc)[:400]
            result = None
        record["seconds"] = round(time.time() - started, 2)
        self.ai_check = record
        log_run("status-check", audit_folder=_get(self.config, "runtime.audit_folder"),
                pc_name=_get(self.config, "storage.pc_name", ""), duration_seconds=record["seconds"],
                counts={"ok": int(record["ok"])}, provider=result.provider if result else None,
                model=result.model if result else None, tokens=result.total_tokens if result else None)
        return self._ai_status()

    def _project_status(self, projects: Any = None) -> dict[str, Any]:
        """Green: a project is chosen (with its gate baseline from the project
        page). Amber: all projects. Red: the vault has no project pages."""
        chosen = knowledge_mod._project_list(projects) if projects is not None else knowledge_mod.active_projects(self.config)
        known = knowledge_mod.list_projects(self.config)
        out: dict[str, Any] = {"state": "red", "projects": chosen, "known": known, "detail": "", "pages": []}
        if not known:
            out["detail"] = "The vault has no project pages (kind: project)."
            return out
        if not chosen:
            out["state"] = "amber"
            out["detail"] = "All projects: every page of the vault is used. Choose a project on the home page."
            return out
        out["state"] = "green"
        out["detail"] = f"Project: {', '.join(chosen)}."
        vault = Path(str(_get(self.config, "knowledge.vault_path", ""))).expanduser()
        try:
            notes = knowledge_mod.load_vault(vault, skip_subfolders=knowledge_mod._roles_inside(self.config, vault))
        except knowledge_mod.KnowledgeUnavailable:
            return out
        for note in knowledge_mod._pinned(notes, chosen):
            out["pages"].append({"path": note.relative, "title": note.title, "summary": note.summary,
                                 "gates": _gate_lines(note.body)})
        unknown = [p for p in chosen if p.lower() not in {k.lower() for k in known}]
        if unknown:
            out["state"] = "amber"
            out["detail"] += f" No project page for: {', '.join(unknown)}."
        return out

    def history(self, state: str = "open") -> list[dict[str, Any]]:
        """The recent work of every agent, newest first (11.1, decision 5):
        the threads on disk and the board topics this server holds in memory
        (their files come with step 3). ``state``: open, closed or all."""
        rows: list[dict[str, Any]] = []
        for row in self.thread_store().list():
            rows.append({"kind": ask_mod.KIND, "id": row["id"], "title": row["title"], "status": row["status"],
                         "updated": row["updated"], "created": row["created"], "projects": row["projects"],
                         "count": row["questions"], "unit": "question"})
        with self.lock:
            live = dict(self.sessions)
        for data in self.topic_store().records(BOARD_KIND):
            session = live.pop(str(data.get("id")), None)
            rows.append(self._topic_row(session) if session is not None else {
                "kind": BOARD_KIND, "id": str(data.get("id")), "title": str(data.get("title") or "") or _record_title(data),
                "status": "closed" if data.get("status") == "closed" else "open",
                "updated": float(data.get("updated") or 0), "created": float(data.get("created") or 0),
                "projects": [str(p) for p in data.get("projects") or []],
                "count": int(data.get("llm_calls") or 0), "unit": "call", "phase": str(data.get("phase") or "")})
        rows.extend(self._topic_row(session) for session in live.values())   # never written yet
        wanted = {"open": ("open",), "closed": ("closed",)}.get(state, ("open", "closed"))
        rows = [r for r in rows if r["status"] in wanted]
        rows.sort(key=lambda r: r["updated"], reverse=True)
        return rows

    @staticmethod
    def _topic_row(session: Session) -> dict[str, Any]:
        with session.lock:
            last = session.marks[-1]["at"] if session.marks else session.started
            return {"kind": BOARD_KIND, "id": session.id, "title": session.title, "status": session.status,
                    "updated": last, "created": session.started, "projects": list(session.projects),
                    "count": session.llm_calls, "unit": "call", "phase": session.phase}

    def delete_record(self, record_id: str) -> None:
        """Delete one piece of work from the archive (11.1, decision 10):
        the file and whatever of it this server still holds. A note it wrote
        into the vault stays where it is."""
        with self.lock:
            session = self.sessions.pop(record_id, None)
            ask_session = self.asks.pop(record_id, None)
        for live in (session, ask_session):
            if live is not None and live.busy:
                live.stop_work()
        try:
            found = self.topic_store().delete(record_id)
        except ValueError:
            raise ApiError(404, "unknown record")
        if not found and session is None and ask_session is None:
            raise ApiError(404, "unknown record")

    # -- Ask the vault (spec section 10) --------------------------------------

    def vault_name(self) -> str:
        """The Obsidian vault name the page links to (``obsidian://open``):
        ``knowledge.vault_name``, else the vault folder's name."""
        configured = _get(self.config, "knowledge.vault_name", "")
        if configured:
            return str(configured)
        vault_path = _get(self.config, "knowledge.vault_path", "")
        return Path(str(vault_path)).expanduser().name if vault_path else ""

    def history_folder(self) -> Path:
        """One folder for the work of every agent (11.1, decision 9)."""
        return history_mod.history_folder(self.config, self.config_path)

    def thread_store(self) -> "ask_mod.ThreadStore":
        return ask_mod.ThreadStore(self.history_folder())

    def topic_store(self) -> history_mod.HistoryStore:
        return history_mod.HistoryStore(self.history_folder())

    def save_session(self, session: Session) -> None:
        """The topic on disk, after every step that changed it. Two steps can
        finish at the same moment - the answer to a request and the background
        job it started - so the older of two snapshots never overwrites the
        newer. A topic that cannot be written is said so on the page, but
        never takes a running board down."""
        stamp = time.monotonic()
        record = session.to_record()
        with self.lock:
            if session.saved_stamp > stamp:
                return                        # a newer snapshot of this topic is already on disk
            session.saved_stamp = stamp
        try:
            if session.discarded:
                self.topic_store().delete(session.id)
                return
            self.topic_store().save(session.id, record)
        except OSError as exc:
            with session.lock:
                if not session.busy and not session.error:
                    session.error = f"The topic could not be saved: {exc}"

    def list_threads(self) -> list[dict[str, Any]]:
        rows = self.thread_store().list()
        with self.lock:
            live = {tid: a for tid, a in self.asks.items()}
        for row in rows:
            session = live.get(row["id"])
            row["busy"] = bool(session and session.busy)
        return rows

    def new_thread(self, projects: Any = None, budget: Any = None) -> AskSession:
        projects_list = knowledge_mod._project_list(projects) if projects is not None else knowledge_mod.active_projects(self.config)
        default_budget = int(_get(self.config, "ask.token_budget", ask_mod.DEFAULT_TOKEN_BUDGET) or ask_mod.DEFAULT_TOKEN_BUDGET)
        try:
            chosen_budget = int(budget) if budget not in (None, "") else default_budget
        except (TypeError, ValueError):
            chosen_budget = default_budget
        store = self.thread_store()
        thread = store.new(projects_list, max(0, min(chosen_budget, 40000)))
        try:
            store.save(thread)
        except OSError as exc:
            raise ApiError(500, f"Cannot write the thread file: {exc}")
        session = AskSession(thread)
        with self.lock:
            self.asks[thread.id] = session
        return session

    def get_thread(self, thread_id: str) -> AskSession:
        with self.lock:
            session = self.asks.get(thread_id)
        if session is not None:
            return session
        try:
            thread = self.thread_store().load(thread_id)
        except ValueError:
            raise ApiError(404, "unknown thread")
        if thread is None:
            raise ApiError(404, "unknown thread")
        session = AskSession(thread)
        with self.lock:
            session = self.asks.setdefault(thread_id, session)
        return session

    def delete_thread(self, thread_id: str) -> None:
        with self.lock:
            session = self.asks.pop(thread_id, None)
        if session is not None and session.busy:
            session.stop_work()
        try:
            found = self.thread_store().delete(thread_id)
        except ValueError:
            raise ApiError(404, "unknown thread")
        if not found and session is None:
            raise ApiError(404, "unknown thread")

    def _save_thread(self, session: AskSession) -> None:
        try:
            self.thread_store().save(session.thread)
        except OSError as exc:
            session.error = f"The thread could not be saved: {exc}"

    def _ceiling(self) -> int:
        return int(_get(self.config, "ask.max_read_tokens") or knowledge_mod.MAX_READ_TOKENS)

    def _max_reads(self) -> int:
        return max(1, int(_get(self.config, "ask.max_reads") or ask_mod.MAX_READS))

    def _table(self, session: AskSession, question: str) -> dict[str, Any]:
        """The table of contents of this question, made once in the choosing
        call and reused by the answer and the check (spec 5.5)."""
        with session.lock:
            table = session.table
        if table is None:
            table = knowledge_mod.contents(self.config, session.thread.projects, question=question)
            with session.lock:
                session.table = table
        return table

    def _ask_pages(self, session: AskSession, question: str, extra: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
        """The pages a read starts from, in order (spec 5.6, decision 3):
        Alex's own ticks, the model's list as whole pages, the kept pages,
        then the rest of the vault in its own order; the ceiling cuts from
        the end. On the whole-vault path there is no list and the order is
        the vault's. When a ranking call failed, the word ranking's pages
        stand in for it (5.3, decision 5). Under the lock."""
        thread = session.thread
        chosen = [knowledge_mod.page_of(k) for k in ((session.picks or {}).get("full") or [])]
        reasons = {knowledge_mod.page_of(k): why for k, why in ((session.picks or {}).get("reasons") or {}).items()}
        if session.overflow and session.picks is None and _get(self.config, "knowledge.vault_path"):
            query = _ranking_query(question, [t["question"] for t in thread.turns[-2:]])
            ranked = knowledge_mod.gather_for_members(self.config, query, {"": []}, token_budget=self._ceiling() // 4,
                                                      projects=thread.projects, include_roles=True)[""]
            chosen = [note.relative for note in ranked.notes]
        rest = [page["path"] for page in (session.table or {}).get("pages") or []]
        order: list[str] = []
        for path in ([knowledge_mod.page_of(x) for x in (extra if extra is not None else thread.extra)]
                     + chosen + list(session.kept) + rest):
            if path not in order:
                order.append(path)
        return order, reasons

    def ask_estimate(self, session: AskSession, body: dict[str, Any]) -> dict[str, Any]:
        """The first read as it would go (spec 5.5): the pages, whole, with
        the reason each; what the ceiling cuts; the size of the call."""
        thread = session.thread
        question = str(body.get("question") or "").strip() or "(the next question)"
        extra = [str(x) for x in body.get("extra") or []] if "extra" in body else None
        exclude = [str(x) for x in body.get("exclude") or []] if "exclude" in body else list(thread.exclude)
        if not _get(self.config, "knowledge.vault_path"):
            sel, order, reasons = None, [], {}
            text, kpi_text, contents_text = "", "", ""
        else:
            try:
                with session.lock:
                    order, reasons = self._ask_pages(session, question, extra)
                    picked_by = "model" if session.picks else "python"
                table = self._table(session, question)
                sel = knowledge_mod.gather_whole(self.config, question, order, ceiling=self._ceiling(),
                                                 projects=thread.projects, exclude=exclude)
                kpi_text = ask_mod.project_kpi_text(self.config, thread.projects)
                partial = bool(sel.left)        # 5.6, decision 5: the table of contents only when a page was left unread
                contents_text = "\n".join(knowledge_mod.contents_lines(table["pages"])) if partial else ""
            except knowledge_mod.KnowledgeUnavailable as exc:
                raise ApiError(409, str(exc))
            text = sel.text
        prompt = ask_mod.ask_prompt(question, text, kpi_text, thread.history(), ", ".join(thread.projects),
                                    read_before=self._read_before(thread) if contents_text else None, contents_text=contents_text)
        overhead, learned_from = session.overhead_per_call()
        tokens = knowledge_mod.estimate_tokens(prompt) + overhead
        with session.lock:
            chosen = {knowledge_mod.page_of(k) for k in ((session.picks or {}).get("full") or [])}
            kept = list(session.kept) if session.overflow else []
            sizes = dict(session.page_tokens)
            overflow = session.overflow
        forced = {knowledge_mod.page_of(x) for x in (extra if extra is not None else thread.extra)}
        pages = []
        if sel:
            for note in sel.notes:
                path = note.relative
                pages.append({"path": path, "tokens": knowledge_mod.estimate_tokens(sel.sent.get(path, "")),
                              "core": path not in order or all(knowledge_mod.section_id(sec) in sel.core_ids for sec in sel.sections if sec.relative == path),
                              "kept": path in kept and path not in chosen, "forced": path in forced,
                              "reason": reasons.get(path, "")})
        return {
            "calls": 2 if overflow else 1, "tokens_in": tokens,
            "per_call": [{"label": "ask the vault", "tokens": tokens}] + ([{"label": "answer check", "tokens": 0}] if overflow else []),
            "overhead_per_call": overhead, "overhead_learned_from": learned_from,
            "ceiling": self._ceiling(), "max_reads": self._max_reads(), "whole_vault": not overflow,
            "knowledge_tokens": sel.tokens if sel else 0, "kpi_tokens": knowledge_mod.estimate_tokens(kpi_text) if kpi_text else 0,
            "contents_tokens": knowledge_mod.estimate_tokens(contents_text) if contents_text else 0,
            "pages": pages,
            "beyond": [{"path": path, "tokens": sizes.get(path, 0), "reason": reasons.get(path, ""), "kept": path in kept and path not in chosen}
                       for path in (sel.left if sel else [])],
            "picked_by": ("vault" if not overflow else picked_by) if sel else "python",
            "kept": kept,
        }

    @staticmethod
    def _read_before(thread: "ask_mod.Thread") -> list[str]:
        """The pages the thread has read in full so far, in the order they
        were first read (spec 5.4, decisions 1 and 2)."""
        seen: list[str] = []
        for turn in thread.turns:
            for path in turn.get("paths") or []:
                if path not in seen:
                    seen.append(path)
        return seen

    def ask_question(self, session: AskSession, body: dict[str, Any]) -> None:
        question = str(body.get("question") or "").strip()
        if not question:
            raise ApiError(400, "Type a question first.")
        with session.lock:
            if session.thread.status == "closed":
                raise ApiError(409, "This thread is closed. Start a new one.")
            if session.busy:
                raise ApiError(409, "Wait for the answer before asking the next question.")
            if session.phase == "picks":
                raise ApiError(409, "A choice is waiting on the picks screen: read it or cancel it first.")
            if body.get("budget") not in (None, ""):
                try:
                    session.thread.budget = max(0, min(int(body["budget"]), 40000))
                except (TypeError, ValueError):
                    pass
            if "extra" in body:
                session.thread.extra = [str(x) for x in body.get("extra") or []]
            if "exclude" in body:
                session.thread.exclude = [str(x) for x in body.get("exclude") or []]
            session.cancelled.clear()
            session.clear_question()
            session.busy = True
            session.error = None
            session.pending_question = question
            session.pick_dropped = 0
            session.table = None
            if not _get(self.config, "knowledge.vault_path"):
                session.phase = "asking"                   # no vault: nothing to choose from
                session.mark("asked")
                self._spawn(session, self._ask, session, question)
                return
        # Spec 5.6: the whole vault is read when it fits the ceiling; the packer itself says whether it does.
        try:
            table = knowledge_mod.contents(self.config, session.thread.projects, question=question)
            fit = knowledge_mod.gather_whole(self.config, question, [page["path"] for page in table["pages"]],
                                             ceiling=self._ceiling(), projects=session.thread.projects, exclude=session.thread.exclude)
        except knowledge_mod.KnowledgeUnavailable as exc:
            with session.lock:
                session.clear_question()
            raise ApiError(409, str(exc))
        sizes = {page["path"]: page["tokens"] for page in table["pages"]}
        for page in table["pages"]:
            sizes.update({sec["id"]: sec["tokens"] for sec in page.get("sections") or []})
        pages = {page["path"] for page in table["pages"]}
        with session.lock:
            session.table = table
            session.page_tokens = sizes
            session.page_count = len(table["pages"])
            session.contents_tokens, session.contents_trimmed = table["tokens"], table["trimmed"]
            session.kept = [path for path in self._read_before(session.thread) if path in pages]
            session.overflow = bool(fit.left) or table["trimmed"]
            if not session.overflow:
                session.busy = False                       # every page fits: nothing to choose, straight to the picks screen
                session.phase = "picks"
                session.mark("picks")
                return
            session.phase = "choosing"                     # spec 5.3: the model ranks first
            session.mark("choosing")
        self._spawn(session, self._choose, session, question)

    def _choose(self, session: AskSession, question: str) -> None:
        """The choosing call (5.3): the table of contents in, the picks out,
        then the thread waits for Alex on the picks screen. A choice that
        does not come back usable leaves the word ranking in force and the
        screen says so; a call that fails is an error like any other."""
        thread = session.thread
        try:
            table = self._table(session, question)
            with session.lock:
                read_before = list(session.kept)     # spec 5.4, decision 2: what the thread has read stays with it
            result = picker.choose(RecordingProvider(self.provider(), session), question,
                                   {"": "the one agent answering the question from the vault"},
                                   table["pages"], trimmed=table["trimmed"], history=thread.history(),
                                   read_before=read_before, overflow=True)
        except Exception as exc:
            with session.lock:
                session.clear_question()
                if not session.cancelled.is_set():
                    session.error = f"The choosing call failed: {exc}"
            return
        with session.lock:
            if session.cancelled.is_set():
                session.clear_question()
                return
            session.pick_dropped = result.dropped
            if result.ok:
                session.picks = result.picks.get("")
                session.pick_error = None
                drop = set(session.picks.get("drop") or [])
                session.kept = [path for path in read_before if path not in drop]
                session.dropped_kept = {path: (session.picks.get("drop_reasons") or {}).get(path, "")
                                        for path in read_before if path in drop}
            else:
                session.picks, session.pick_error = None, result.error
                session.kept, session.dropped_kept = list(read_before), {}      # a failed choice keeps everything
            session.busy = False
            session.phase = "picks"
            session.mark("picks")

    def ask_read(self, session: AskSession, body: dict[str, Any]) -> None:
        """Alex confirmed the picks screen: read what is ticked and answer."""
        with session.lock:
            if session.phase != "picks" or not session.pending_question:
                raise ApiError(409, "There is no choice waiting to be read.")
            if body.get("budget") not in (None, ""):
                try:
                    session.thread.budget = max(0, min(int(body["budget"]), 40000))
                except (TypeError, ValueError):
                    pass
            if "extra" in body:
                session.thread.extra = [str(x) for x in body.get("extra") or []]
            if "exclude" in body:
                session.thread.exclude = [str(x) for x in body.get("exclude") or []]
            session.cancelled.clear()
            session.busy = True
            session.error = None
            session.phase = "asking"
            question = session.pending_question
            session.mark("asked")
        self._spawn(session, self._ask, session, question)

    def _ask(self, session: AskSession, question: str) -> None:
        """The answer, and the loop that secures it (spec 5.5, decision 4):
        read whole pages, answer, check the answer against the table of
        contents, read what the check and the answer asked for, answer
        again - until the check names nothing new, the reads reach the cap,
        or the ceiling has no room left."""
        thread = session.thread
        started = time.monotonic()
        provider = RecordingProvider(self.provider(), session)
        vault = bool(_get(self.config, "knowledge.vault_path"))
        read_before = self._read_before(thread)
        rounds: list[dict[str, Any]] = []
        sent: dict[str, str] = {}
        left: list[str] = []
        still_wanted: list[str] = []
        cap_hit = False
        answer: ask_mod.Answer | None = None
        check_note = ""
        wanted: list[str] = []
        try:
            kpi_text = ask_mod.project_kpi_text(self.config, thread.projects) if vault else ""
            table = self._table(session, question) if vault else {"pages": [], "trimmed": False}
            contents_lines = "\n".join(knowledge_mod.contents_lines(table["pages"])) if vault else ""
            with session.lock:
                order, _reasons = self._ask_pages(session, question) if vault else ([], {})
            max_reads = self._max_reads()
            for round_no in range(1, max_reads + 1):
                if session.cancelled.is_set():
                    break
                added: list[str] = []
                if vault:
                    with session.lock:
                        session.round = round_no
                        session.read_counts.append(0)
                        session.phase = "asking"
                    sel = knowledge_mod.gather_whole(self.config, question, order, ceiling=self._ceiling(),
                                                     projects=thread.projects, exclude=thread.exclude)
                    added = [path for path in sel.sent if path not in sent]
                    if round_no > 1 and not added:
                        still_wanted = list(order[:len(wanted)])   # a page larger than the ceiling itself: the swap read nothing new
                        with session.lock:
                            session.round -= 1
                            session.read_counts.pop()
                        break
                    sent, left, text = dict(sel.sent), list(sel.left), sel.text
                    with session.lock:
                        session.read_counts[-1] = len(added) if round_no > 1 else len(sel.sent)
                else:
                    text = ""
                    with session.lock:
                        session.round = round_no
                        session.read_counts.append(0)
                        session.phase = "asking"
                partial = bool(left)          # 5.6, decision 5: the table of contents only when a page was left unread
                session.set_live("")
                answer = ask_mod.ask(provider, question, text, kpi_text, thread.history(), sent, None,
                                     ", ".join(thread.projects), read_before=read_before if partial else None,
                                     contents_text=contents_lines if partial else "",
                                     earlier=rounds[-1]["_answer"] if rounds else None, check_note=check_note,
                                     round_no=round_no,
                                     on_text=lambda text: session.set_live(ask_mod.live_answer(text)))
                record: dict[str, Any] = {"n": round_no, "read": added, "answer": answer.answer, "gaps": answer.gaps,
                                          "sources": answer.sources, "_answer": answer}
                if not vault or not partial or session.cancelled.is_set():
                    rounds.append(record)         # 5.6, decision 4: nothing left unread, nothing to check
                    break
                with session.lock:
                    session.phase = "checking"
                    session.mark("checking")
                result = picker.check(provider, question, answer.answer, sorted(sent), table["pages"],
                                      history=thread.history(), trimmed=table["trimmed"])
                wanted_names = list(answer.missing) + list(result.paths)
                found, dropped = knowledge_mod.resolve_page_names(self.config, wanted_names, thread.projects)
                wanted = [path for path in found if path not in sent and path not in thread.exclude]   # 5.6: what the ceiling left is what a swap is for
                record["check"] = {"note": result.note, "wanted": wanted, "reasons": dict(result.reasons),
                                   "dropped": result.dropped + dropped, "error": result.error, "complete": result.complete}
                rounds.append(record)
                check_note = result.note
                if result.error or not wanted:
                    break
                if round_no == max_reads:
                    cap_hit, still_wanted = True, wanted
                    break
                order = wanted + [path for path in order if path not in wanted]    # 5.6, decision 4: the swap; the ceiling drops the lowest
                with session.lock:
                    session.phase = "reading_more"
                    session.mark("reading more")
        except Exception as exc:
            with session.lock:
                cancelled = session.cancelled.is_set()
                session.clear_question()
                if not cancelled:
                    session.error = f"The call failed: {exc}"
            return
        if answer is None:
            with session.lock:
                session.clear_question()
            return
        paths = sorted(sent)
        for record in rounds:
            record.pop("_answer", None)
        with session.lock:
            if session.cancelled.is_set():
                session.clear_question()
                return
            # Which of the pages sent for this question the thread had not
            # seen before (spec 5.2): every question selects its own.
            seen = {path for turn in thread.turns for path in turn.get("paths") or []}
            chosen = {knowledge_mod.page_of(k) for k in ((session.picks or {}).get("full") or [])}
            thread.turns.append({
                "question": question, "answer": answer.answer, "sources": answer.sources, "gaps": answer.gaps,
                "dropped": answer.dropped, "decision_question": answer.decision_question,
                "parse_error": answer.parse_error, "paths": paths, "new_pages": sorted(set(paths) - seen),
                "kept": [path for path in session.kept if path in sent and path not in chosen],   # kept, not chosen anew
                "at": time.time(),
                "chosen_by": "vault" if not session.overflow else ("model" if session.picks else "python"),
                "pick_reasons": {knowledge_mod.page_of(k): why for k, why in ((session.picks or {}).get("reasons") or {}).items()},
                "rounds": rounds, "left": left, "cap_hit": cap_hit, "still_wanted": still_wanted,
            })
            session.clear_question()
            session.mark("answered")
            self._save_thread(session)
        ai_result = answer.ai_result
        log_run("ask", audit_folder=_get(self.config, "runtime.audit_folder"), pc_name=_get(self.config, "storage.pc_name", ""),
                duration_seconds=time.monotonic() - started,
                counts={"questions": len(thread.turns), "sources": len(answer.sources), "gaps": len(answer.gaps),
                        "dropped_sources": answer.dropped, "rounds": len(rounds), "pages": len(paths)},
                provider=ai_result.provider if ai_result else None, model=ai_result.model if ai_result else None,
                tokens=ai_result.total_tokens if ai_result else None)

    def ask_stop(self, session: AskSession) -> None:
        """Stop the running call, or leave the picks screen; the question
        stays typed, nothing is recorded."""
        session.stop_work()
        with session.lock:
            session.clear_question()
            session.mark("stopped")

    def ask_close(self, session: AskSession, remember: bool) -> None:
        """Close the thread (the page asked for confirmation first); with
        ``remember`` the board's memory proposal is started for it."""
        with session.lock:
            if session.busy:
                raise ApiError(409, "Wait for the answer before closing.")
            if not session.thread.turns:
                raise ApiError(409, "Nothing was asked yet - delete the thread instead.")
            session.thread.status = "closed"
            session.mark("closed")
            self._save_thread(session)
            if not remember:
                return
            if not _get(self.config, "knowledge.vault_path"):
                raise ApiError(400, "No knowledge source is configured - set the vault folder in Options first.")
            session.cancelled.clear()
            session.phase = "proposing"
            session.busy = True
        self._spawn(session, self._ask_propose, session)

    def _ask_propose(self, session: AskSession) -> None:
        thread = session.thread
        turns = [(t["question"], t["answer"]) for t in thread.turns]
        try:
            proposal = memory_writer.propose(
                RecordingProvider(self.provider(), session), Path(str(_get(self.config, "knowledge.vault_path"))).expanduser(),
                topic=thread.title, inputs={"context": "A thread of Ask the vault: questions answered from the vault."},
                synthesis=turns[-1][1] if turns else "", turns=turns[:-1])
        except Exception as exc:
            with session.lock:
                session.phase, session.busy = "idle", False
                session.error = f"Could not propose a memory entry: {exc}"
            return
        with session.lock:
            session.proposal = proposal
            session.phase, session.busy = "proposal", False
            session.error = None

    def ask_write_memory(self, session: AskSession, edited: dict[str, Any]) -> Path:
        with session.lock:
            if session.phase != "proposal" or session.proposal is None:
                raise ApiError(409, "There is no memory proposal to write.")
            proposal = session.proposal
            proposal.path = str(edited.get("path") or proposal.path)
            proposal.title = str(edited.get("title") or proposal.title).strip() or session.thread.title
            if "tags" in edited:
                proposal.tags = [str(x).strip().lstrip("#") for x in edited["tags"] if str(x).strip()]
            if "body" in edited:
                proposal.body = str(edited["body"])
            if not proposal.body.strip():
                raise ApiError(400, "The note body must not be empty.")
            try:
                target = memory_writer.write_note(Path(str(_get(self.config, "knowledge.vault_path"))).expanduser(), proposal)
            except OSError as exc:
                raise ApiError(500, f"Could not write the note: {exc}")
            session.thread.written_path = str(target)
            session.phase = "written"
            self._save_thread(session)
            return target

    def ask_discard_memory(self, session: AskSession) -> None:
        with session.lock:
            session.proposal = None
            session.phase = "idle"

    def abandon(self, session: Session) -> None:
        """Leave the topic: stop any running call and drop it. Start over is
        not closing (11.1, decision 10): the topic had no result to keep, so
        it goes, rather than filling the archive with abandoned questions."""
        session.stop_work()
        with session.lock:
            session.busy = False
            session.phase = "closed"
            session.discarded = True
            session.mark("abandoned")
        with self.lock:
            self.sessions.pop(session.id, None)

    def close(self, session: Session, remember: bool) -> None:
        """Close the topic; with ``remember`` the memory proposal is started."""
        with session.lock:
            if session.phase != "result":
                raise ApiError(409, "There is no open topic to close.")
            if session.busy:
                raise ApiError(409, "Wait for the board's answer before closing.")
            if not remember:
                session.phase = "closed"
                return
            if not session.knowledge.get("vault_path"):
                raise ApiError(400, "No knowledge source is configured - set the vault folder in Options first.")
            session.cancelled.clear()
            session.phase = "proposing"
            session.busy = True
        self._spawn(session, self._propose, session)

    def _propose(self, session: Session) -> None:
        try:
            proposal = memory_writer.propose(
                RecordingProvider(self.provider(), session), Path(session.knowledge["vault_path"]),
                topic=session.inputs["topic"], inputs=session.inputs,
                synthesis=(session.result or {}).get("synthesis_data") or (session.result or {}).get("synthesis", ""),
                turns=[(t["question"], t["answer"]) for t in session.turns if not t.get("pending")],
            )
        except Exception as exc:
            with session.lock:
                session.phase = "result"
                session.busy = False
                session.error = f"Could not propose a memory entry: {exc}"
            return
        with session.lock:
            session.proposal = proposal
            session.llm_calls += 1
            session.phase = "proposal"
            session.busy = False
            session.error = None

    def write_memory(self, session: Session, edited: dict[str, Any]) -> Path:
        """Write the confirmed, possibly edited, proposal into the vault."""
        with session.lock:
            if session.phase != "proposal" or session.proposal is None:
                raise ApiError(409, "There is no memory proposal to write.")
            vault = Path(session.knowledge["vault_path"])
            proposal = session.proposal
            proposal.path = str(edited.get("path") or proposal.path)
            proposal.title = str(edited.get("title") or proposal.title).strip() or session.inputs["topic"]
            if "tags" in edited:
                proposal.tags = [str(t).strip().lstrip("#") for t in edited["tags"] if str(t).strip()]
            if "body" in edited:
                proposal.body = str(edited["body"])
            if not proposal.body.strip():
                raise ApiError(400, "The note body must not be empty.")
            try:
                target = memory_writer.write_note(vault, proposal)
            except OSError as exc:
                raise ApiError(500, f"Could not write the note: {exc}")
            session.written_path = str(target)
            session.phase = "written"
            return target

    def discard_memory(self, session: Session) -> None:
        """Close the topic without writing the proposal."""
        with session.lock:
            if session.phase != "proposal":
                raise ApiError(409, "There is no memory proposal to discard.")
            session.phase = "closed"


def _gate_lines(body: str) -> list[str]:
    """The gate baseline of a project page: the rows of its gate table, and
    only those, when the page holds one; the plain lines that name a gate or
    a phase when it does not (10 September 2026 - a page that writes its
    baseline as prose still says something, a page with a table must not
    have prose about MG3 mixed into it)."""
    lines = [line for line in body.splitlines() if _GATE_LINE.search(line)]
    rows = [line for line in lines if line.lstrip().startswith("|") and not _TABLE_RULE.match(line.strip())]
    if not rows:
        rows = [line for line in lines if not line.lstrip().startswith(("#", "|", "phases:", "---"))]
    gates: list[str] = []
    for line in rows[:_GATE_LINES]:
        cleaned = re.sub(r"\s*\|\s*", " · ", line.strip().strip("|")).strip(" ·-*")
        if cleaned and cleaned not in gates:
            gates.append(cleaned[:120])
    return gates


def _ranking_query(question: str, context: list[str]) -> str:
    """What a selection is ranked by (spec 5.2, 10 September 2026): the
    question being answered now, and the earlier question or the topic only
    when this one carries no word of its own ("and who approves that?").

    The ranking counts how often a query word appears in a section, so a
    long-standing subject in the query outweighs the new question every
    time: carrying the earlier questions along was why a follow-up kept
    getting the pages of the first question back."""
    question = (question or "").strip()
    if knowledge_mod.query_terms(question):
        return question
    return "\n".join([question] + [c for c in context if c and c.strip()])


def _record_title(data: dict[str, Any]) -> str:
    """A topic's title from its record: the clarified topic, else the question."""
    inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    topic = str(inputs.get("topic") or "").strip()
    return (topic or str(data.get("question") or "").strip())[:120] or "New topic"


def _sentence(text: str) -> str:
    """The first letter upper-cased, the rest as written (``capitalize`` would lower-case "OpenCode")."""
    return text[:1].upper() + text[1:]


def _config_file_has_key(path: str) -> bool:
    """Whether an opencode.json holds a key: any ``apiKey``-like entry with a
    value, or an ``{env:NAME}`` placeholder whose variable is set. Read
    only; the value itself never leaves this function."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and ("key" in key.lower() or "token" in key.lower()) and value.strip():
                    match = re.fullmatch(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
                    if match is None or os.environ.get(match.group(1)):
                        found = True
                        return
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def pick_folder(initial: str = "", *, kind: str = "folder", title: str = "") -> str | None:
    """Opens the native folder (or, with ``kind="file"``, file) dialog in a
    separate Python process (tkinter is not safe to drive from a server
    thread) and returns the chosen path, or ``None`` if the dialog was
    cancelled or tkinter is unavailable."""
    script = (
        "import sys\n"
        "try:\n"
        "    import tkinter as tk\n"
        "    from tkinter import filedialog\n"
        "except Exception:\n"
        "    sys.exit(3)\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        "if sys.argv[2] == 'file':\n"
        "    path = filedialog.askopenfilename(title='Choose the OpenCode configuration (opencode.json)', "
        "initialdir=sys.argv[1] or None, filetypes=[('JSON', '*.json'), ('All files', '*.*')])\n"
        "else:\n"
        "    path = filedialog.askdirectory(title=sys.argv[3] or 'Choose a folder', "
        "initialdir=sys.argv[1] or None, mustexist=True)\n"
        "root.destroy()\n"
        "sys.stdout.write(path or '')\n"
    )
    if initial and kind == "file":
        initial = str(Path(initial).expanduser().parent)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, initial, kind,
             title or "Choose the knowledge source (Obsidian vault)"],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    path = completed.stdout.strip()
    return path or None


def make_handler(server: BoardServer):
    """The request handler class bound to one ``BoardServer``: routing only."""
    class Handler(BaseHTTPRequestHandler):
        server_version = "ProgramMind/1.0"

        def log_message(self, format: str, *args: Any) -> None:   # noqa: A002 - stdlib signature
            return   # quiet by default; errors surface as JSON responses

        # -- helpers --------------------------------------------------------

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _local_only(self, *, post: bool) -> None:
            """Only the page served by this server may talk to it (review of
            9 September 2026). The Host header must name this machine and
            port, or a DNS-rebinding page could read every response; a POST
            must come without an Origin or from this origin, and carry JSON,
            or any web page could issue a "simple" cross-origin POST that
            rewrites the configuration, starts paid calls or writes a note
            into the vault."""
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").strip().lower()
            hostname, _, host_port = host.rpartition(":") if not host.startswith("[") or "]:" in host else (host, "", "")
            if not host_port:
                hostname, host_port = host, "80"
            named_host = site_host(server.config)      # the one name this server answers to besides localhost (spec 9.5)
            if hostname.strip("[]") not in _LOCAL_HOSTS | {named_host} or host_port != str(port):
                raise ApiError(403, "This server answers only its own page on this machine.")
            if post:
                origin = (self.headers.get("Origin") or "").strip().lower()
                allowed = {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]", named_host)}
                if origin and origin not in allowed:
                    raise ApiError(403, "Cross-origin requests are not accepted.")
                content_type = (self.headers.get("Content-Type") or "").lower()
                has_body = (self.headers.get("Content-Length") or "0").strip() not in ("", "0")
                if has_body and not content_type.startswith("application/json"):
                    raise ApiError(415, "Send JSON.")

        def _query(self) -> dict[str, str]:
            """The query string as plain strings, the last value of a repeated key winning."""
            from urllib.parse import parse_qs
            raw = self.path.split("?", 1)[1] if "?" in self.path else ""
            return {key: values[-1] for key, values in parse_qs(raw).items() if values}

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "Bad Content-Length.")
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ApiError(413, "Request body too large.")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ApiError(400, "Request body is not valid JSON.")
            return data if isinstance(data, dict) else {}

        def _static(self, name: str) -> None:
            # /static/agents/<agent>/<file> comes from that agent's web folder (restructuring of 10 September 2026).
            if name.startswith("agents/"):
                parts = name.split("/")
                root = (AGENTS_DIR / parts[1] / "web").resolve() if len(parts) == 3 else None
                path = (root / parts[2]).resolve() if root else None
            else:
                root = WEB_DIR.resolve()
                path = (WEB_DIR / name).resolve()
            if path is None or root not in path.parents or not path.is_file():
                self._json(404, {"error": "not found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _spec(self) -> None:
            """The specification as it is on disk (11.1, decision 12): plain
            text, so it opens in a tab and needs no build step and no
            internet. Missing when the program was copied without ``docs/``."""
            if not SPEC_FILE.is_file():
                self._json(404, {"error": "The specification is not next to this copy of the program."})
                return
            body = SPEC_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # -- routing --------------------------------------------------------

        def do_GET(self) -> None:   # noqa: N802 - stdlib naming
            path = self.path.split("?", 1)[0]
            try:
                self._local_only(post=False)
                if path in ("/", "/index.html", "/board", "/ask", "/archive", "/privacy", "/about") \
                        or path.startswith(("/ask/", "/board/")):
                    self._static("index.html")       # one page; the script reads the path (spec 9.5)
                elif path == "/spec":
                    self._spec()
                elif path.startswith("/static/"):
                    self._static(path[len("/static/"):])
                elif path == "/api/config":
                    self._json(200, server.config_view())
                elif path == "/api/status":
                    self._json(200, server.status(self._query().get("projects")))
                elif path == "/api/history":
                    self._json(200, {"items": server.history(self._query().get("state", "open"))})
                elif path.startswith("/api/sessions/"):
                    session = server.get_session(path.split("/")[3])
                    self._json(200, session.snapshot())
                elif path == "/api/ask":
                    self._json(200, {"threads": server.list_threads()})
                elif path.startswith("/api/ask/"):
                    self._json(200, server.get_thread(path.split("/")[3]).snapshot())
                else:
                    self._json(404, {"error": "not found"})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})

        def do_DELETE(self) -> None:   # noqa: N802 - stdlib naming
            path = self.path.split("?", 1)[0]
            try:
                self._local_only(post=True)
                if path.startswith("/api/ask/"):
                    server.delete_thread(path.split("/")[3])
                    self._json(200, {"deleted": True})
                elif path.startswith("/api/history/"):
                    server.delete_record(path.split("/")[3])       # the archive deletes either agent's work
                    self._json(200, {"deleted": True})
                else:
                    self._json(404, {"error": "not found"})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})

        def do_POST(self) -> None:   # noqa: N802 - stdlib naming
            path = self.path.split("?", 1)[0]
            try:
                self._local_only(post=True)
                body = self._body()
                if path == "/api/config":
                    self._json(200, server.update_config(body))
                elif path == "/api/status/ai":
                    self._json(200, server.check_ai())
                elif path == "/api/pick-folder":
                    chosen = pick_folder(str(body.get("initial") or ""), title=str(body.get("title") or ""))
                    self._json(200, {"path": chosen})
                elif path == "/api/roles/install":
                    self._json(200, server.install_roles())
                elif path == "/api/pick-file":
                    chosen = pick_folder(str(body.get("initial") or ""), kind="file")
                    self._json(200, {"path": chosen})
                elif path == "/api/sessions":
                    session = server.start_session(str(body.get("question") or ""), body.get("projects"))
                    server.save_session(session)
                    self._json(201, session.snapshot())
                elif path.startswith("/api/sessions/"):
                    parts = path.split("/")
                    session = server.get_session(parts[3])
                    action = parts[4] if len(parts) > 4 else ""
                    if action == "answers":
                        server.answer(session, list(body.get("answers") or []), final=bool(body.get("final")))
                    elif action == "run":
                        server.run(session, body)
                    elif action == "follow-up":
                        server.follow_up(session, str(body.get("question") or ""), body.get("members"), body.get("mode"))
                    elif action == "estimate":
                        self._json(200, server.estimate(session, body))
                        return
                    elif action == "pick":
                        server.pick(session, body)
                    elif action == "back":
                        server.back(session)
                    elif action == "forward":
                        server.forward(session)
                    elif action == "abandon":
                        server.abandon(session)
                    elif action == "close":
                        server.close(session, bool(body.get("remember")))
                    elif action == "memory":
                        server.write_memory(session, body)
                    elif action == "discard-memory":
                        server.discard_memory(session)
                    else:
                        raise ApiError(404, "unknown action")
                    server.save_session(session)      # the topic on disk follows every step (11.1, decision 9)
                    self._json(200, session.snapshot())
                elif path == "/api/ask":
                    self._json(201, server.new_thread(body.get("projects"), body.get("budget")).snapshot())
                elif path.startswith("/api/ask/"):
                    parts = path.split("/")
                    thread = server.get_thread(parts[3])
                    action = parts[4] if len(parts) > 4 else ""
                    if action == "question":
                        server.ask_question(thread, body)
                    elif action == "read":
                        server.ask_read(thread, body)
                    elif action == "estimate":
                        self._json(200, server.ask_estimate(thread, body))
                        return
                    elif action == "stop":
                        server.ask_stop(thread)
                    elif action == "close":
                        server.ask_close(thread, bool(body.get("remember")))
                    elif action == "memory":
                        server.ask_write_memory(thread, body)
                    elif action == "discard-memory":
                        server.ask_discard_memory(thread)
                    else:
                        raise ApiError(404, "unknown action")
                    self._json(200, thread.snapshot())
                else:
                    self._json(404, {"error": "not found"})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})

    return Handler


def create_http_server(
    config: dict, config_path: Path | None, *, port: int = DEFAULT_PORT, provider: AiProvider | None = None,
) -> tuple[ThreadingHTTPServer, BoardServer]:
    """The listening server on 127.0.0.1 and the ``BoardServer`` behind it."""
    board_server = BoardServer(config, config_path, provider=provider)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(board_server))
    httpd.daemon_threads = True
    return httpd, board_server


def serve(config: dict, config_path: Path | None, *, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    """Runs the server until Ctrl+C. Local only: it binds to 127.0.0.1 and
    nothing else (owner decision, 8 September 2026)."""
    try:
        httpd, _ = create_http_server(config, config_path, port=port)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{port}: {exc}", file=sys.stderr)
        return 1
    listening_port = httpd.server_address[1]
    url = f"http://{site_host(config)}:{listening_port}/"   # spec 9.5: the named address
    print(f"Program Mind is running at {url}  (Ctrl+C to stop)")
    print(f"Also reachable at http://127.0.0.1:{listening_port}/")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        stop_shared()          # spec 4.1: the OpenCode server goes when the program does
    return 0
