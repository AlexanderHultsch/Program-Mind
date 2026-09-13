"""AI Board orchestration (docs/spec.md chapter 9, FR-3.1..FR-3.6).

The board's members are whoever has a role profile in the roles folder
(section 3.4, ``roles.py``) - there is no member list in code. Each
produces a separate, clearly attributed assessment (FR-3.3): view, risks,
recommendation. FR-3.3a is a deliberate exception to AP-3 (one trigger,
one batched run): members are polled in isolation, one model call per
member, so a call never contains another member's answer. Batching all
members into one call is cheaper and would satisfy AP-3, but a model
writing the last assessment can see the ones it has already written and
converges towards them - the disagreement FR-3.6 exists to surface would
be smoothed away before anyone could read it. One board run is therefore
N+1 calls: one per member plus one synthesis call over the collected
assessments (FR-3.4).

Nothing here degrades to a partial answer without a provider - N
perspectives minus a model is not a board, so ``run_board`` raises rather
than reporting "not configured" and carrying on.

The module renders nothing but the tables and prose FR-3.5 asks Python,
not the model, to produce - ``render`` turns a ``BoardResult`` into text;
the CLI (``cli.py``) is the only place that prints it.

Since 9 September 2026 there is also a combined form, ``run_board_combined``:
one call writes every chosen member's entry and the synthesis. It is not
the default and it is exactly the batching the paragraph above argues
against; it exists as a stated cost trade-off (about one call instead of
N+1), the interface names it, and ``_entry_flags`` checks each entry for
the convergence the isolation was meant to prevent.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from programmind.ai.prompts import load_prompt
from programmind.ai.provider import TASK_BOARD, AiNotConfiguredError, AiProvider, AiResult, is_configured
from programmind.memory.audit import log_run
from programmind.knowledge.knowledge import active_project, kpi_notes
from programmind.agents.board.roles import Board, RoleProfile, load_board


def _get(config: dict, dotted: str, default: Any = None) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node not in ("", None) else default


@dataclass(frozen=True)
class MemberAssessment:
    member: str
    view: str
    risks: str
    recommendation: str
    applies: bool = True      # False: the member said, with reasons, that the topic does not touch it
    impact: str = ""          # the dependency chain into this member's area, one bullet per step
    # Where the material came from (decided 9 September 2026): facts the member
    # took from the knowledge net, each with its note and Python's verdict on
    # whether that note was sent and carries the wording; and the member's own
    # judgement, one bullet each. Anything not in ``sources`` is the model's.
    sources: tuple[dict[str, Any], ...] = ()
    judgement: str = ""
    flags: tuple[str, ...] = ()   # Python's warnings on this entry, e.g. "generic" in the combined mode


@dataclass
class BoardResult:
    topic: str
    assessments: list[MemberAssessment]
    synthesis: str
    failed_members: list[str]     # member plus why, short strings
    ai_result: AiResult | None    # the synthesis call's AiResult, for AI-2
    llm_calls: int = 0
    synthesis_data: dict[str, Any] | None = None   # the parsed synthesis JSON, for the HMI
    sources: dict[str, Any] = field(default_factory=dict)   # network notes used, unverified citations, judgement count
    mode: str = "individual"      # "individual": one call per member; "combined": one call for all


@dataclass
class BoardConversation:
    """A follow-up conversation attached to one completed ``BoardResult``
    (the owner's decision: "die Nachfrage reicht in der Synthese"). Lives for
    the duration of one ``board`` CLI invocation only - nothing here is
    written to disk or resumed across invocations."""
    result: BoardResult
    turns: list[tuple[str, str]]   # (question, answer), in order
    llm_calls: int = 0             # follow-up calls only; run_board counts its own
    roles: dict[str, RoleProfile] | None = None   # the profiles the run used
    # What a member needs to be asked again (decided 9 September 2026:
    # a follow-up may go back to chosen members): the input the run used,
    # the conduct note, the KPI data per member and the project.
    inputs: dict[str, Any] = field(default_factory=dict)
    conduct: str = ""
    member_data: dict[str, str] = field(default_factory=dict)
    project: str = ""
    sent_notes: dict[str, str] = field(default_factory=dict)   # note path -> body, what every member received
    member_knowledge: dict[str, str] = field(default_factory=dict)   # member -> its own knowledge block
    member_notes: dict[str, dict[str, str]] = field(default_factory=dict)   # member -> note path -> text sent
    # Spec 5.1: the shared core once, and each member's delta, for the combined form.
    shared_knowledge: str = ""
    member_delta: dict[str, str] = field(default_factory=dict)


@dataclass
class FollowUp:
    """One follow-up turn: the board's answer, structured where the model
    kept the shape, and the answers of the members asked again (if any)."""
    question: str
    answer: str                                   # plain text, always
    data: dict[str, Any] | None = None            # the parsed JSON answer, for the HMI
    assessments: list[MemberAssessment] = field(default_factory=list)
    failed_members: list[str] = field(default_factory=list)
    llm_calls: int = 0


def _input_block(topic: str, context: str, options: tuple[str, ...], constraints: tuple[str, ...]) -> list[str]:
    lines = ["## Input (FR-3.1)", "", f"Topic: {topic}"]
    if context:
        lines.append(f"Context: {context}")
    if options:
        lines.append("Options under consideration:")
        lines.extend(f"- {option}" for option in options)
    if constraints:
        lines.append("Hard constraints:")
        lines.extend(f"- {constraint}" for constraint in constraints)
    return lines


def _member_prompt(
    topic: str, context: str, options: tuple[str, ...], constraints: tuple[str, ...], member: str,
    role: RoleProfile | None = None, conduct: str = "", kpi_data: str = "", project: str = "",
    knowledge: str = "",
) -> str:
    """The prompt for one member's call.

    ``load_prompt("board_members")`` describes the single-member contract
    (isolation, response shape) FR-3.3a requires. The parts every member
    receives alike come first - the rules, the conduct note, the input -
    so a gateway that caches a shared prefix serves them from cache
    (decided 9 September 2026); then this member's knowledge block, then
    what is only this member's: its name, profile and KPI data."""
    lines = [load_prompt("board_members")]
    if conduct:
        lines += ["", "## Board member conduct (section 3.4, the same for every member)", "", conduct]
    lines += ["", *_input_block(topic, context, options, constraints)]
    if knowledge:
        lines += ["", knowledge]
    lines += ["", "## Member (FR-3.3a)", "", f"Member: {member}"]
    if project:
        lines += [
            f"Project: {project}. This question belongs to this project. The knowledge and KPI "
            "data were selected for it; pages of other projects were left out.",
        ]
    if role is not None and role.body:
        lines += ["", "## Role profile (section 3.4)", ""]
        if role.roles:
            lines.append(
                f"This member is the {role.member} swim lane. It speaks as its highest-ranked role, "
                f"{role.title} (level {role.level}), and answers for every role in the swim lane. "
                "Roles by rank - a lower level number is a higher rank and carries more weight where "
                "roles would disagree:"
            )
            lines.extend(f"- level {r.level}: {r.name}" for r in role.roles)
            lines.append("")
        lines.append(
            "This profile is authoritative for this call: assess from these responsibilities, "
            "against these KPIs, in this vocabulary."
        )
        lines += ["", role.body]
    if kpi_data:
        lines += [
            "",
            "## KPI data from the knowledge network (section 3.4)",
            "",
            "The values behind the targets this member is judged on, as recorded in the "
            "knowledge network. Baseline MG0 unless the note says otherwise. Judge every "
            "option against these numbers, name the delta, and quote the date a value was "
            "recorded whenever you use it.",
            "",
            kpi_data,
        ]
    elif role is not None and role.body:
        lines += [
            "",
            "## KPI data from the knowledge network (section 3.4)",
            "",
            "No KPI note for this member is in the knowledge network yet. Where a target "
            "would decide the answer, say that its value is not recorded and state the "
            "assumption you use instead.",
        ]
    lines += ["", "Answer now for this member, as the JSON object described above."]
    return "\n".join(lines)


def _member_questions(text: str) -> list[str] | None:
    """The questions a member returned instead of an assessment, if that
    is what it did (the ``{"status": "questions"}`` shape). Since the
    clarifier step in front of the board, members are told not to do this;
    when one does anyway, the questions are recorded against that member
    rather than lost."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("status") == "questions":
        questions = data.get("questions")
        return [str(item) for item in questions] if isinstance(questions, list) else []
    return None


def _parse_member_response(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _parse_member_dict(data)


def _parse_member_dict(data: Any) -> dict[str, Any] | None:
    """One member's answer as the fields the board keeps, or ``None`` when
    the three required keys are missing."""
    if not isinstance(data, dict) or not all(key in data for key in ("view", "risks", "recommendation")):
        return None
    applies = data.get("applies", True)
    if isinstance(applies, str):
        applies = applies.strip().lower() not in ("false", "no", "0")
    raw_facts = data.get("facts_from_network")
    facts: list[dict[str, Any]] = []
    for item in (raw_facts if isinstance(raw_facts, list) else []):
        if isinstance(item, dict):
            fact, source = str(item.get("fact", "")).strip(), str(item.get("source", "")).strip()
        else:
            fact, source = str(item).strip(), ""
        if fact:
            facts.append({"fact": fact, "source": source})
    return {
        "view": _bullets(data["view"]),
        "risks": _bullets(data["risks"]),          # one bullet per risk; the interface shows "- " lines as a list
        "recommendation": _bullets(data["recommendation"]),
        "applies": bool(applies),
        "impact": _bullets(data.get("impact", "")),
        "sources": tuple(facts),
        "judgement": _bullets(data.get("own_judgement", "")),
    }


def _bullets(value: Any) -> str:
    """A model field as text: a string as it is, a list as one "- " bullet
    per item (nested lists flattened one level), ``None`` as empty - never
    a Python repr in the reader's face."""
    if value is None:
        return ""
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, list):
                items.extend(str(x).strip() for x in item if str(x).strip())
            elif isinstance(item, dict):
                items.append(", ".join(f"{k}: {v}" for k, v in item.items()))
            elif str(item).strip():
                items.append(str(item).strip())
        return "\n".join(f"- {item}" for item in items)
    if isinstance(value, dict):
        return "\n".join(f"- {k}: {v}" for k, v in value.items())
    return str(value)


