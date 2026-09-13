"""The board's knowledge source: an Obsidian vault read from disk (spec
section 5).

A vault is a folder of Markdown files. Nothing here knows or cares whether
Obsidian is installed - the board reads ``.md`` files under the configured
folder and nothing else. Selection is deterministic Python (AP-1): every
note is split at its headings, the sections are ranked by how many of the
question's terms - and, for a member's own block, the member's own terms -
appear in the note's title, tags, file name, the heading and the section
text, and the best-ranked sections are packed into a token budget. KPI
notes (``kind: kpi``) and the roles folder are never part of that ranking:
they are attached to their member deterministically.

The model never lists or reads files itself. It receives what this module
selected, as one text block per member (FR-3.7).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

DEFAULT_TOKEN_BUDGET = 6000
_CHARS_PER_TOKEN = 4          # a rough, deliberately conservative estimate
_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}
MAX_NOTE_BYTES = 2 * 1024 * 1024   # a note larger than this is skipped, not read into every prompt
_STOPWORDS = {
    # English
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "our", "you", "your", "can", "should",
    "would", "could", "what", "which", "when", "where", "how", "why", "into",
    "than", "then", "them", "they", "there", "these", "those", "will", "about",
    "also", "any", "all", "one", "two", "does", "did", "been", "being", "its",
    "option", "options", "board", "decide", "decision", "question", "please",
    # German
    "und", "oder", "der", "die", "das", "den", "dem", "des", "ein", "eine",
    "einen", "einem", "einer", "ist", "sind", "wir", "ihr", "sie", "nicht",
    "mit", "von", "für", "auf", "aus", "bei", "nach", "über", "auch", "wie",
    "was", "wenn", "dann", "noch", "kann", "soll", "sollen", "werden", "wird",
    "haben", "hat", "sein", "zum", "zur", "als", "sich", "uns", "dass",
}


class KnowledgeUnavailable(RuntimeError):
    """The configured vault folder cannot be read. The board does not run
    without its knowledge source once one is configured (owner decision,
    8 September 2026): the error is shown, never silently worked around."""


@dataclass(frozen=True)
class Note:
    path: Path              # absolute
    relative: str           # vault-relative, forward slashes, for display
    title: str
    tags: tuple[str, ...]
    body: str               # full file text, front matter included
    kind: str = ""          # front matter ``kind``: "kpi" marks a KPI data note
    member: tuple[str, ...] = ()   # front matter ``affected_swimlanes`` (``member`` still accepted): who the note is attached to
    projects: tuple[str, ...] = ()  # front matter ``projects``: which projects the note belongs to; empty means all
    # Decided 10 September 2026 (spec 5.1): the properties the ranking reads.
    lead: str = ""                  # ``lead_swimlane``: the role that owns the page
    summary: str = ""               # ``summary``: two lines, AI-written and marked so, for the outline and the brief tier
    aliases: tuple[str, ...] = ()   # ``aliases``: other names of the page (PPAP for Customer Part Approval)
    phases: tuple[str, ...] = ()    # ``phases``: the maturity phases a task is active in (MP1, MP2, ...)


@dataclass(frozen=True)
class Section:
    """One part of a note, split at its headings (decided 9 September 2026:
    the budget buys the sections that match the question, not whole
    pages). A short note is one section."""
    note: Note
    heading: str          # the heading line without its hashes; "" for the opening part or a whole note
    body: str             # the section's text, heading line included
    index: int = 0        # position within the note, for document order and for a unique id

    @property
    def relative(self) -> str:
        return self.note.relative


@dataclass
class KnowledgeSelection:
    vault_path: Path | None
    project: str | None = None                            # the project the notes were filtered to
    notes: list[Note] = field(default_factory=list)      # selected, in rank order
    sections: list[Section] = field(default_factory=list)   # the sections sent, in rank order
    sent: dict[str, str] = field(default_factory=dict)   # note path -> the text of it that was sent
    forced_tokens: int = 0                                # what the manual picks added on top of the budget
    total_notes: int = 0
    tokens: int = 0                                       # estimate for ``text``
    truncated: bool = False                               # a note was cut to fit
    text: str = ""                                        # the block sent to the model
    # Spec 5.1 (decided 10 September 2026): the block has three tiers. The
    # shared core every member gets, this member's own sections, and one
    # line per further page. ``core_text`` and ``own_text`` are the parts of
    # ``text``; the combined mode sends the core once for all members.
    core_text: str = ""
    own_text: str = ""
    brief_text: str = ""
    core_tokens: int = 0
    own_tokens: int = 0
    brief_tokens: int = 0
    briefs: list[Note] = field(default_factory=list)    # the pages summarised in the brief tier
    brief_sent: dict[str, str] = field(default_factory=dict)   # page -> the line sent, for citation checks
    reasons: dict[str, str] = field(default_factory=dict)   # section id -> why the model picked it (AI-assisted selection)
    picked_by: str = "python"                             # "python" or "model"
    ranked: list[Section] = field(default_factory=list)   # the sent sections in rank order (``sections`` is grouped per page)
    core_ids: set[str] = field(default_factory=set)       # ids of the sent sections that sit in the shared core
    left: list[str] = field(default_factory=list)         # spec 5.4: pages of a second pass that did not fit its budget

    @property
    def relative_paths(self) -> list[str]:
        return [note.relative for note in self.notes]


def estimate_tokens(text: str) -> int:
    return estimate_tokens_for(len(text))


def estimate_tokens_for(chars: int) -> int:
    return (chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _front_matter(text: str) -> dict[str, str | list[str]]:
    """The minimal YAML this tool needs: ``title:``, ``tags:`` and the like, either
    inline (``tags: [a, b]`` / ``tags: a, b``) or as a ``- item`` list. No
    YAML library - standard library only, and Obsidian front matter in the
    wild is rarely more than this."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip("\n")
    result: dict[str, str | list[str]] = {}
    current_key: str | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        list_item = re.match(r"^\s*-\s+(.*)$", line)
        if list_item and current_key is not None:
            items = result.setdefault(current_key, [])
            if isinstance(items, list):
                items.append(list_item.group(1).strip().strip("'\""))
            continue
        # Keys may carry spaces ("Part of Decision Board AI: true" is a
        # checkbox property in Obsidian); they are read lower-cased.
        key_value = re.match(r"^([A-Za-z_][\w -]*?):\s*(.*)$", line)
        if not key_value:
            continue
        key, value = key_value.group(1).lower(), key_value.group(2).strip()
        current_key = key
        if value == "":
            result[key] = []
        elif value.startswith("[") and value.endswith("]"):
            result[key] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        else:
            result[key] = value.strip("'\"")
    return result


