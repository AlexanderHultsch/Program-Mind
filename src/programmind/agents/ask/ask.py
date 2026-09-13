"""Ask the vault (spec section 10, decided 10 September 2026): one agent,
no board members, that answers any question from the Obsidian vault and
says where the answer comes from.

The knowledge block is the board's (``knowledge.gather_for_members`` with
one nameless member); the call is one call; the thread keeps every question
and answer so the next question can refer to them. Python does what it can
(AP-1): it assembles the prompt, parses the JSON, keeps only the sources
that were actually sent, and stores the thread as one JSON file outside
the vault. Nothing here writes into the vault; the memory step of the board
does that, after a yes.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from programmind.ai import livejson
from programmind.ai.prompts import load_prompt
from programmind.memory.history import HistoryStore, new_id
from programmind.ai.provider import TASK_BOARD, AiProvider, AiResult
from programmind.knowledge.knowledge import (
    KPI_STALE_DAYS, KPI_TOKEN_CAP, _CHARS_PER_TOKEN, _config_value, _freshness, _project_list, _roles_inside,
    active_projects, for_project, load_vault,
)
from programmind.knowledge.picker import MAX_HISTORY_CHARS, history_lines

KIND = "ask"                              # the ``kind`` of this agent's records in the history folder
MARKER = "## Question to the vault"       # the line the statistics recognise an ask call by
DEFAULT_TOKEN_BUDGET = 12000              # ``ask.token_budget``: kept on the record; no read is capped by it since spec 5.5
MAX_ANSWER_CHARS = 8000
MAX_MISSING = 40                          # pages the model may ask for in one answer (spec 5.4, decision 7; 5.5)
MAX_READS = 2                             # ``ask.max_reads``: answer rounds per question on a partial read (spec 5.5, decision 4; 5.6, decision 4)


@dataclass
class Answer:
    answer: str
    sources: list[dict[str, str]] = field(default_factory=list)   # path, heading, why - only what was sent
    gaps: list[str] = field(default_factory=list)
    dropped: int = 0                                               # sources named that were not sent
    decision_question: bool = False
    missing: list[str] = field(default_factory=list)              # pages the model asked for (spec 5.4), as named
    ai_result: AiResult | None = None
    parse_error: str | None = None


def ask_prompt(question: str, knowledge_text: str, kpi_text: str, history: list[tuple[str, str]],
               project: str = "", *, read_before: list[str] | None = None, contents_text: str = "",
               earlier: "Answer | None" = None, check_note: str = "", round_no: int = 1) -> str:
    """The one prompt: instructions, the thread so far, the pages read for
    it, the question, the KPI notes, the knowledge block, and (spec 5.5)
    the table of contents so the model can name any page. A later round
    carries the earlier answer and what the checker said, before the
    pages, which are everything read so far."""
    lines = [load_prompt("ask"), ""]
    if project:
        lines += [f"Project: {project}", ""]
    if history:
        lines += ["## Earlier in this thread", ""] + history_lines(history, MAX_HISTORY_CHARS) + [""]
    if read_before:
        lines += ["## Pages read earlier in this thread", ""] + [f"- {path}" for path in read_before] + [""]
    lines += [MARKER, "", question.strip(), ""]
    if earlier is not None:
        lines += [f"## Your answer so far (round {round_no - 1})", "", earlier.answer.strip() or "(empty)", ""]
        if earlier.gaps:
            lines += ["Gaps you named: " + "; ".join(earlier.gaps), ""]
        if check_note:
            lines += ["What the check said: " + check_note.strip(), ""]
        lines += [f"This is round {round_no}: the pages below are everything read so far, the pages added for this "
                  "round among them. Write the whole answer again, complete.", ""]
    if kpi_text:
        lines += ["## KPI notes of the project", "", kpi_text, ""]
    if knowledge_text:
        lines += [knowledge_text, ""]
    if contents_text:
        lines += ["## Table of contents of the vault", "",
                  "Every page of the vault, read or not. Name in `missing` any page here that would change the answer.", "",
                  contents_text, ""]
    return "\n".join(lines)


def live_answer(partial: str) -> str:
    """The answer as far as the model has written it (spec 4.1), read out of
    the half-finished JSON the stream carries. For the page's eye only: the
    answer Python keeps is the one ``parse_answer`` reads from the finished
    call."""
    return livejson.field(partial, "answer")


def _note_key(path: str) -> str:
    name = path.replace("\\", "/").strip().strip("[]").lower()
    return name[:-3] if name.endswith(".md") else name


def parse_answer(text: str, sent: dict[str, str], briefs: dict[str, str] | None = None) -> Answer:
    """The model's JSON with Python's guarantees applied: only sources
    that were sent (in full, or as a one-line summary) survive, by path or
    by file name; a heading survives only when the page's sent text
    carries it; the rest is dropped and counted."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return Answer(answer=text.strip()[:MAX_ANSWER_CHARS], parse_error="the answer did not parse as JSON")
    by_key: dict[str, str] = {}
    names: dict[str, int] = {}
    known = dict(sent)
    for path in (briefs or {}):
        known.setdefault(path, "")
    for path in known:
        key = _note_key(path)
        by_key[key] = path
        name = key.rsplit("/", 1)[-1]
        names[name] = names.get(name, 0) + 1
    for path in known:
        name = _note_key(path).rsplit("/", 1)[-1]
        if names[name] == 1:
            by_key.setdefault(name, path)
    sources: list[dict[str, str]] = []
    dropped = 0
    seen: set[tuple[str, str]] = set()
    for item in data.get("sources") or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        named = str(item.get("path") or "")
        path = by_key.get(_note_key(named)) or by_key.get(_note_key(named).rsplit("/", 1)[-1])
        if not path:
            dropped += 1
            continue
        heading = str(item.get("heading") or "").strip()
        body = sent.get(path, "")
        if heading and not re.search(r"^#+\s*" + re.escape(heading) + r"\s*$", body, flags=re.I | re.M):
            heading = ""
        if (path, heading) in seen:
            continue
        seen.add((path, heading))
        sources.append({"path": path, "heading": heading, "why": str(item.get("why") or "").strip()[:200],
                        "brief": path not in sent})
    gaps = data.get("gaps")
    if isinstance(gaps, str):
        gaps = [g.strip("- ").strip() for g in gaps.splitlines()]
    gaps = [str(g).strip() for g in (gaps or []) if str(g).strip()][:10]
    missing_raw = data.get("missing")
    if isinstance(missing_raw, str):
        missing_raw = [missing_raw]
    missing: list[str] = []
    for item in (missing_raw if isinstance(missing_raw, list) else []):
        name = str(item.get("path") or "") if isinstance(item, dict) else str(item)
        name = name.strip()
        if name and name not in missing:
            missing.append(name)
    return Answer(answer=str(data.get("answer") or "").strip()[:MAX_ANSWER_CHARS], sources=sources, gaps=gaps,
                  dropped=dropped, decision_question=bool(data.get("decision_question")), missing=missing[:MAX_MISSING])