_SOURCE_STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "have", "will", "which", "about",
                     "their", "there", "than", "then", "were", "been", "into", "also", "only", "when", "what"}


def _note_key(path: str) -> str:
    name = path.replace("\\", "/").strip().strip("[]").lower()
    return name[:-3] if name.endswith(".md") else name


def verify_sources(facts: list[dict[str, Any]] | tuple[dict[str, Any], ...], sent: dict[str, str]) -> list[dict[str, Any]]:
    """Python's verdict on every fact a member says it took from the
    knowledge net (AP-1). The cited note must be one the board actually
    sent - by path, file name or title - and some distinctive word of the
    fact must appear in that note. A fact that fails is kept and flagged,
    never dropped: the reader sees what the model claimed and what held."""
    by_key: dict[str, tuple[str, str]] = {}
    names: dict[str, int] = {}
    for path in sent:
        names[_note_key(path).rsplit("/", 1)[-1]] = names.get(_note_key(path).rsplit("/", 1)[-1], 0) + 1
    for path, body in sent.items():
        key = _note_key(path)
        by_key[key] = (path, body)
        name = key.rsplit("/", 1)[-1]
        if names[name] == 1:                     # a bare file name only when it is unambiguous
            by_key[name] = (path, body)
    verdicts: list[dict[str, Any]] = []
    for item in facts:
        fact, source = str(item.get("fact", "")), str(item.get("source", ""))
        hit = by_key.get(_note_key(source)) or by_key.get(_note_key(source).rsplit("/", 1)[-1])
        if hit is None:
            verdicts.append({"fact": fact, "source": source, "verified": False,
                             "note": "not among the notes sent to this member" if source else "no note named"})
            continue
        path, body = hit
        lowered = body.lower()
        words = [w for w in re.findall(r"[\w][\w.,%-]{3,}", fact.lower()) if w.strip(".,%-") not in _SOURCE_STOPWORDS]
        found = [w for w in words if w in lowered]     # substring, so "6 weeks" matches "6-week" and plurals
        if words and not found:
            verdicts.append({"fact": fact, "source": path, "verified": False,
                             "note": "the note was sent, but none of the fact's words appear in it"})
        else:
            verdicts.append({"fact": fact, "source": path, "verified": True, "note": ""})
    return verdicts