def _load_note(vault: Path, path: Path) -> Note:
    body = path.read_text(encoding="utf-8-sig", errors="replace")    # -sig: a BOM must not void the front matter
    meta = _front_matter(body)
    title = meta.get("title")
    if not isinstance(title, str) or not title:
        title = path.stem
    tags_raw = meta.get("tags", [])
    if isinstance(tags_raw, str):
        tags = tuple(tag.strip().lstrip("#") for tag in tags_raw.split(",") if tag.strip())
    else:
        tags = tuple(str(tag).lstrip("#") for tag in tags_raw)
    relative = path.relative_to(vault).as_posix()
    kind = meta.get("kind") if isinstance(meta.get("kind"), str) else ""
    # ``affected_swimlanes`` is the key (decided 9 September 2026: a page says
    # which swim lanes it affects); ``member`` is read as well for old notes.
    member_raw = meta.get("affected_swimlanes", meta.get("member", ()))
    if isinstance(member_raw, str):
        members = [m.strip() for m in member_raw.split(",") if m.strip()]
    else:
        members = [str(m).strip() for m in member_raw if str(m).strip()]
    lead = meta.get("lead_swimlane")
    if isinstance(lead, str) and lead.strip() and lead.strip() not in members:
        members.insert(0, lead.strip())     # the lead is affected by definition
    members = tuple(members)
    # ``projects`` (decided 9 September 2026): a page that belongs to one or
    # more projects lists them; a page without the property is common to
    # every project (the process, the roles, the guide). ``project`` is read
    # as well for a page written with the singular.
    projects_raw = meta.get("projects", meta.get("project", ()))
    if isinstance(projects_raw, str):
        projects = tuple(p.strip() for p in projects_raw.split(",") if p.strip())
    else:
        projects = tuple(str(p).strip() for p in projects_raw if str(p).strip())
    lead_name = lead.strip() if isinstance(lead, str) else ""
    summary = meta.get("summary")
    return Note(path=path, relative=relative, title=title, tags=tags, body=body,
                kind=str(kind).lower(), member=members, projects=projects, lead=lead_name,
                summary=summary.strip() if isinstance(summary, str) else "",
                aliases=_tuple_of(meta.get("aliases")), phases=tuple(p.upper() for p in _tuple_of(meta.get("phases"))))


def _tuple_of(raw) -> tuple[str, ...]:
    """A front matter value as a tuple of strings: a list, or one string
    with commas."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return tuple(str(p).strip() for p in raw if str(p).strip())


# Notes already read, per vault: path -> (mtime_ns, size, Note). A second
# walk in the same process only stats the files and re-reads what changed,
# so "read fresh on every question" stays true at the cost of a stat per
# file instead of a read (the estimate on the confirm screen walks the
# vault on every slider move).
_VAULT_CACHE: dict[str, dict[str, tuple[int, int, Note]]] = {}
_VAULT_CACHE_LOCK = threading.Lock()


def load_vault(vault_path: Path | str, *, skip_subfolders: tuple[str, ...] = ()) -> list[Note]:
    """Every ``.md`` note under ``vault_path``, Obsidian's own folders
    skipped, plus any top-level ``skip_subfolders`` (the Roles folder: a
    member's profile is mandatory context for that member, section 3.4,
    not a note competing for the budget). Symbolic links are not followed
    and a note over ``MAX_NOTE_BYTES`` is left out: the vault is the
    boundary of what reaches a prompt. Raises ``KnowledgeUnavailable`` when
    the folder cannot be read."""
    vault = Path(vault_path)
    skip_parts = [tuple(part.lower() for part in Path(name).parts) for name in skip_subfolders if name]
    if not vault.exists():
        raise KnowledgeUnavailable(f"knowledge source not found: {vault}")
    if not vault.is_dir():
        raise KnowledgeUnavailable(f"knowledge source is not a folder: {vault}")
    key = str(vault.resolve())
    with _VAULT_CACHE_LOCK:
        cached = dict(_VAULT_CACHE.get(key, {}))
    notes: list[Note] = []
    fresh: dict[str, tuple[int, int, Note]] = {}
    try:
        for root, dirs, files in os.walk(vault, followlinks=False):
            root_path = Path(root)
            rel_parts = root_path.relative_to(vault).parts
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
                             and not (root_path / d).is_symlink())
            lowered = tuple(part.lower() for part in rel_parts)
            if any(lowered[:len(skip)] == skip for skip in skip_parts if skip):
                dirs[:] = []
                continue
            for name in sorted(files):
                if not name.lower().endswith(".md"):
                    continue
                path = root_path / name
                if path.is_symlink():
                    continue
                stat = path.stat()
                if stat.st_size > MAX_NOTE_BYTES:
                    continue
                relative = path.relative_to(vault).as_posix()
                entry = cached.get(relative)
                if entry is not None and entry[0] == stat.st_mtime_ns and entry[1] == stat.st_size:
                    note = entry[2]
                else:
                    note = _load_note(vault, path)
                fresh[relative] = (stat.st_mtime_ns, stat.st_size, note)
                notes.append(note)
    except OSError as exc:
        raise KnowledgeUnavailable(f"knowledge source could not be read: {vault} ({exc})") from exc
    with _VAULT_CACHE_LOCK:
        _VAULT_CACHE[key] = fresh
    notes.sort(key=lambda n: n.relative)
    return notes


def _project_list(project) -> list[str]:
    """One project, several as a list, or several in one string separated by
    commas or semicolons; empty means all."""
    if project is None:
        return []
    if isinstance(project, (list, tuple, set)):
        return [str(p).strip() for p in project if str(p).strip()]
    return [p.strip() for p in re.split(r"[,;]", str(project)) if p.strip()]


def for_project(notes: list[Note], project) -> list[Note]:
    """The notes that apply to the project(s): every note without a
    ``projects`` property (common to all projects) plus those that name
    one of them. No active project: every note. Names match
    case-insensitively."""
    wanted = {p.lower() for p in _project_list(project)}
    if not wanted:
        return list(notes)
    return [n for n in notes if not n.projects or any(p.lower() in wanted for p in n.projects)]


def project_names(notes: list[Note]) -> list[str]:
    """Every project the vault knows, for a project picker: the title of
    each ``kind: project`` page plus every name a ``projects`` property
    uses, sorted, deduplicated case-insensitively (first spelling wins)."""
    seen: dict[str, str] = {}
    for note in notes:
        names = [note.title] if note.kind == "project" else []
        names.extend(note.projects)
        for name in names:
            key = name.strip().lower()
            if key and key not in seen:
                seen[key] = name.strip()
    return sorted(seen.values(), key=str.lower)


def active_projects(config: dict) -> list[str]:
    """``knowledge.project`` as a list (decided 9 September 2026: a question
    may concern several projects); empty means every page."""
    return _project_list(_config_value(config, "knowledge.project"))


def active_project(config: dict) -> str | None:
    """The active project(s) as one string for a prompt line, or ``None``."""
    projects = active_projects(config)
    return ", ".join(projects) if projects else None


def list_projects(config: dict) -> list[str]:
    """``project_names`` for the configured vault; empty when no vault is
    configured or it cannot be read (a picker, not a gate)."""
    vault_path = _config_value(config, "knowledge.vault_path")
    if not vault_path:
        return []
    vault = Path(str(vault_path)).expanduser()
    try:
        notes = load_vault(vault, skip_subfolders=_roles_inside(config, vault))
    except KnowledgeUnavailable:
        return []
    return project_names(notes)


_SECTION_MIN_CHARS = 1200     # a note shorter than this is one section
_SECTION_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


def split_sections(note: Note) -> list[Section]:
    """The note split at its level-one to level-three headings. The front
    matter stays with the opening part. Each section starts with a line
    naming the note and the heading, so a section read alone still says
    where it belongs."""
    text = note.body.strip()
    if len(text) < _SECTION_MIN_CHARS:
        return [Section(note=note, heading="", body=text)]
    lines = text.splitlines()
    sections: list[Section] = []
    current: list[str] = []
    heading = ""
    content_seen = False          # anything beyond the front matter in ``current``
    in_front_matter = lines[:1] == ["---"]
    in_fence = False              # a "# line" inside a code block is code, not a heading
    for index, line in enumerate(lines):
        if in_front_matter:
            current.append(line)
            if index > 0 and line.strip() == "---":
                in_front_matter = False
            continue
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        match = None if in_fence else _SECTION_HEADING.match(line)
        if match and content_seen:
            sections.append(Section(note=note, heading=heading, body="\n".join(current).strip(), index=len(sections)))
            current, heading, content_seen = [], match.group(2).strip(), False
        elif match:
            heading = match.group(2).strip()
        current.append(line)
        if line.strip():
            content_seen = True
    if any(l.strip() for l in current):
        sections.append(Section(note=note, heading=heading, body="\n".join(current).strip(), index=len(sections)))
    return sections or [Section(note=note, heading="", body=text)]


def score_section(section: Section, terms: list[str], lowered: "_Lowered | None" = None) -> int:
    if not terms:
        return 0
    low = lowered or _Lowered.of(section)
    score = 0
    for term in terms:
        if term in low.title or term in low.name:
            score += 5
        if low.heading and term in low.heading:
            score += 4
        if term in low.tags:
            score += 5
        score += min(low.body.count(term), 5)
    return score


@dataclass(frozen=True)
class _Lowered:
    """A section's searchable text, lower-cased once: ranking runs per
    member, and lower-casing a whole vault eight times is waste."""
    title: str
    name: str
    heading: str
    tags: str
    body: str

    @staticmethod
    def of(section: Section) -> "_Lowered":
        note = section.note
        return _Lowered(note.title.lower(), note.path.stem.lower(), section.heading.lower(),
                        " ".join(note.tags).lower(), section.body.lower())


@dataclass
class _Prepared:
    """The vault split once for several selections: the sections, their
    lower-cased text and each note's mtime."""
    sections: list[Section]
    lowered: dict[int, _Lowered]
    mtimes: dict[str, float]

    @staticmethod
    def of(notes: list[Note]) -> "_Prepared":
        sections = [section for note in notes for section in split_sections(note)]
        mtimes: dict[str, float] = {}
        for note in notes:
            try:
                mtimes[note.relative] = note.path.stat().st_mtime
            except OSError:
                mtimes[note.relative] = 0.0
        return _Prepared(sections, {id(s): _Lowered.of(s) for s in sections}, mtimes)