def ask(provider: AiProvider, question: str, knowledge_text: str, kpi_text: str, history: list[tuple[str, str]],
        sent: dict[str, str], briefs: dict[str, str] | None = None, project: str = "", *,
        read_before: list[str] | None = None, contents_text: str = "", earlier: Answer | None = None,
        check_note: str = "", round_no: int = 1, on_text=None) -> Answer:
    """One call, first round or later. Raises whatever the provider raises.

    ``on_text`` is handed the answer as the model writes it, when the
    provider streams (spec 4.1); what it receives is for the page, and the
    answer is the one parsed from the finished call."""
    ai_result = provider.complete(TASK_BOARD, ask_prompt(question, knowledge_text, kpi_text, history, project,
                                                         read_before=read_before, contents_text=contents_text,
                                                         earlier=earlier, check_note=check_note, round_no=round_no),
                                  on_text=on_text)
    answer = parse_answer(ai_result.text, sent, briefs)
    answer.ai_result = ai_result
    return answer


def project_kpi_text(config: dict, projects=None, *, today: date | None = None) -> str:
    """Every KPI note of the chosen project(s), whole, with its freshness
    line, within ``KPI_TOKEN_CAP`` - the agent has no swim lane, so it gets
    them all."""
    vault_path = _config_value(config, "knowledge.vault_path")
    if not vault_path:
        return ""
    vault = Path(str(vault_path)).expanduser()
    chosen = _project_list(projects) if projects is not None else active_projects(config)
    notes = [n for n in for_project(load_vault(vault, skip_subfolders=_roles_inside(config, vault)), chosen) if n.kind == "kpi"]
    stale_days = int(_config_value(config, "knowledge.kpi_stale_days") or KPI_STALE_DAYS)
    day = today or date.today()
    limit = KPI_TOKEN_CAP * _CHARS_PER_TOKEN
    kept: list[str] = []
    used = 0
    for note in notes:
        chunk = f"### {note.relative}\n{_freshness(note, day, stale_days)}\n\n{note.body.strip()}"
        if used + len(chunk) > limit and kept:
            break
        kept.append(chunk[:limit])
        used += len(chunk)
    text = "\n\n".join(kept)
    if len(notes) > len(kept):
        text += f"\n\n[{len(notes) - len(kept)} further KPI note(s) omitted: over the KPI budget]"
    return text