def _kpi_note_bodies(member_data_text: str) -> dict[str, str]:
    """The KPI notes inside one member's data block, by the ``### path``
    headings ``knowledge.kpi_notes`` writes."""
    bodies: dict[str, str] = {}
    for chunk in re.split(r"(?m)^(?=### .+\.md\s*$)", member_data_text or ""):
        match = re.match(r"^### (.+\.md)\s*$", chunk, re.M)
        if match:
            bodies[match.group(1).strip()] = chunk
    return bodies


def _sent_to(member: str, sent_notes: dict[str, str] | None, member_notes: dict[str, dict[str, str]] | None,
             member_data: dict[str, str] | None) -> dict[str, str]:
    """Everything this member received from the vault: the shared notes,
    its own block, its KPI notes - the set its citations are checked against."""
    sent = dict(sent_notes or {})
    sent.update((member_notes or {}).get(member, {}))
    sent.update(_kpi_note_bodies((member_data or {}).get(member, "")))
    return sent


def _summarise_sources(assessments: list[MemberAssessment]) -> dict[str, Any]:
    network: list[str] = []
    unverified: list[str] = []
    judgement = 0
    for a in assessments:
        for s in a.sources:
            if s.get("verified"):
                if s["source"] not in network:
                    network.append(s["source"])
            else:
                unverified.append(f"{a.member}: \"{s.get('fact', '')}\" -> {s.get('source') or '(no note)'}: {s.get('note', '')}")
        judgement += len([line for line in a.judgement.splitlines() if line.strip()])
    return {"network": network, "unverified": unverified, "judgement_count": judgement}


def _synthesis_prompt(
    assessments: list[MemberAssessment], roles: dict[str, RoleProfile] | None = None
) -> str:
    payload = [
        {
            "member": assessment.member,
            "applies": assessment.applies,
            "view": assessment.view,
            "impact": assessment.impact,
            "risks": assessment.risks,
            "recommendation": assessment.recommendation,
            "facts_from_network": [
                {"fact": s["fact"], "source": s["source"], "verified_by_python": bool(s.get("verified"))}
                for s in assessment.sources
            ],
            "own_judgement": assessment.judgement,
        }
        for assessment in assessments
    ]
    prompt = load_prompt("board_synthesis")
    if roles:
        # One line per member (section 3.4): the synthesis weighs who said
        # what; it never receives a member's full profile.
        prompt += "\n\n## Board members\n\n" + "\n".join(
            f"- {role.title}: {role.perspective}" for role in roles.values() if role.perspective
        )
    return prompt + "\n\n## Assessments\n\n" + json.dumps(payload, indent=2)


def _synthesis_text(data: dict[str, Any]) -> str:
    lines = [
        f"Overall recommendation: {data.get('overall_recommendation', '')}",
        f"Decisive criterion: {data.get('decisive_criterion', '')}",
    ]
    counter_arguments = data.get("counter_arguments") or []
    if counter_arguments:
        lines.append("Counter-arguments:")
        lines.extend(f"  - {item}" for item in counter_arguments)
    lines.append(f"What would change it: {data.get('what_would_change_it', '')}")
    rests = data.get("rests_on_judgement") or []
    if rests:
        lines.append("Rests on judgement (not in the knowledge net):")
        lines.extend(f"  - {item}" for item in rests)
    disagreements = data.get("disagreements") or []
    if disagreements:
        lines.append("Disagreements (FR-3.6):")
        lines.extend(f"  - {item}" for item in disagreements)
    else:
        lines.append("Disagreements (FR-3.6): none stated.")
    not_affected = data.get("not_affected") or []
    if not_affected:
        lines.append("Not affected:")
        lines.extend(f"  - {item}" for item in not_affected)
    return "\n".join(lines)


_TOOL_ONLY_MARKERS = ("no answer text", "called tool(s) instead of answering")
_RETRY_PREFIX = (
    "IMPORTANT: your previous attempt at this call used tools and returned no text. "
    "You have no tools. Do not read, search or list anything. Reply with the JSON object only.\n\n"
)


def _is_tool_only_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TOOL_ONLY_MARKERS)