FORCED_CAP_TOKENS = 12000     # manual picks on top of the budget stop here, whatever the slider says
CORE_SHARE = 0.4              # the shared core may take this share of the budget, the rest is the member's own
BRIEF_PAGES = 20              # one-line summaries of further pages, per member
BRIEF_CAP_TOKENS = 800        # ... within this many tokens, on top of the budget
SECTION_SHARE = 0.5           # one section may take this share of the budget; a bigger one is not sent whole unless picked by hand
_BIG_SECTION_TOKENS = 1500    # ... the rule applies to sections at least this big, so a small budget still gets its best section
_BOOST_LEAD = 6               # spec 5.1: the page's lead_swimlane is this member
_BOOST_AFFECTED = 3           # the member is among the page's affected_swimlanes
_BOOST_PHASE = 3              # the task is active in a phase the question names
_GATE_SECTION = "maturity phases and gates"    # the overview section every member gets in the core
CORE_SKIP_HEADINGS = ("Vehicle concepts", "Awarded volumes")   # spec 5.4, decision 10: project-page sections left out of the core


def core_skip_headings(config: dict | None) -> tuple[str, ...]:
    """``knowledge.core_skip_headings``: the headings of the project page
    that the core leaves out (spec 5.4, decision 10). Matched without
    regard to case, by their start, so "Awarded volumes (2026)" is skipped
    by "Awarded volumes". The default when the key is absent; an empty
    list in the config skips nothing."""
    raw = _config_value(config or {}, "knowledge.core_skip_headings")
    if raw is None:
        return CORE_SKIP_HEADINGS
    if isinstance(raw, str):
        raw = raw.split(",")
    return tuple(str(x).strip() for x in (raw if isinstance(raw, (list, tuple)) else []) if str(x).strip())


def _skipped_heading(heading: str, skip: tuple[str, ...]) -> bool:
    low = heading.strip().lower()
    return bool(low) and any(low.startswith(h.lower()) for h in skip)

# Words that name a maturity phase without its number (decided 10 September
# 2026): DV testing closes MP4, PV MP6, SOP is MP7. "MPn" and "MGn" are read
# directly; a gate closes the phase of the same number.
_PHASE_WORDS = {"pursuit": ("PURSUIT",), "dv": ("MP3", "MP4"), "pv": ("MP5", "MP6"),
                "sop": ("MP7",), "pilot": ("MP5", "MP6")}


def question_phases(text: str) -> tuple[str, ...]:
    """The maturity phases a question names, as ``MPn`` (``PURSUIT`` for the
    pursuit phase), for the phase boost and the shared core."""
    found: list[str] = []
    for match in re.finditer(r"\b(?:MP|MG)\s?(\d{1,2})\b", text, re.I):
        phase = f"MP{int(match.group(1))}"
        if phase not in found:
            found.append(phase)
    lowered = text.lower()
    for word, phases in _PHASE_WORDS.items():
        if re.search(rf"\b{word}\b", lowered):
            found.extend(p for p in phases if p not in found)
    return tuple(found)


def expand_terms(terms: list[str], question: str, notes: list[Note]) -> list[str]:
    """The question's terms plus what the vault says they mean (spec 5.1):
    a page's ``aliases`` reach its title words and back, and a row of the
    Abbreviations page reaches the full form's words and the title words
    of the pages the row links to. "PPAP" in a question then matches
    "part approval" on a page and the Customer Part Approval page itself,
    and "part approval" reaches a page whose alias is PPAP. Two-letter
    abbreviations (DV, PV) are read from the question directly, since
    ``query_terms`` drops them."""
    table: dict[str, set[str]] = {}
    title_words_of = {n.path.stem.lower(): query_terms(re.sub(r"[_\-]+", " ", n.title)) for n in notes}

    def link(key: str, words) -> None:
        key = key.strip().lower()
        if len(key) >= 2:
            table.setdefault(key, set()).update(w for w in words if len(w) >= 3)

    for note in notes:
        title_words = query_terms(re.sub(r"[_\-]+", " ", note.title))
        for alias in note.aliases:
            link(alias, title_words)
            for word in query_terms(alias):
                link(word, title_words)
            for word in title_words:
                link(word, [alias.lower()])
        if _is_abbreviations(note):
            for line in note.body.splitlines():
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))] if line.startswith("|") else []
                if len(cells) < 2 or cells[0].lower() in ("abbreviation", "") or cells[0].startswith("-"):
                    continue
                full = [w for w in query_terms(cells[1]) if w != "(?)"][:4]
                # The row's links name the pages the abbreviation stands for
                # (decided 10 September 2026: the table is the one place for
                # abbreviations, a page needs no alias of its own for them).
                linked: list[str] = []
                for target in re.findall(r"\[\[([^\]|#\\]+)", " ".join(cells[2:])):
                    linked.extend(w for w in title_words_of.get(target.strip().lower(), ()) if w not in linked)
                for abbr in cells[0].split("/"):
                    link(abbr, full + linked)
    short = [w.lower() for w in re.findall(r"\b[A-Za-z]{2}\b", question) if w.lower() in table]
    expanded = list(terms)
    for term in list(terms) + short:
        for word in sorted(table.get(term, ())):
            if word not in expanded:
                expanded.append(word)
    return expanded


def _is_abbreviations(note: Note) -> bool:
    return note.kind == "reference" and "abbreviation" in note.title.lower()