# -- threads ------------------------------------------------------------------

@dataclass
class Thread:
    """One conversation with the vault, kept until Alex closes it and
    readable after that. Stored as one JSON file, never in the vault."""
    id: str
    projects: list[str] = field(default_factory=list)
    budget: int = DEFAULT_TOKEN_BUDGET
    status: str = "open"                                   # open, closed
    turns: list[dict[str, Any]] = field(default_factory=list)   # question, answer, sources, gaps, dropped, paths, briefs, at
    calls: list[dict[str, Any]] = field(default_factory=list)   # the statistics rows
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    written_path: str | None = None                        # the vault note written at the close, if any
    extra: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return (self.turns[0]["question"] if self.turns else "").strip()[:120] or "New thread"

    def history(self) -> list[tuple[str, str]]:
        return [(t["question"], t["answer"]) for t in self.turns if t.get("answer")]

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "questions": len(self.turns),
                "projects": list(self.projects), "created": self.created, "updated": self.updated}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": KIND, "projects": list(self.projects), "budget": self.budget, "status": self.status,
                "turns": self.turns, "calls": self.calls, "created": self.created, "updated": self.updated,
                "written_path": self.written_path, "extra": list(self.extra), "exclude": list(self.exclude)}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Thread":
        thread = Thread(id=str(data.get("id") or uuid.uuid4().hex[:12]))
        thread.projects = [str(p) for p in data.get("projects") or []]
        thread.budget = int(data.get("budget") or DEFAULT_TOKEN_BUDGET)
        thread.status = "closed" if data.get("status") == "closed" else "open"
        thread.turns = [t for t in data.get("turns") or [] if isinstance(t, dict)]
        thread.calls = [c for c in data.get("calls") or [] if isinstance(c, dict)]
        thread.created = float(data.get("created") or time.time())
        thread.updated = float(data.get("updated") or thread.created)
        thread.written_path = data.get("written_path")
        thread.extra = [str(x) for x in data.get("extra") or []]
        thread.exclude = [str(x) for x in data.get("exclude") or []]
        return thread


class ThreadStore:
    """The threads of this agent inside the shared history folder (spec
    11.1, decision 9): one JSON file per thread, ``kind: ask``, never in
    the vault. A file that does not parse is skipped, never deleted."""

    def __init__(self, folder: Path | str) -> None:
        self.store = HistoryStore(folder)

    @property
    def folder(self) -> Path:
        return self.store.folder

    def _path(self, thread_id: str) -> Path:
        return self.store.path(thread_id)

    def new(self, projects: list[str] | None = None, budget: int | None = None) -> Thread:
        return Thread(id=new_id(), projects=list(projects or []), budget=int(budget or DEFAULT_TOKEN_BUDGET))

    def save(self, thread: Thread) -> Path:
        thread.updated = time.time()
        return self.store.save(thread.id, thread.to_dict())

    def load(self, thread_id: str) -> Thread | None:
        data = self.store.load(thread_id)
        if data is None or data.get("kind", KIND) != KIND:
            return None                     # a board topic is not a thread
        return Thread.from_dict(data)

    def delete(self, thread_id: str) -> bool:
        return self.store.delete(thread_id)

    def list(self) -> list[dict[str, Any]]:
        """Newest first. A record written before the ``kind`` property
        existed is a thread: nothing else was kept then."""
        threads = [Thread.from_dict(data) for data in self.store.records()
                   if data.get("kind", KIND) == KIND]
        threads.sort(key=lambda t: t.updated, reverse=True)
        return [t.summary() for t in threads]