class CallFailed(RuntimeError):
    """A member call that failed, carrying how many calls were made so the
    cost is counted even when nothing came back."""

    def __init__(self, cause: BaseException, calls: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.calls = calls


def _complete_with_retry(provider: AiProvider, prompt: str, on_text=None) -> tuple[AiResult, int]:
    """One member call, retried once when the model called tools instead of
    answering (seen 9 September 2026: Manufacturing ran ``glob`` and
    produced no text). Returns the result and the number of calls made;
    raises ``CallFailed`` with the count when both attempts fail.
    ``on_text`` receives the answer as it is written (spec 4.1, 5.8)."""
    try:
        return provider.complete(TASK_BOARD, prompt, on_text=on_text), 1
    except Exception as exc:
        if not _is_tool_only_failure(exc):
            raise CallFailed(exc, 1) from exc
    try:
        return provider.complete(TASK_BOARD, _RETRY_PREFIX + prompt, on_text=on_text), 2
    except Exception as exc:
        raise CallFailed(exc, 2) from exc


def run_board(
    config: dict,
    provider: AiProvider | None,
    *,
    topic: str,
    context: str = "",
    options: tuple[str, ...] = (),
    constraints: tuple[str, ...] = (),
    on_member: Callable[[str, str], None] | None = None,
    on_assessment: Callable[[MemberAssessment], None] | None = None,
    board: Board | None = None,
    member_data: dict[str, str] | None = None,
    members: tuple[str, ...] | list[str] | None = None,
    sent_notes: dict[str, str] | None = None,
    member_knowledge: dict[str, str] | None = None,
    member_notes: dict[str, dict[str, str]] | None = None,
    on_text: Callable[[str, str], None] | None = None,
) -> BoardResult:
    """One AI Board run (FR-3.1..FR-3.6): one isolated call per member, then
    one synthesis call over what they produced. ``roles`` is the board
    (section 3.4); when not given it is read from the configuration's roles
    folder - fresh, on this run.

    ``on_member(member, state)`` is called from the worker threads as each
    member starts (``"running"``) and finishes (``"done"`` or ``"failed"``),
    so a front end can show progress. It carries no answer text - member
    isolation (FR-3.3a) is not weakened by a progress callback.

    Raises ``AiNotConfiguredError`` when no provider is given or
    ``provider.models.board`` has no model configured - unlike the other
    three triggers, the board does not degrade to a partial answer without a
    model (perspectives minus a model is not a board).

    A member whose response does not parse as JSON, or is missing one of
    ``view``/``risks``/``recommendation``, is recorded in ``failed_members``
    and left out of ``assessments`` - the other members still run. With
    fewer than two assessments a synthesis is skipped rather than produced
    over too little to synthesise, and that is stated plainly in
    ``synthesis`` rather than attempted anyway."""
    start = time.monotonic()
    if provider is None or not is_configured(config, TASK_BOARD):
        raise AiNotConfiguredError(
            "AI Board has no model configured - set provider.models.board"
        )

    if board is None:
        board = load_board(config)
    roles = board.profiles
    conduct = board.conduct
    if members:
        # Decided 9 September 2026: Alex chooses which members are asked;
        # a topic need not concern every swim lane.
        chosen = {str(m).strip().lower() for m in members}
        roles = {name: role for name, role in roles.items() if name.lower() in chosen}
        if not roles:
            raise ValueError("none of the chosen members is on the board")
    members = tuple(roles)                          # from here on: the members actually run
    if member_data is None:
        # The KPI data each member is judged against, attached deterministically
        # (AP-1) whatever the question - never left to the ranked selection.
        member_data = kpi_notes(config, members)
    project = active_project(config) or ""
    audit_folder = _get(config, "runtime.audit_folder")
    pc_name = _get(config, "storage.pc_name", "")

    def _log(result: BoardResult) -> BoardResult:
        # There is no data store in this repository (decision 0005) - the
        # board is the only run there is, so it logs unconditionally rather
        # than skipping when a store is absent.
        ai_result = result.ai_result
        log_run(
            "board",
            audit_folder=audit_folder, pc_name=pc_name,
            duration_seconds=time.monotonic() - start,
            counts={
                "assessments": len(result.assessments),
                "failed_members": len(result.failed_members),
                "llm_calls": result.llm_calls,
                "over_token_limit": 1 if ai_result is not None and ai_result.over_token_limit else 0,
            },
            provider=ai_result.provider if ai_result is not None else None,
            model=ai_result.model if ai_result is not None else None,
            tokens=ai_result.total_tokens if ai_result is not None else None,
        )
        return result

    assessments: list[MemberAssessment] = []
    failed_members: list[str] = []

    # FR-3.3a: one call per member, in isolation - every prompt is built
    # here, before any call is dispatched, so it is structurally
    # impossible, not merely conventional, for one member's prompt to
    # contain another member's answer. Polling the calls concurrently
    # below does not weaken that isolation, it strengthens it: none of the
    # calls can see another's response, because none of them has
    # produced one yet when they are submitted.
    prompts = [
        _member_prompt(topic, context, options, constraints, member, roles[member], conduct,
                       member_data.get(member, ""), project, (member_knowledge or {}).get(member, ""))
        for member in members
    ]

    def _notify(member: str, state: str) -> None:
        if on_member is not None:
            try:
                on_member(member, state)
            except Exception:   # a progress display must never take a run down
                pass

    extra_calls = [0]                               # retries, counted under the lock
    early: dict[str, MemberAssessment] = {}         # parsed and verified as each answer arrives
    early_lock = threading.Lock()

    def _assessment(member: str, parsed: dict[str, Any]) -> MemberAssessment:
        parsed["sources"] = tuple(verify_sources(parsed["sources"], _sent_to(member, sent_notes, member_notes, member_data)))
        return MemberAssessment(member=member, **parsed)

    def _call(member: str, prompt: str) -> AiResult:
        _notify(member, "running")
        try:
            # Spec 5.8, decision 5: the page shows each member's view as it is written.
            sink = (lambda text, who=member: on_text(who, text)) if on_text is not None else None
            result, calls = _complete_with_retry(provider, prompt, sink)
        except CallFailed as failed:
            with early_lock:
                extra_calls[0] += failed.calls - 1
            _notify(member, "failed")
            raise failed.cause
        with early_lock:
            extra_calls[0] += calls - 1
        parsed = _parse_member_response(result.text)
        if parsed is not None:
            # Decided 9 September 2026: an answer can be read as soon as it
            # is in, while the others still think. It is verified here, once,
            # and handed out; the final list below reuses it. Isolation
            # (FR-3.3a) is untouched - it goes to Alex, never to a member.
            assessment = _assessment(member, parsed)
            with early_lock:
                early[member] = assessment
            if on_assessment is not None:
                try:
                    on_assessment(assessment)
                except Exception:   # a progress display must never take a run down
                    pass
        _notify(member, "done" if parsed is not None else "failed")
        return result

    results: list[AiResult | None] = [None] * len(members)
    errors: list[BaseException | None] = [None] * len(members)
    with ThreadPoolExecutor(max_workers=len(members)) as executor:
        futures = [
            executor.submit(_call, member, prompt) for member, prompt in zip(members, prompts)
        ]
        for index, future in enumerate(futures):
            try:
                results[index] = future.result()
            except Exception as exc:
                errors[index] = exc

    llm_calls = len(members) + extra_calls[0]

    for member, result, error in zip(members, results, errors):
        if error is not None:
            failed_members.append(f"{member}: {error}")
            continue
        if member in early:
            assessments.append(early[member])
            continue
        # Not in ``early``: the answer did not parse. Say how it failed.
        questions = _member_questions(result.text)
        if questions is not None:
            asked = " | ".join(questions) if questions else "(none listed)"
            failed_members.append(f"{member}: asked questions instead of assessing: {asked}")
        else:
            failed_members.append(f"{member}: response did not parse as JSON with view/risks/recommendation")

    if len(assessments) < 2:
        return _log(BoardResult(
            topic=topic,
            assessments=assessments,
            synthesis=(
                "Only one member answered, so there is nothing to consolidate: its assessment is the answer."
                if len(assessments) == 1 else
                "No member produced an assessment, so there is nothing to consolidate."
            ),
            failed_members=failed_members,
            ai_result=None,
            llm_calls=llm_calls,
            sources=_summarise_sources(assessments),
        ))

    ai_result = provider.complete(TASK_BOARD, _synthesis_prompt(assessments, roles),
                                  on_text=(lambda text: on_text("", text)) if on_text is not None else None)
    llm_calls += 1

    try:
        synthesis_data = json.loads(ai_result.text)
    except json.JSONDecodeError:
        synthesis_data = None

    if isinstance(synthesis_data, dict):
        synthesis = _synthesis_text(synthesis_data)
    else:
        synthesis = f"Synthesis response did not parse as JSON: {ai_result.text}"
        synthesis_data = None

    return _log(BoardResult(
        topic=topic,
        assessments=assessments,
        synthesis=synthesis,
        failed_members=failed_members,
        ai_result=ai_result,
        llm_calls=llm_calls,
        synthesis_data=synthesis_data,
        sources=_summarise_sources(assessments),
    ))


def _member_section(member: str, role: RoleProfile | None, kpi_data: str, knowledge: str = "") -> list[str]:
    """One member's material inside the combined prompt: the same profile,
    knowledge and KPI block the single call gets, under the member's heading."""
    lines = [f"### Member: {member}", ""]
    if knowledge:
        lines += [knowledge.replace("## Knowledge selected for this member", "#### Knowledge selected for this member", 1)
                  .replace("## Knowledge from the vault", "#### Knowledge selected for this member", 1)
                  .replace("## Further pages in the vault, one line each", "#### Further pages in the vault, one line each", 1), ""]
    if role is not None and role.roles:
        lines.append(f"This member is the {role.member} swim lane. It speaks as {role.title} (level {role.level}) "
                     "and answers for every role in the swim lane. Roles by rank:")
        lines.extend(f"- level {r.level}: {r.name}" for r in role.roles)
        lines.append("")
    if role is not None and role.body:
        lines += [role.body, ""]
    if kpi_data:
        lines += ["#### KPI data from the knowledge network", "", kpi_data, ""]
    else:
        lines += ["#### KPI data from the knowledge network", "",
                  "No KPI note for this member is in the knowledge network yet.", ""]
    return lines


def _combined_prompt(
    topic: str, context: str, options: tuple[str, ...], constraints: tuple[str, ...],
    roles: dict[str, RoleProfile], conduct: str, member_data: dict[str, str], project: str,
    member_knowledge: dict[str, str] | None = None, shared_knowledge: str = "",
    member_delta: dict[str, str] | None = None,
) -> str:
    """With ``shared_knowledge`` and ``member_delta`` (spec 5.1) the core
    goes in once, before the members, and each member carries only its own
    tier; without them every member carries its whole block, as before."""
    lines = [load_prompt("board_combined"), ""]
    if conduct:
        lines += ["## Board member conduct (the same for every member)", "", conduct, ""]
    lines += [*_input_block(topic, context, options, constraints), ""]
    if project:
        lines += [f"Project: {project}. This question belongs to this project.", ""]
    if shared_knowledge:
        lines += [shared_knowledge, ""]
    lines += ["## Members to assess, in this order", ""]
    lines.append(", ".join(roles))
    lines.append("")
    per_member = member_delta if member_delta is not None else (member_knowledge or {})
    for member, role in roles.items():
        lines += _member_section(member, role, member_data.get(member, ""), per_member.get(member, ""))
    return "\n".join(lines)


_GENERIC_MIN_TERMS = 1
_ROLE_TERM_NOISE = _SOURCE_STOPWORDS | {
    "targets", "judged", "process", "protect", "cannot", "everything", "member", "network", "knowledge",
    "values", "baseline", "record", "tasks", "official", "owner", "writes", "before", "answering", "question",
    "touches", "defines", "points", "there", "every", "phase", "itself", "pages", "affected", "swimlanes",
    "section", "level",
}
_ROLE_TERM_SECTIONS = ("Targets I am judged on", "Process", "What I protect when I cannot have everything")
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
_role_term_cache: dict[tuple[str, str], frozenset[str]] = {}


def _role_terms(role: RoleProfile | None) -> set[str]:
    """Distinctive words from the sections that make a role its own: the
    targets it is judged on, the process tasks that name it, its title.
    Cached per profile text: a run asks for them once per member, the
    combined check once per entry."""
    if role is None:
        return set()
    key = (role.title, role.body)
    cached = _role_term_cache.get(key)
    if cached is None:
        text = role.title + " "
        # A section runs from its heading to the next heading of the same
        # or a higher level, whatever level the page uses (the role pages
        # moved from ## to ### on 10 September 2026).
        headings = list(_HEADING_LINE.finditer(role.body))
        for index, match in enumerate(headings):
            if match.group(2).strip() not in _ROLE_TERM_SECTIONS:
                continue
            level = len(match.group(1))
            end = next((m.start() for m in headings[index + 1:] if len(m.group(1)) <= level), len(role.body))
            text += role.body[match.end():end] + " "
        cached = frozenset(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z&-]{4,}", text)) - _ROLE_TERM_NOISE
        if len(_role_term_cache) > 256:
            _role_term_cache.clear()
        _role_term_cache[key] = cached
    return set(cached)


def _entry_flags(entry: dict[str, Any], role: RoleProfile | None) -> tuple[str, ...]:
    """Python's check that a combined entry was written from the member's
    own profile: it must name at least one of the role's own terms."""
    terms = _role_terms(role)
    if not terms:
        return ()
    text = " ".join(str(entry.get(k, "")) for k in ("view", "impact", "risks", "recommendation", "judgement")).lower()
    hits = [t for t in terms if t in text]
    return () if len(hits) >= _GENERIC_MIN_TERMS else ("generic: names none of this role's measures or tasks",)


def _parse_combined(text: str, members: tuple[str, ...]) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    """The combined answer: entries by member (raw, parsed like a single
    answer), the synthesis object and the follow-up object, if present."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}, None, None
    if not isinstance(data, dict):
        return {}, None, None
    wanted = {m.lower(): m for m in members}
    entries: dict[str, dict[str, Any]] = {}
    for item in data.get("members") or []:
        if not isinstance(item, dict):
            continue
        name = wanted.get(str(item.get("member", "")).strip().lower())
        if name is None or name in entries:
            continue
        parsed = _parse_member_dict(item)
        if parsed is not None:
            entries[name] = parsed
    synthesis = data.get("synthesis") if isinstance(data.get("synthesis"), dict) else None
    follow_up = data.get("follow_up") if isinstance(data.get("follow_up"), dict) else None
    return entries, synthesis, follow_up


def run_board_combined(
    config: dict,
    provider: AiProvider | None,
    *,
    topic: str,
    context: str = "",
    options: tuple[str, ...] = (),
    constraints: tuple[str, ...] = (),
    on_member: Callable[[str, str], None] | None = None,
    board: Board | None = None,
    member_data: dict[str, str] | None = None,
    members: tuple[str, ...] | list[str] | None = None,
    sent_notes: dict[str, str] | None = None,
    on_assessment: Callable[[MemberAssessment], None] | None = None,
    member_knowledge: dict[str, str] | None = None,
    member_notes: dict[str, dict[str, str]] | None = None,
    shared_knowledge: str = "",
    member_delta: dict[str, str] | None = None,
) -> BoardResult:
    """The combined form (decided 9 September 2026): one call writes every
    chosen member's assessment and the synthesis. Everything the single
    calls receive is in this one prompt; Python checks that every member
    came back, that each entry names its own role's terms, and every
    citation, as in ``run_board``. Cheaper by a factor of the member count;
    the entries influence each other, which the interface says."""
    start = time.monotonic()
    if provider is None or not is_configured(config, TASK_BOARD):
        raise AiNotConfiguredError("AI Board has no model configured - set provider.models.board")
    if board is None:
        board = load_board(config)
    roles = board.profiles
    if members:
        chosen = {str(m).strip().lower() for m in members}
        roles = {name: role for name, role in roles.items() if name.lower() in chosen}
        if not roles:
            raise ValueError("none of the chosen members is on the board")
    names = tuple(roles)
    if member_data is None:
        member_data = kpi_notes(config, names)
    project = active_project(config) or ""
    prompt = _combined_prompt(topic, context, options, constraints, roles, board.conduct, member_data, project,
                              member_knowledge, shared_knowledge, member_delta)

    def _notify(state: str) -> None:
        if on_member is not None:
            for member in names:
                try:
                    on_member(member, state)
                except Exception:
                    pass

    def _notify_one(member: str, state: str) -> None:
        if on_member is not None:
            try:
                on_member(member, state)
            except Exception:   # a progress display must never take a run down
                pass

    _notify("running")
    try:
        ai_result, calls = _complete_with_retry(provider, prompt)
    except CallFailed as failed_call:
        _notify("failed")
        raise failed_call.cause
    entries, synthesis_data, _follow = _parse_combined(ai_result.text, names)
    unparsed = not entries and not synthesis_data
    assessments: list[MemberAssessment] = []
    failed: list[str] = []
    for member in names:
        parsed = entries.get(member)
        if parsed is None:
            failed.append(f"{member}: " + ("the combined answer did not parse as JSON with members and synthesis"
                                           if unparsed else "no entry in the combined answer"))
            _notify_one(member, "failed")
            continue
        parsed["sources"] = tuple(verify_sources(parsed["sources"], _sent_to(member, sent_notes, member_notes, member_data)))
        parsed["flags"] = _entry_flags(parsed, roles.get(member))
        assessment = MemberAssessment(member=member, **parsed)
        assessments.append(assessment)
        if on_assessment is not None:
            try:
                on_assessment(assessment)
            except Exception:
                pass
        _notify_one(member, "done")
    if synthesis_data is not None:
        synthesis = _synthesis_text(synthesis_data)
    elif assessments:
        synthesis = "The combined answer carried no synthesis object; the member entries stand on their own."
    else:
        synthesis = "No member produced an assessment, so there is nothing to consolidate."
    result = BoardResult(
        topic=topic, assessments=assessments, synthesis=synthesis, failed_members=failed,
        ai_result=ai_result, llm_calls=calls, synthesis_data=synthesis_data,
        sources=_summarise_sources(assessments), mode="combined",
    )
    log_run(
        "board", audit_folder=_get(config, "runtime.audit_folder"), pc_name=_get(config, "storage.pc_name", ""),
        duration_seconds=time.monotonic() - start,
        counts={"assessments": len(assessments), "failed_members": len(failed), "llm_calls": calls,
                "over_token_limit": 1 if ai_result.over_token_limit else 0, "combined": 1},
        provider=ai_result.provider, model=ai_result.model, tokens=ai_result.total_tokens,
    )
    return result


def prompt_sizes(
    *, topic: str, context: str, options: tuple[str, ...], constraints: tuple[str, ...],
    roles: dict[str, RoleProfile], conduct: str, member_data: dict[str, str], project: str,
    member_knowledge: dict[str, str], mode: str = "individual", shared_knowledge: str = "",
    member_delta: dict[str, str] | None = None,
) -> list[tuple[str, int]]:
    """The prompts a run would send, as ``(label, characters)`` - built by
    the same builders the run uses, so the estimate on the confirm screen
    counts exactly what the model will read. The synthesis prompt cannot be
    built before the answers exist; its size is taken as the synthesis
    rules plus a typical answer per member."""
    sizes: list[tuple[str, int]] = []
    if mode == "combined":
        sizes.append(("board, combined", len(_combined_prompt(topic, context, options, constraints, roles, conduct,
                                                              member_data, project, member_knowledge,
                                                              shared_knowledge, member_delta))))
        return sizes
    for member, role in roles.items():
        sizes.append((member, len(_member_prompt(topic, context, options, constraints, member, role, conduct,
                                                 member_data.get(member, ""), project, member_knowledge.get(member, "")))))
    sizes.append(("synthesis", len(load_prompt("board_synthesis")) + 2400 * len(roles)))
    return sizes


def role_terms(role: RoleProfile | None) -> set[str]:
    """The words that make a role its own, for ranking its knowledge block."""
    return _role_terms(role)


def _combined_follow_up_prompt(conversation: BoardConversation, chosen: list[str], question: str) -> str:
    inputs = conversation.inputs
    roles = {m: (conversation.roles or {}).get(m) for m in chosen}
    lines = [load_prompt("board_combined"), ""]
    if conversation.project:
        lines += [f"Project: {conversation.project}.", ""]
    if conversation.conduct:
        lines += ["## Board member conduct (the same for every member)", "", conversation.conduct, ""]
    if conversation.shared_knowledge:
        lines += [conversation.shared_knowledge, ""]
    lines += ["## Members to ask again, in this order", "", ", ".join(chosen), ""]
    for member, role in roles.items():
        lines += _member_section(member, role, conversation.member_data.get(member, ""),
                                 (conversation.member_delta.get(member) if conversation.shared_knowledge
                                  else conversation.member_knowledge.get(member, "")) or "")
        earlier = next((a for a in conversation.result.assessments if a.member == member), None)
        lines += ["#### Earlier assessment of this member", ""]
        lines.append(json.dumps({"applies": earlier.applies, "view": earlier.view, "impact": earlier.impact,
                                 "risks": earlier.risks, "recommendation": earlier.recommendation}, indent=2)
                     if earlier else "(none in the first round)")
        lines.append("")
    lines += _input_block(str(inputs.get("topic", conversation.result.topic)), str(inputs.get("context", "")),
                          tuple(inputs.get("options", ())), tuple(inputs.get("constraints", ())))
    lines += ["", "## Board recommendation so far", "", conversation.result.synthesis, "", "## Conversation so far"]
    if conversation.turns:
        for q, a in conversation.turns:
            lines += [f"Q: {q}", f"A: {a}"]
    else:
        lines.append("(none yet)")
    lines += ["", "## New question", "", question]
    return "\n".join(lines)


def _follow_up_prompt(
    assessments: list[MemberAssessment], turns: list[tuple[str, str]], question: str,
    roles: dict[str, RoleProfile] | None = None,
    member_answers: list[MemberAssessment] | None = None, knowledge: str = "",
) -> str:
    lines = [_synthesis_prompt(assessments, roles), "", "## Conversation so far"]
    if turns:
        for turn_question, turn_answer in turns:
            lines.append(f"Q: {turn_question}")
            lines.append(f"A: {turn_answer}")
    else:
        lines.append("(none yet)")
    if member_answers:
        lines += ["", "## Member answers to the new question", ""]
        lines.append(json.dumps([
            {"member": a.member, "applies": a.applies, "view": a.view, "impact": a.impact, "risks": a.risks,
             "recommendation": a.recommendation,
             "facts_from_network": [{"fact": s["fact"], "source": s["source"], "verified_by_python": bool(s.get("verified"))}
                                    for s in a.sources],
             "own_judgement": a.judgement}
            for a in member_answers
        ], indent=2))
    if knowledge:
        # Pages selected for this question, not for the first one (spec 5.2).
        lines += ["", knowledge]
    lines.append("")
    lines.append("## New question")
    lines.append(question)
    return "\n".join(lines)


def _member_follow_up_prompt(conversation: BoardConversation, member: str, question: str) -> str:
    """The prompt for one member asked again: its original call, then its
    earlier assessment, the board's recommendation so far, the conversation
    and the new question (board_members.md, "Follow-up turn")."""
    inputs = conversation.inputs
    role = (conversation.roles or {}).get(member)
    lines = [_member_prompt(
        str(inputs.get("topic", conversation.result.topic)), str(inputs.get("context", "")),
        tuple(inputs.get("options", ())), tuple(inputs.get("constraints", ())),
        member, role, conversation.conduct, conversation.member_data.get(member, ""), conversation.project,
        conversation.member_knowledge.get(member, ""),
    )]
    earlier = next((a for a in conversation.result.assessments if a.member == member), None)
    lines += ["", "## Your earlier assessment", ""]
    if earlier is not None:
        lines.append(json.dumps({"applies": earlier.applies, "view": earlier.view, "impact": earlier.impact,
                                 "risks": earlier.risks, "recommendation": earlier.recommendation}, indent=2))
    else:
        lines.append("(you did not produce an assessment in the first round)")
    lines += ["", "## Board recommendation so far", "", conversation.result.synthesis, "", "## Conversation so far"]
    if conversation.turns:
        for turn_question, turn_answer in conversation.turns:
            lines.append(f"Q: {turn_question}")
            lines.append(f"A: {turn_answer}")
    else:
        lines.append("(none yet)")
    lines += ["", "## New question", "", question]
    return "\n".join(lines)


def _follow_up_text(data: dict[str, Any]) -> str:
    lines = [str(data.get("answer", "")).strip()]
    reasons = data.get("reasons") or []
    if reasons:
        lines.append("Reasons:")
        lines.extend(f"  - {item}" for item in reasons)
    if data.get("recommendation_now"):
        lines.append(f"Recommendation now: {data['recommendation_now']}")
    disagreements = data.get("disagreements") or []
    if disagreements:
        lines.append("Disagreements:")
        lines.extend(f"  - {item}" for item in disagreements)
    return "\n".join(line for line in lines if line)


def ask_follow_up_full(
    config: dict, provider: AiProvider | None, conversation: BoardConversation, question: str,
    members: tuple[str, ...] | list[str] = (), mode: str = "individual", knowledge: str = "",
) -> FollowUp:
    """One follow-up turn on a completed board run.

    With ``members`` empty this is the one-call form: the synthesis prompt
    is re-run over the original assessments and the conversation so far.
    With members named (decided 9 September 2026), each of them is asked
    again, in isolation, with its earlier assessment and the conversation
    in front of it, and the synthesis then answers over those new answers:
    one call per member plus one. With ``mode="combined"`` the named
    members and the board's answer come from one call (``board_combined.md``,
    "Follow-up turn").

    Raises ``AiNotConfiguredError`` under the same conditions ``run_board``
    does, and ``ValueError`` for a blank question."""
    if not question.strip():
        raise ValueError("a follow-up question must not be blank")
    if provider is None or not is_configured(config, TASK_BOARD):
        raise AiNotConfiguredError(
            "AI Board has no model configured - set provider.models.board"
        )
    chosen = [m for m in (conversation.roles or {}) if m.lower() in {str(x).strip().lower() for x in members}]
    member_answers: list[MemberAssessment] = []
    failed: list[str] = []
    calls = 0
    if chosen and mode == "combined":
        # One call: every chosen member's answer and the board's answer together.
        try:
            ai_result, calls = _complete_with_retry(provider, _combined_follow_up_prompt(conversation, chosen, question))
        except CallFailed as failed_call:
            conversation.llm_calls += failed_call.calls
            raise failed_call.cause
        entries, _synthesis, data = _parse_combined(ai_result.text, tuple(chosen))
        for member in chosen:
            parsed = entries.get(member)
            if parsed is None:
                failed.append(f"{member}: no entry in the combined answer")
                continue
            parsed["sources"] = tuple(verify_sources(
                parsed["sources"], _sent_to(member, conversation.sent_notes, conversation.member_notes, conversation.member_data)))
            parsed["flags"] = _entry_flags(parsed, (conversation.roles or {}).get(member))
            member_answers.append(MemberAssessment(member=member, **parsed))
        if isinstance(data, dict) and "answer" in data:
            answer = _follow_up_text(data)
        else:
            data = None
            answer = ai_result.text
        conversation.turns.append((question, answer))
        conversation.llm_calls += calls
        return FollowUp(question=question, answer=answer, data=data, assessments=member_answers,
                        failed_members=failed, llm_calls=calls)
    if chosen:
        prompts = {member: _member_follow_up_prompt(conversation, member, question) for member in chosen}
        results: dict[str, AiResult | BaseException] = {}
        with ThreadPoolExecutor(max_workers=len(chosen)) as executor:
            futures = {member: executor.submit(_complete_with_retry, provider, prompts[member]) for member in chosen}
            for member, future in futures.items():
                try:
                    result, made = future.result()
                    results[member] = result
                    calls += made
                except CallFailed as failed_call:
                    results[member] = failed_call.cause
                    calls += failed_call.calls
                except Exception as exc:
                    results[member] = exc
                    calls += 1
        for member in chosen:
            outcome = results[member]
            if isinstance(outcome, BaseException):
                failed.append(f"{member}: {outcome}")
                continue
            parsed = _parse_member_response(outcome.text)
            if parsed is None:
                failed.append(f"{member}: response did not parse as JSON with view/risks/recommendation")
                continue
            parsed["sources"] = tuple(verify_sources(
                parsed["sources"], _sent_to(member, conversation.sent_notes, conversation.member_notes, conversation.member_data)))
            member_answers.append(MemberAssessment(member=member, **parsed))

    prompt = _follow_up_prompt(conversation.result.assessments, conversation.turns, question,
                               conversation.roles, member_answers or None, knowledge)
    ai_result = provider.complete(TASK_BOARD, prompt)
    calls += 1
    try:
        data = json.loads(ai_result.text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "answer" in data:
        answer = _follow_up_text(data)
    else:
        data = None
        answer = ai_result.text
    conversation.turns.append((question, answer))
    conversation.llm_calls += calls
    return FollowUp(question=question, answer=answer, data=data, assessments=member_answers,
                    failed_members=failed, llm_calls=calls)


def ask_follow_up(
    config: dict, provider: AiProvider | None, conversation: BoardConversation, question: str,
    members: tuple[str, ...] | list[str] = (),
) -> str:
    """``ask_follow_up_full`` for callers that want the text only (the CLI)."""
    return ask_follow_up_full(config, provider, conversation, question, members).answer


def render_follow_up(question: str, answer: str) -> str:
    """FR-3.5-style plain text for one follow-up turn - no markdown, matching
    ``render``'s style."""
    return f"Q: {question}\nA: {answer}"