def is_meta(note: Note) -> bool:
    """A page about the vault itself, never knowledge for a decision: the
    guide (``kind: guide``) and the abbreviations page. Neither is ranked
    or summarised for a member (a manual pick still sends it). The
    abbreviations page still expands the question's terms and supplies the
    rows of the core, see ``abbreviation_rows``. Decided 10 September
    2026, when the abbreviations table, one 4,800-token section, was found
    to take four fifths of every member's budget on every question."""
    return note.kind == "guide" or _is_abbreviations(note)


_ABBREV_HEADING = "Abbreviations used in the question"
_ABBREV_ROWS = 25
_ABBREV_INDEX = 900           # a synthetic section, numbered past any real one


def abbreviation_rows(notes: list[Note], question: str) -> Section | None:
    """The rows of the abbreviations table whose abbreviation the question
    uses (``MG4`` finds ``MG``), as one small section for the shared core,
    so every member reads what the question's abbreviations mean instead
    of the whole table. None when nothing matches."""
    note = next((n for n in notes if _is_abbreviations(n)), None)
    if note is None or not question.strip():
        return None
    words: set[str] = set()
    for word in re.findall(r"[A-Za-z][\w&/-]*", question):
        words.add(word.lower())
        words.add(re.sub(r"\d+$", "", word).lower())
    header: list[str] = []
    rows: list[str] = []
    for line in note.body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(header) < 2:
            header.append(line.strip())           # the header row and the separator
            continue
        if cells and any(part.strip().lower() in words for part in cells[0].split("/")):
            rows.append(line.strip())
    if len(header) < 2 or not rows:
        return None
    body = f"## {_ABBREV_HEADING}\n\n" + "\n".join(header + rows[:_ABBREV_ROWS])
    return Section(note=note, heading=_ABBREV_HEADING, body=body, index=_ABBREV_INDEX)


def core_sections(prepared: "_Prepared", notes: list[Note], projects: list[str], phases: tuple[str, ...],
                  question: str = "", skip: tuple[str, ...] | None = None) -> list[Section]:
    """The shared core (spec 5.1): the project page(s), the section of the
    process overview that defines the phases and gates, the rows of the
    abbreviations table the question uses, and the Definition of every task
    active in a phase the question names. In that order; the packing caps
    it at ``CORE_SHARE`` of the budget. The project page's sections under
    a heading in ``skip`` (spec 5.4, decision 10: the vehicle concepts and
    the awarded volumes) are left out; they stay choosable."""
    pinned = {n.relative for n in _pinned(notes, projects)}
    skip = CORE_SKIP_HEADINGS if skip is None else skip
    core = [s for s in prepared.sections if s.relative in pinned and not _skipped_heading(s.heading, skip)]
    core += [s for s in prepared.sections if s.heading.lower() == _GATE_SECTION and s.note.kind == "process"]
    abbreviations = abbreviation_rows(notes, question)
    if abbreviations is not None:
        core.append(abbreviations)
    if phases:
        wanted = set(phases)
        core += [s for s in prepared.sections
                 if s.note.phases and (wanted & set(s.note.phases)) and s.heading.lower() == "definition"]
    return core


def _label(section: Section) -> str:
    return f"### {section.relative}" + (f" - {section.heading}" if section.heading else "")