def render(result: BoardResult) -> str:
    """FR-3.5: one plain-text table per member, columns View / Risks /
    Recommendation, then the synthesis as prose below - produced here in
    Python from structured output, never asked of the model. Fixed-width
    label column, no markdown."""
    lines: list[str] = []
    for assessment in result.assessments:
        lines.append(assessment.member)
        lines.append("-" * len(assessment.member))
        lines.append(f"{'View':<16}{assessment.view}")
        lines.append(f"{'Risks':<16}{assessment.risks}")
        lines.append(f"{'Recommendation':<16}{assessment.recommendation}")
        if assessment.sources:
            lines.append(f"{'From the net':<16}" + "; ".join(
                f"{s['fact']} [{s['source']}{'' if s.get('verified') else ', NOT VERIFIED: ' + s.get('note', '')}]"
                for s in assessment.sources))
        if assessment.judgement:
            lines.append(f"{'Own judgement':<16}{assessment.judgement}")
        for flag in assessment.flags:
            lines.append(f"{'Check':<16}{flag}")
        lines.append("")

    if result.sources:
        lines.append("Sources")
        lines.append("-------")
        lines.append("Knowledge net notes used (verified): " + (", ".join(result.sources.get("network", [])) or "none"))
        lines.append(f"Statements from the members' own judgement: {result.sources.get('judgement_count', 0)}")
        for item in result.sources.get("unverified", []):
            lines.append(f"  NOT VERIFIED: {item}")
        lines.append("")

    if result.failed_members:
        lines.append("Failed member(s) - the rest still ran:")
        for entry in result.failed_members:
            lines.append(f"  {entry}")
        lines.append("")

    lines.append("Synthesis")
    lines.append("-" * len("Synthesis"))
    lines.append(result.synthesis)
    return "\n".join(lines)