def select_sections(
    notes: list[Note], question: str, token_budget: int = DEFAULT_TOKEN_BUDGET,
    *, extra_terms: list[str] | tuple[str, ...] = (), pinned: list[Note] | None = None,
    extra: list[str] | tuple[str, ...] = (), exclude: list[str] | tuple[str, ...] = (),
    prepared: "_Prepared | None" = None, member: str = "", phases: tuple[str, ...] | None = None,
    core: list[Section] | None = None, preferred: list[str] | tuple[str, ...] = (),
    brief_first: list[str] | tuple[str, ...] = (), reasons: dict[str, str] | None = None,
    projects: list[str] | None = None, terms: list[str] | None = None, exclusive: bool = False,
) -> KnowledgeSelection:
    """Rank every section of every note against the question - and, for a
    member's own block, against ``extra_terms`` (the member's targets,
    process tasks and title) - and pack the best into ``token_budget``.

    Spec 5.1 (decided 10 September 2026): the queue is manual picks, then
    the shared ``core`` (within ``CORE_SHARE`` of the budget), then the
    sections the model ``preferred`` in that order, then everything else by
    score. The score carries the page properties: the page's lead is this
    ``member`` (+6), the member is affected (+3), the task is active in a
    phase the question names (+3); the question's terms are expanded by
    aliases and abbreviations. A section larger than ``SECTION_SHARE`` of
    the budget is never sent whole unless picked by hand (the task table
    of the process overview took four fifths of every block before this
    rule). After the budget is spent, up to
    ``BRIEF_PAGES`` further pages go in as one line each (the page's
    ``summary``), within ``BRIEF_CAP_TOKENS`` on top. ``pinned`` notes are
    accepted for older callers and become part of the core.

    Spec 5.3 (10 September 2026): with ``exclusive`` and a non-empty
    ``preferred``, the block is what the model chose, the core and the
    manual picks, and nothing else is topped up from the ranking - Alex sees
    the choice and reads exactly that. A chosen section is never held back
    by the big-section rule; the budget alone caps it."""
    terms = list(terms) if terms is not None else expand_terms(query_terms(question), question, notes)   # once per question when the caller has them
    member_terms = [t for t in (str(x).lower().strip() for x in extra_terms) if len(t) >= 4 and t not in terms]
    forced = {str(x) for x in extra}
    banned = {str(x) for x in exclude}
    prep = prepared or _Prepared.of(notes)
    named_phases = tuple(phases) if phases is not None else question_phases(question)
    if core is None:
        core = core_sections(prep, notes, projects or [], named_phases, question)
        core = [s for s in prep.sections if s.relative in {n.relative for n in (pinned or [])}] + [s for s in core if s.relative not in {n.relative for n in (pinned or [])}]
    core_ids = {section_id(s): i for i, s in enumerate(core)}
    preferred_ids = {str(x): i for i, x in enumerate(preferred)}
    all_sections = [section for section in prep.sections
                    if section_id(section) not in banned and section.relative not in banned
                    and (not is_meta(section.note) or section_id(section) in forced or section.relative in forced)]
    all_sections += [section for section in core if section.index >= _ABBREV_INDEX      # built for this question, not in the vault
                     and section.relative not in banned and section_id(section) not in banned]
    member_key = member.strip().lower()

    def is_forced(section: Section) -> bool:
        return section_id(section) in forced or section.relative in forced

    def boosts(section: Section) -> int:
        note = section.note
        score = 0
        if member_key:
            if note.lead.lower() == member_key:
                score += _BOOST_LEAD
            elif any(m.lower() == member_key for m in note.member):
                score += _BOOST_AFFECTED
        if named_phases and note.phases and set(named_phases) & set(note.phases):
            score += _BOOST_PHASE
        return score

    def rank(section: Section) -> tuple:
        sid = section_id(section)
        if is_forced(section):
            pin, order = 0, 0
        elif sid in core_ids:
            pin, order = 1, core_ids[sid]
        elif sid in preferred_ids or section.relative in preferred_ids:
            pin, order = 2, preferred_ids.get(sid, preferred_ids.get(section.relative, 0))
        else:
            pin, order = 3, 0
        low = prep.lowered.get(id(section))
        score = score_section(section, terms, low) * 2 + score_section(section, member_terms, low) + boosts(section)
        return (pin, order, -score, -prep.mtimes.get(section.relative, 0.0), section.relative, section.index)

    ranked = sorted(all_sections, key=rank)
    selection = KnowledgeSelection(vault_path=None, total_notes=len(notes), reasons=dict(reasons or {}),
                                   picked_by="model" if preferred else "python")
    if not notes:
        return selection
    remaining = token_budget - estimate_tokens("## Knowledge from the vault\n\n")
    core_cap = int(token_budget * CORE_SHARE)
    chosen: list[tuple[Section, str, str]] = []      # section, chunk, tier ("core" or "own")
    core_used = 0
    cut_once = False
    skipped_big: Section | None = None       # the best section too big for its share, cut in when nothing else fills the block
    for section in ranked:
        label = _label(section)
        chunk = f"{label}\n{section.body}\n\n"
        cost = estimate_tokens(chunk)
        sid = section_id(section)
        if is_forced(section):
            if selection.forced_tokens + cost > FORCED_CAP_TOKENS:
                selection.truncated = True      # the picks alone would overflow the model; the rest is dropped
                continue
            chosen.append((section, chunk, "own"))    # a manual pick is sent whole, on top of the budget
            selection.forced_tokens += cost
            continue
        if sid in core_ids:
            if core_used + cost > core_cap and chosen:
                continue          # the core may not eat the member's share
            if cost <= remaining:
                chosen.append((section, chunk, "core"))
                remaining -= cost
                core_used += cost
            continue
        chosen_by_model = sid in preferred_ids or section.relative in preferred_ids
        if exclusive and preferred_ids and not chosen_by_model:
            continue          # spec 5.3: the model's choice is the block; the rest may still go in as one line
        if not chosen_by_model and cost > _BIG_SECTION_TOKENS and cost > token_budget * SECTION_SHARE:
            if skipped_big is None:
                skipped_big = section
            continue          # one table must not be the whole block; the page's summary still reaches the member
        if cost <= remaining:
            chosen.append((section, chunk, "own"))
            remaining -= cost
            continue
        if remaining < 50:
            break             # the budget is spent
        room_chars = remaining * _CHARS_PER_TOKEN - len(label) - 40
        if room_chars > 200 and not cut_once:
            # This section does not fit whole: cut it once to what is left,
            # then keep looking for smaller sections that still fit.
            cut = section.body[:room_chars]
            chosen.append((section, f"{label}\n{cut}\n[... cut to fit the token budget]\n\n", "own"))
            selection.truncated = True
            cut_once = True
            remaining = 0
            break
        continue              # too big: a smaller, lower-ranked section may still fit
    if skipped_big is not None and not cut_once and not any(tier == "own" for _s, _c, tier in chosen):
        # Nothing else filled the member's share: the best big section is
        # cut to what is left rather than sending an empty block.
        label = _label(skipped_big)
        room_chars = remaining * _CHARS_PER_TOKEN - len(label) - 40
        if room_chars > 200:
            chosen.append((skipped_big, f"{label}\n{skipped_big.body[:room_chars]}\n[... cut to fit the token budget]\n\n", "own"))
            selection.truncated = True
            remaining = 0
    # Sections of one note stay together, in the note's own order, under the
    # note's first appearance in the ranking - per tier: a page's Definition
    # in the core does not pull its Coaching into the core.
    selection.ranked = [section for section, _c, _t in chosen]
    selection.core_ids = {section_id(section) for section, _c, tier in chosen if tier == "core"}
    by_note: dict[tuple[str, str], list[tuple[Section, str]]] = {}
    order: dict[str, list[str]] = {"core": [], "own": []}
    for section, chunk, tier in chosen:
        key = (tier, section.relative)
        if key not in by_note:
            by_note[key] = []
            order[tier].append(section.relative)
        by_note[key].append((section, chunk))
    parts: dict[str, list[str]] = {"core": [], "own": []}
    for tier in ("core", "own"):
        for relative in order[tier]:
            items = sorted(by_note[(tier, relative)], key=lambda item: item[0].index)
            parts[tier].append("".join(chunk for _s, chunk in items))
            if items[0][0].note not in selection.notes:
                selection.notes.append(items[0][0].note)
            selection.sections.extend(section for section, _c in items)
            selection.sent[relative] = selection.sent.get(relative, "") + "".join(section.body + "\n" for section, _c in items)
    sent_pages = {relative for _tier, relative in by_note}
    # The brief tier: one line per further page, the model's brief picks first.
    brief_order: list[Note] = []
    wanted_first = [str(x).split("#", 1)[0] for x in brief_first]
    for relative in wanted_first:
        note = next((s.note for s in all_sections if s.relative == relative), None)
        if note is not None and relative not in sent_pages and note not in brief_order:
            brief_order.append(note)
    for section in ranked:
        if section.relative in sent_pages or section.note in brief_order or section.note.kind == "project" or is_meta(section.note):
            continue
        brief_order.append(section.note)
    brief_lines: list[str] = []
    brief_used = 0
    for note in brief_order[:BRIEF_PAGES]:
        headings = [s.heading for s in split_sections(note) if s.heading][:4]
        line = f"- {note.relative}: " + (note.summary or ("sections: " + ", ".join(headings) if headings else "no summary yet"))
        cost = estimate_tokens(line + "\n")
        if brief_used + cost > BRIEF_CAP_TOKENS:
            break
        brief_lines.append(line)
        brief_used += cost
        selection.briefs.append(note)
        selection.brief_sent[note.relative] = note.summary or line
    core_body = "".join(parts["core"]).rstrip()
    own_body = "".join(parts["own"]).rstrip()
    selection.core_text = ("## Knowledge from the vault, shared by every member\n\n" + core_body) if core_body else ""
    selection.own_text = ("## Knowledge selected for this member\n\n" + own_body) if own_body else ""
    selection.brief_text = ("## Further pages in the vault, one line each\n\nThese pages were not sent in full. "
                            "Say so when you rely on one of them.\n\n" + "\n".join(brief_lines)) if brief_lines else ""
    selection.core_tokens = estimate_tokens(selection.core_text)
    selection.own_tokens = estimate_tokens(selection.own_text)
    selection.brief_tokens = estimate_tokens(selection.brief_text)
    blocks = [b for b in (core_body, own_body) if b]
    text = "## Knowledge from the vault\n\n" + "\n\n".join(blocks) if blocks else ""
    if selection.brief_text:
        text = (text + "\n\n" if text else "") + selection.brief_text
    selection.text = text
    selection.tokens = estimate_tokens("## Knowledge from the vault\n\n" + "\n\n".join(blocks)) if blocks else 0   # the budgeted tiers; briefs ride on top
    return selection


def query_terms(question: str) -> list[str]:
    words = re.findall(r"[\w][\w'-]{2,}", question.lower())
    seen: list[str] = []
    for word in words:
        if word in _STOPWORDS or word.isdigit() or word in seen:
            continue
        seen.append(word)
    return seen


def select_notes(
    notes: list[Note], question: str, token_budget: int = DEFAULT_TOKEN_BUDGET
) -> KnowledgeSelection:
    """The selection for one question with no member in view: see
    ``select_sections``."""
    return select_sections(notes, question, token_budget)


def _knowledge_notes(config: dict, projects=None, *, include_roles: bool = False) -> tuple[Path | None, list[str], list[Note]]:
    """The notes a selection may draw on. The roles folder is left out for
    the board (section 3.4: a profile is a member's mandatory context, not a
    note competing for the budget) and read in for Ask the vault and the
    table of contents (spec 5.3): "who is responsible" is answered there."""
    vault_path = _config_value(config, "knowledge.vault_path")
    if not vault_path:
        return None, [], []
    vault = Path(str(vault_path)).expanduser()
    chosen = _project_list(projects) if projects is not None else active_projects(config)
    skip = () if include_roles else _roles_inside(config, vault)
    notes = [n for n in for_project(load_vault(vault, skip_subfolders=skip), chosen) if n.kind != "kpi"]
    return vault, chosen, notes


def _pinned(notes: list[Note], projects: list[str]) -> list[Note]:
    """The project page(s): the common core every member receives."""
    wanted = {p.lower() for p in projects}
    if not wanted:
        return []
    return [n for n in notes if n.kind == "project"
            and (n.title.strip().lower() in wanted or any(p.lower() in wanted for p in n.projects))]


def section_id(section: Section) -> str:
    """``path#heading`` for a section, the path alone for a whole note; a
    repeated heading gets its position appended so the ids stay unique."""
    if not section.heading:
        return section.relative if section.index == 0 else f"{section.relative}#{section.index}"
    same = [s for s in split_sections(section.note) if s.heading == section.heading]
    if len(same) > 1 and same[0].index != section.index:
        return f"{section.relative}#{section.heading}#{section.index}"
    return f"{section.relative}#{section.heading}"


def outline(config: dict, projects=None) -> list[dict]:
    """Every note and its sections, with ids and token sizes, for the
    manual picks under the estimate (decided 9 September 2026)."""
    vault, _chosen, notes = _knowledge_notes(config, projects)
    if vault is None:
        return []
    result = []
    for note in notes:
        sections = split_sections(note)
        result.append({"path": note.relative, "title": note.title, "kind": note.kind, "summary": note.summary,
                       "sections": [{"id": section_id(s),
                                     "heading": s.heading or ("(whole note)" if len(sections) == 1 else "(opening)"),
                                     "tokens": estimate_tokens(s.body)} for s in sections]})
    return result


CONTENTS_LIMIT_TOKENS = 40000     # ``knowledge.contents_limit_tokens``: above this the table of contents is trimmed (spec 5.3)


def contents(config: dict, projects=None, *, question: str = "", limit_tokens: int | None = None,
             include_roles: bool = True) -> dict[str, Any]:
    """The table of contents of the vault (spec 5.3): every page of the
    chosen project(s) as one entry with its title, properties and summary,
    and its sections with their ids, headings and sizes. Nothing ranked,
    nothing left out - unless the whole list would exceed ``limit_tokens``,
    in which case the pages the word ranking puts first for ``question``
    are kept and ``trimmed`` says so."""
    vault, chosen, notes = _knowledge_notes(config, projects, include_roles=include_roles)
    if vault is None:
        return {"pages": [], "tokens": 0, "trimmed": False, "total_pages": 0}
    limit = int(limit_tokens or _config_value(config, "knowledge.contents_limit_tokens") or CONTENTS_LIMIT_TOKENS)
    pages: list[dict[str, Any]] = []
    for note in notes:
        sections = split_sections(note)
        pages.append({
            "path": note.relative, "title": note.title, "kind": note.kind, "lead": note.lead,
            "affected": list(note.member), "phases": list(note.phases), "aliases": list(note.aliases),
            "summary": note.summary, "tokens": estimate_tokens(note.body),
            "sections": [{"id": section_id(sec),
                          "heading": sec.heading or ("(whole page)" if len(sections) == 1 else "(opening)"),
                          "tokens": estimate_tokens(sec.body)} for sec in sections],
        })
    total = len(pages)
    size = estimate_tokens("\n".join(contents_lines(pages)))
    trimmed = False
    if size > limit and question.strip():
        # The valve: keep the pages the ranking puts first, until the list fits.
        prepared = _Prepared.of(notes)
        ranked = select_sections(notes, question, 10 ** 7, prepared=prepared, projects=chosen).ranked
        order = {sec.relative: i for i, sec in enumerate(ranked) if sec.relative not in {}}
        pages.sort(key=lambda page: order.get(page["path"], len(order)))
        kept: list[dict[str, Any]] = []
        used = 0
        for page in pages:
            cost = estimate_tokens("\n".join(contents_lines([page])))
            if used + cost > limit and kept:
                break
            kept.append(page)
            used += cost
        pages = sorted(kept, key=lambda page: page["path"])
        size, trimmed = used, True
    return {"pages": pages, "tokens": size, "trimmed": trimmed, "total_pages": total}


def contents_lines(pages: list[dict[str, Any]]) -> list[str]:
    """The table of contents as the model reads it: one line per page with
    what the page holds, then one ``- id:`` line per section."""
    lines: list[str] = []
    for page in pages:
        props = [f"kind: {page['kind']}" if page.get("kind") else "",
                 f"lead: {page['lead']}" if page.get("lead") else "",
                 f"affected: {', '.join(page['affected'])}" if page.get("affected") else "",
                 f"phases: {', '.join(page['phases'])}" if page.get("phases") else "",
                 f"aliases: {', '.join(page['aliases'])}" if page.get("aliases") else ""]
        head = f"- page: {page['path']} | {page['title']}" + "".join(f" | {p}" for p in props if p) + f" | {page['tokens']} tokens"
        lines.append(head)
        if page.get("summary"):
            lines.append(f"  summary: {page['summary']}")
        for sec in page.get("sections") or []:
            lines.append(f"- id: {sec['id']} | {page['path']} - {sec['heading']} | {sec['tokens']} tokens")
    return lines


def expand_rules(rules: list[dict[str, Any]], pages: list[dict[str, Any]]) -> list[str]:
    """The pages a rule names (spec 5.3, decision 3): every page whose
    properties carry every value the rule gives, compared without case.
    ``kind``, ``lead``, ``affected``, ``phases`` and ``aliases`` may be
    named; a rule with no known key matches nothing."""
    keys = ("kind", "lead", "affected", "phases", "aliases")
    found: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        wanted = {k: str(v).strip().lower() for k, v in rule.items()
                  if k in keys and str(v).strip()}
        if not wanted:
            continue
        for page in pages:
            match = True
            for key, value in wanted.items():
                have = page.get(key)
                haves = [str(h).lower() for h in (have if isinstance(have, list) else [have or ""])]
                if value not in haves:
                    match = False
                    break
            if match and page["path"] not in found:
                found.append(page["path"])
    return found


def gather(config: dict, question: str, *, token_budget: int | None = None, projects=None) -> KnowledgeSelection:
    """The knowledge block for ``question`` under the configured source.

    No source configured: an empty selection, the board runs on the
    question alone. A source configured but unreadable: ``KnowledgeUnavailable``."""
    vault, chosen, notes = _knowledge_notes(config, projects)
    if vault is None:
        return KnowledgeSelection(vault_path=None)
    budget = token_budget or _config_value(config, "knowledge.token_budget") or DEFAULT_TOKEN_BUDGET
    selection = select_sections(notes, question, int(budget), pinned=_pinned(notes, chosen), projects=chosen)
    selection.vault_path = vault
    selection.project = ", ".join(chosen) if chosen else None
    return selection


def gather_for_members(
    config: dict, question: str, member_terms: dict[str, list[str] | tuple[str, ...] | set[str]],
    *, token_budget: int | None = None, projects=None,
    extra: list[str] | tuple[str, ...] = (), exclude: list[str] | tuple[str, ...] = (),
    picks: dict[str, dict[str, Any]] | None = None, include_roles: bool = False,
) -> dict[str, KnowledgeSelection]:
    """One knowledge block per member (decided 9 September 2026): the
    sections ranked by the question and by the member's own terms, so each
    member receives what concerns it, the shared core first for all (spec
    5.1). ``extra`` section ids are sent to every member on top of the
    budget; ``exclude`` ids are never sent. ``picks`` is the model's
    choice per member (``{"full": [ids], "brief": [ids], "reasons": {id:
    why}}``, ``picker.pick``): its full picks lead the queue, its brief
    picks lead the brief tier; ``exclusive`` (default: a non-empty
    ``full``) makes them the whole block (spec 5.3). The vault is read
    once. No source configured: empty selections."""
    vault, chosen, notes = _knowledge_notes(config, projects, include_roles=include_roles)
    budget = int(token_budget or _config_value(config, "knowledge.token_budget") or DEFAULT_TOKEN_BUDGET)
    result: dict[str, KnowledgeSelection] = {}
    prepared = _Prepared.of(notes) if vault is not None else None      # split and lower-case the vault once
    phases = question_phases(question)
    core = core_sections(prepared, notes, chosen, phases, question, core_skip_headings(config)) if prepared is not None else []
    expanded = expand_terms(query_terms(question), question, notes) if prepared is not None else []
    for member, terms in member_terms.items():
        if vault is None:
            result[member] = KnowledgeSelection(vault_path=None)
            continue
        pick = (picks or {}).get(member) or {}
        selection = select_sections(notes, question, budget, extra_terms=tuple(terms), extra=extra, exclude=exclude,
                                    prepared=prepared, member=member, phases=phases, core=core, projects=chosen, terms=expanded,
                                    preferred=tuple(pick.get("full") or ()), brief_first=tuple(pick.get("brief") or ()),
                                    reasons=dict(pick.get("reasons") or {}),
                                    exclusive=bool(pick.get("exclusive", bool(pick.get("full")))))
        selection.vault_path = vault
        selection.project = ", ".join(chosen) if chosen else None
        result[member] = selection
    return result


MAX_READ_TOKENS = 120000      # ``knowledge.max_read_tokens``: the ceiling of one call (spec 5.5, decision 1; 5.9)
# What a call carries besides the pages (spec 5.9, decision 2), measured on
# 13 September 2026 against a vault of 95,000 tokens: a member's profile,
# the conduct note and its KPI block about 3,000, six of them in the
# combined form about 5,500, and the gateway's own overhead about 6,300.
READ_RESERVE_TOKENS = 15000


def page_of(key: str) -> str:
    """The page a pick stands for: a section id ``path#heading`` reads its
    whole page (spec 5.5, decision 2)."""
    return str(key).split("#", 1)[0]


def gather_whole(config: dict, question: str, paths: list[str], *, ceiling: int | None = None, projects=None,
                 exclude: list[str] | tuple[str, ...] = (), include_roles: bool = True) -> KnowledgeSelection:
    """The block of Ask the vault (spec 5.5): the shared core, then the
    named pages, whole, in the order given, within ``ceiling`` - no
    ranking, no one-line tier, no budget. A page that does not fit whole
    stays out and is listed in ``left``; a name that is not a page of the
    vault is ignored here (the caller resolved the names); a page in
    ``exclude`` is never sent. No source configured: an empty selection."""
    vault, chosen, notes = _knowledge_notes(config, projects, include_roles=include_roles)
    selection = KnowledgeSelection(vault_path=vault, total_notes=len(notes), picked_by="model")
    if vault is None:
        return selection
    limit = int(ceiling or _config_value(config, "ask.max_read_tokens") or MAX_READ_TOKENS)
    selection.project = ", ".join(chosen) if chosen else None
    banned = {page_of(x) for x in exclude}
    prepared = _Prepared.of(notes)
    core = [sec for sec in core_sections(prepared, notes, chosen, question_phases(question), question, core_skip_headings(config))
            if sec.relative not in banned]
    remaining = limit - estimate_tokens("## Knowledge from the vault\n\n")
    core_parts: list[str] = []
    for sec in core:
        chunk = f"{_label(sec)}\n{sec.body}\n\n"
        cost = estimate_tokens(chunk)
        if cost > remaining:
            continue
        remaining -= cost
        core_parts.append(chunk)
        selection.sections.append(sec)
        selection.core_ids.add(section_id(sec))
        if sec.note not in selection.notes and sec.index < _ABBREV_INDEX:
            selection.notes.append(sec.note)
        selection.sent[sec.relative] = selection.sent.get(sec.relative, "") + sec.body + "\n"
    core_pages = {sec.relative for sec in core if sec.index < _ABBREV_INDEX and section_id(sec) in selection.core_ids}
    by_path = {note.relative: note for note in notes}
    parts: list[str] = []
    seen: set[str] = set()
    for key in paths:
        path = page_of(key)
        note = by_path.get(path)
        if note is None or path in seen or path in banned:
            continue
        seen.add(path)
        sections = [sec for sec in split_sections(note) if section_id(sec) not in selection.core_ids]
        if path in core_pages and not sections:
            continue                                    # the core already carries the whole page
        chunks = [f"{_label(sec)}\n{sec.body}\n\n" for sec in sections]
        cost = estimate_tokens("".join(chunks))
        if cost > remaining:
            selection.left.append(path)
            continue
        remaining -= cost
        parts.append("".join(chunks))
        if note not in selection.notes:
            selection.notes.append(note)
        selection.sections.extend(sections)
        selection.sent[path] = selection.sent.get(path, "") + "".join(sec.body + "\n" for sec in sections)
    core_body = "".join(core_parts).rstrip()
    own_body = "".join(parts).rstrip()
    selection.core_text = ("## Knowledge from the vault, the shared core\n\n" + core_body) if core_body else ""
    selection.own_text = ("## Pages read for this question\n\n" + own_body) if own_body else ""
    blocks = [b for b in (selection.core_text, selection.own_text) if b]
    selection.text = ("## Knowledge from the vault\n\n" + "\n\n".join(blocks)) if blocks else ""
    selection.core_tokens = estimate_tokens(selection.core_text)
    selection.own_tokens = estimate_tokens(selection.own_text)
    selection.tokens = estimate_tokens(selection.text)
    return selection


def resolve_page_names(config: dict, names: list[str], projects=None, *, include_roles: bool = True) -> tuple[list[str], int]:
    """The vault paths the model's page names stand for (spec 5.4,
    decision 7): a path as listed, or a file name that is unique in the
    vault, with or without ``.md``; anything else is dropped and counted."""
    vault, _chosen, notes = _knowledge_notes(config, projects, include_roles=include_roles)
    if vault is None:
        return [], len(names)

    def key(path: str) -> str:
        low = path.replace("\\", "/").strip().strip("[]").lower()
        return low[:-3] if low.endswith(".md") else low

    by_key: dict[str, str] = {}
    by_name: dict[str, list[str]] = {}
    for note in notes:
        k = key(note.relative)
        by_key[k] = note.relative
        by_name.setdefault(k.rsplit("/", 1)[-1], []).append(note.relative)
    found: list[str] = []
    dropped = 0
    for name in names:
        k = key(str(name))
        path = by_key.get(k)
        if path is None:
            same = by_name.get(k.rsplit("/", 1)[-1]) or []
            path = same[0] if len(same) == 1 else None
        if path is None:
            dropped += 1
        elif path not in found:
            found.append(path)
    return found, dropped


CANDIDATES_PER_MEMBER = 40


def candidates(config: dict, question: str, member_terms: dict[str, list[str] | tuple[str, ...] | set[str]],
               *, projects=None, limit: int = CANDIDATES_PER_MEMBER) -> dict[str, list[dict[str, Any]]]:
    """The Python first cut for the AI-assisted pick (spec 5.1): per member
    the ``limit`` best-ranked sections with id, page, heading, the page's
    summary and size. The model chooses from these and from nothing else."""
    vault, chosen, notes = _knowledge_notes(config, projects)
    if vault is None:
        return {m: [] for m in member_terms}
    prepared = _Prepared.of(notes)
    phases = question_phases(question)
    core = core_sections(prepared, notes, chosen, phases, question, core_skip_headings(config))
    core_ids = {section_id(s) for s in core}
    expanded = expand_terms(query_terms(question), question, notes)
    result: dict[str, list[dict[str, Any]]] = {}
    for member, terms in member_terms.items():
        # A very large budget: the ranking decides, the packing takes everything that fits.
        selection = select_sections(notes, question, 10 ** 7, extra_terms=tuple(terms), prepared=prepared,
                                    member=member, phases=phases, core=core, projects=chosen, terms=expanded)
        rows = []
        for section in selection.ranked:          # rank order, not the per-page order of ``sections``
            sid = section_id(section)
            if sid in core_ids:
                continue          # the core goes to every member anyway
            rows.append({"id": sid, "path": section.relative, "heading": section.heading,
                         "summary": section.note.summary, "tokens": estimate_tokens(section.body)})
            if len(rows) >= limit:
                break
        result[member] = rows
    return result


KPI_TOKEN_CAP = 2500       # per member; a KPI note is a table, not a chapter
KPI_STALE_DAYS = 30        # beyond this the age is called out, not just stated


def _updated_on(note: "Note") -> date | None:
    meta = _front_matter(note.body)
    raw = meta.get("updated")
    if not isinstance(raw, str):
        return None
    for shape in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw.strip(), shape).date()
        except ValueError:
            continue
    return None


def _freshness(note: "Note", today: date, stale_days: int) -> str:
    """One line above a KPI note saying how old it is. Python computes the
    age (AP-1) so a member can never quote a number without its date."""
    updated = _updated_on(note)
    if updated is None:
        return ("Last updated: not recorded in this note. Treat every value in it as an "
                "assumption and say so.")
    age = (today - updated).days
    if age < 0:
        return (f"Last updated {updated.isoformat()}, which is in the future: check the date before "
                "you rely on a value from it.")
    when = f"Last updated {updated.isoformat()}, {age} day(s) ago."
    if age > stale_days:
        return (f"{when} **This is older than {stale_days} days: say so before you rely on a value "
                f"from it, and name what would have to be re-checked.**")
    return when


def kpi_notes(config: dict, members: list[str] | tuple[str, ...], *, today: date | None = None, projects=None) -> dict[str, str]:
    """The KPI data block for each member (spec 3.4, decided 9 September
    2026): every note in the vault whose front matter says ``kind: kpi`` and
    lists the member under ``affected_swimlanes`` is attached to that
    member's call, always, whatever the question - the role says which KPI,
    the network holds the number. With ``knowledge.project`` set, only the
    notes of that project (and notes without a ``projects`` property) count,
    so a second project's gates never reach this project's board.
    Members without a note get no block; the role profile tells them to say
    the target is not in the network yet. Raises ``KnowledgeUnavailable`` as
    ``gather`` does."""
    vault_path = _config_value(config, "knowledge.vault_path")
    if not vault_path:
        return {}
    vault = Path(str(vault_path)).expanduser()
    notes = [n for n in for_project(load_vault(vault, skip_subfolders=_roles_inside(config, vault)),
                                    _project_list(projects) if projects is not None else active_projects(config))
             if n.kind == "kpi"]
    wanted = {m.lower(): m for m in members}
    stale_days = int(_config_value(config, "knowledge.kpi_stale_days") or KPI_STALE_DAYS)
    day = today or date.today()
    blocks: dict[str, list[str]] = {}
    for note in notes:
        for named in note.member:
            member = wanted.get(named.lower())
            if member is None:
                continue
            chunk = f"### {note.relative}\n{_freshness(note, day, stale_days)}\n\n{note.body.strip()}"
            blocks.setdefault(member, []).append(chunk)
    result: dict[str, str] = {}
    limit = KPI_TOKEN_CAP * _CHARS_PER_TOKEN
    for member, chunks in blocks.items():
        # Whole notes only: a KPI table cut mid-row, or a note without its
        # freshness line, is exactly the "number without its date" this
        # module exists to prevent.
        kept: list[str] = []
        used = 0
        for chunk in chunks:
            if used + len(chunk) > limit and kept:
                break
            kept.append(chunk[:limit] if len(chunk) > limit else chunk)
            used += len(chunk)
        omitted = len(chunks) - len(kept)
        text = "\n\n".join(kept)
        if omitted:
            text += f"\n\n[{omitted} further KPI note(s) omitted: over the KPI budget]"
        result[member] = text
    return result


def detect_roles_folder(vault: Path) -> Path | None:
    """A roles folder inside the vault by name: ``Roles``,
    ``Roles&Responsibilities``, ``Roles & Responsibilities``, ``R&R``..."""
    if not vault.is_dir():
        return None
    try:
        for child in sorted(vault.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            name = child.name.lower().replace(" ", "")
            if name.startswith("role") or name in ("r&r", "randr", "rnr"):
                return child
    except OSError:
        return None
    return None


def resolve_roles_folder(config: dict) -> tuple[Path | None, str]:
    """Where the role profiles come from and why: the configured folder,
    else a roles folder detected in the vault, else nothing. It lives here
    rather than with the board (10 September 2026): every reader of the
    vault has to know which folder to leave out, and a module every agent
    uses must not depend on one of them."""
    configured = _config_value(config, "knowledge.roles_folder")
    if configured:
        return Path(str(configured)).expanduser(), "configured"
    vault = _config_value(config, "knowledge.vault_path")
    if vault:
        detected = detect_roles_folder(Path(str(vault)).expanduser())
        if detected is not None:
            return detected, "vault"
    return None, "none"


DEFAULT_ROLES_SUBFOLDER = "Roles"     # the folder the wizard offers to create inside the vault


def _roles_inside(config: dict, vault: Path) -> tuple[str, ...]:
    """The roles folder as a vault-relative path when it lies inside the
    vault (section 3.4: profiles are not knowledge notes), else nothing."""
    folder, _ = resolve_roles_folder(config)
    if folder is None:
        return ()
    try:
        return (folder.resolve().relative_to(vault.resolve()).as_posix(),)
    except (ValueError, OSError):
        return ()


def vault_outline(vault_path: Path | str, *, max_folders: int = 200, max_titles: int = 400) -> str:
    """A compact description of the vault for the memory proposal call
    (``memory_writer``): its folders and note titles, capped so a large
    vault does not become the prompt."""
    notes = load_vault(vault_path)
    folders = sorted({str(Path(note.relative).parent.as_posix()) for note in notes} - {"."})
    lines = ["Folders:"]
    lines.extend(f"- {folder}/" for folder in folders[:max_folders])
    if len(folders) > max_folders:
        lines.append(f"- ... and {len(folders) - max_folders} more")
    lines.append("")
    lines.append("Existing notes (path: title):")
    for note in notes[:max_titles]:
        lines.append(f"- {note.relative}: {note.title}")
    if len(notes) > max_titles:
        lines.append(f"- ... and {len(notes) - max_titles} more")
    return "\n".join(lines)


def _config_value(config: dict, dotted: str):
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if node not in ("", None) else None
