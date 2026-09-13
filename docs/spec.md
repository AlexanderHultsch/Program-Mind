# Program Mind — specification

**What this file is.** Every decision this program rests on, with the date
it was taken and the reason behind it. It is written in the order the
decisions were taken, not in the order a reader would meet them, because
the record is the point: a decision that was later withdrawn stays here,
marked, so that nobody takes it again for the same wrong reason.

**How to read it.** "The program as it stands" below says what happens
today and names the section that decided it; follow the one you need. A
subsection that has been overtaken says so in its first lines and names
what overtook it. What follows such a line is history, and still worth
reading for the reason.

**The rules of the file.** English, plain, short sentences (section 7). A
decision keeps its date. A decision that no longer holds is marked, never
deleted. Nothing here is a wish: what is written as built is built, and
what is not yet built says so.

## The program as it stands, 13 September 2026

| What happens | Where it was decided |
|---|---|
| Two agents on one Obsidian vault: the **Board**, where every member answers a decision alone and one call consolidates them, and **Ask the vault**, one agent answering any question in a thread. They sit in a shell with a home page, a menu and an archive. | Sections 3, 10, 11 |
| **Every question reads the whole vault**: every page of the chosen project, whole, for Ask the vault and for every board member alike, as long as the whole fits one read (`knowledge.max_read_tokens`, 120,000). Nothing is ranked and no call is spent choosing while it fits. | 5.6, 5.8 |
| **The pages sit under the question box**, one folded line with a tick per page. Untick one and it is left out of that read. Asking reads and answers in one step. | 5.7 |
| **The answer appears as the model writes it**, through OpenCode's server mode; on the board, each member writes in its own card. What is streamed is for the eye: the answer, its sources and every check come from the finished call. | 4.1, 5.8 |
| **Sources and gaps.** Every answer names the pages it used, and Python drops a source that was never sent. A gap says "Not in the vault" when the whole vault was read, and "Not in the pages read" when something was left out. | 10.1, 5.7 |
| **Nothing is written into the vault** without a yes: the memory note at the close of a topic or a thread is proposed, shown in full, and written only after Alex confirms. | 5, AP-4 |
| **The work is kept** as one JSON file per board topic and per thread, beside the configuration, never in the vault, with the paths of the pages sent but never their text. | 11.1, decision 9 |
| **When a vault outgrows one read** the older machinery takes over, and only then: the model ranks the pages from the table of contents, the read fills to the ceiling, a check after the answer swaps in what was left, and the board falls back to its per-member ranking with the slider and the selection dropdown. | 5.1, 5.3, 5.5 |

## 1. Purpose and scope

Program Mind (Decision Board until 10 September 2026, see section 11) is a
local site that hosts AI agents on an Obsidian vault. Two agents run on it
today. The **board** (section 3) takes a decision: Alex poses a question — a
topic, its context, the options under consideration and any hard
constraints — and the board's members — whoever has a role profile in the
roles folder, section 3.4 — answer it from their professional perspectives,
each in a model call that cannot see any other member's answer; one more
call consolidates them into one recommendation. **Ask the vault**
(section 10) answers any question from the same vault, in a thread that
stays open until it is closed. The board has a second front end on the
command line; everything else lives in the browser interface (section 9).

The tool does one thing: run this board and hold the follow-up conversation
that follows it, for the length of one topic. It has no database, no
scheduler, and no access of its own to Jira, SharePoint, Outlook or Teams.
Its one source of knowledge is a folder of Markdown notes Alex chooses — an
Obsidian vault (section 5) — which the board reads for every question and
writes to only after Alex has confirmed a note. Anything else the board
should reason about — an OIL item, a Jira status, a cost figure — is
supplied by Alex as free text.

## 2. Principles

Carried over from the source project's architecture principles (Program Lead
Cockpit spec 3.2), reworded for a tool that is a single command with no
persistent state:

| ID | Principle |
|---|---|
| AP-1 | **Deterministic before AI.** Assembling the six prompts, parsing each member's JSON response, ordering assessments back into a fixed member order, and rendering the tables and synthesis prose are all Python. The model is used only for the language understanding, per-member assessment and synthesis text themselves. |
| AP-3 | **Batch.** One invocation of the `board` command is one run covering the whole topic — options, constraints and all six perspectives — never a separate invocation per member. FR-3.3a (3.2) is a deliberate, narrowly-scoped exception to this principle: see below. |
| AP-4 | **Propose, do not execute.** The board produces an answer and, in a follow-up turn, may put a question back to Alex — it never acts on its own recommendation. The memory design in section 5 extends this principle to remembering: a proposed entry is written only after Alex confirms it, never on the board's own initiative. |
| AP-7 | **Encapsulated AI layer.** Provider and model are configuration, not code. `provider.models.board` in `config/config.local.json` names the `provider/model` string OpenCode is invoked with; nothing in `board.py` or `cli.py` names a model. |

## 3. The board

### 3.1 Members

**The board is whoever has a filled role profile in the roles folder**
(section 3.4). There is no member list in the code, in the prompts or in
this document, and the repository ships no members: adding a profile adds
a member, removing one removes it, and a board needs at least two. Today
the members are the programme lead and the project managers of the
programme's swim lanes, each speaking for every role and responsibility
under their lead; more roles
(working level, leadership within a swimlane) can be added as profiles
later. One board run is one call per member plus one synthesis call.

### 3.2 Process

| ID | Requirement |
|---|---|
| FR-3.1 | Input: topic, context, options under consideration, hard constraints. The CLI asks for each of the four in turn (`cli.py`, `cmd_board`). The browser interface asks for one free-text question and derives the four through FR-3.2. |
| FR-3.2 | **Clarification happens before the board, in one call, independent of the members** (decided 8 September 2026). The clarifier (`clarify.py`, prompt `clarifier.md`) reads Alex's question plus the knowledge block selected for it (section 5), extracts the four FR-3.1 inputs, and asks at least one question — always at least one, even for a clear topic: the one whose answer would most change the recommendation. Python enforces the minimum (`FALLBACK_QUESTION` when the model returns none). Alex's answers are folded into the context by string assembly (`merge_answers`), Alex confirms or edits the four inputs, and only then are the members polled. The clarifier's questions never reach the members; the member prompt (`board_members.md`) now tells a member to state an assumption rather than ask back. A member that returns the old `{"status": "questions"}` shape anyway is recorded under `failed_members` with its questions, not silently lost. The CLI `board` command does not run the clarifier; it asks for the four inputs directly. |
| FR-3.3 | Each member produces a separate, clearly attributed assessment: `view`, `risks`, `recommendation` (`MemberAssessment`). |
| FR-3.3a | **Members are polled in isolation.** Each member is a separate model call that does not see any other member's answer, nor any other member's profile or name (3.4). All prompts are built before any call is dispatched (`run_board`), so isolation is structural, not conventional, even though the calls run concurrently. Only once all have returned are the results combined and passed to the synthesis call. |
| FR-3.4 | A synthesis follows: overall recommendation, decisive criterion, main counter-arguments, and what new information would change the recommendation (`board_synthesis.md`, `_synthesis_text`). |
| FR-3.5 | Output format: one table per member, synthesis as short prose. `board.render` produces this from the structured `BoardResult` in Python; it is never asked of the model. |
| FR-3.6 | Disagreement between members is stated explicitly, never smoothed over. The synthesis prompt requires a `disagreements` list (empty only where members genuinely agree), and `render` prints it under the synthesis, or states plainly that none were stated. |
| FR-3.7 | Context supplied by Alex — a pasted OIL summary, a Jira status, a cost figure — is passed through the single free-text `Context` input and is used as context by every member (`board_members.md`). The board does not fetch such data itself: this repository holds no data store and no Jira, SharePoint or Outlook access (section 8). What was "the board draws in current OIL, Jira and cost status" in the source specification is, here, "Alex draws it in by typing or pasting it." |

**FR-3.3a is a deliberate exception to AP-3.** One batched call across all six perspectives would be far cheaper and would satisfy AP-3, but a model writing the sixth assessment can see the five it has already written and converges towards them — the disagreement FR-3.6 exists to surface would be smoothed away before anyone could read it. The cost of isolation is accepted for the independence it buys, and that cost is known, not assumed: a verification run against OpenCode 1.18.11 in the source project (Program Lead Cockpit spec 3.8) sent a four-word prompt and got a two-token answer back, and it still reported 8,025 input tokens — OpenCode's own system prompt and tool definitions, charged on every call regardless of prompt size. Seven calls per board question therefore carry roughly 56,000 input tokens of fixed overhead before a single word of the actual topic is counted.

### 3.3 Conversation

A board run does not end at the synthesis. `cmd_board` opens a follow-up loop
immediately after printing the result: it keeps asking for a follow-up
question until Alex enters a blank line.

The owner decision behind this loop is that a follow-up is not a new board
run. `ask_follow_up` re-runs only the synthesis prompt — one model call — over
the original six assessments plus every question/answer pair asked so far
(`BoardConversation.turns`); the members are never polled again for the
same topic. This is why `board_members.md` requires each member's first
answer to be self-contained and to carry its reasoning, not just its
conclusion: it is the only material any later follow-up will ever have to
work with.

The synthesis prompt's follow-up instructions (`board_synthesis.md`, "Follow-up
turn") allow the model to put a question back to Alex rather than guess, where
what would be needed to answer with confidence is genuinely missing from the
six assessments and the conversation so far.

The whole conversation — the initial result and every follow-up turn — lives
only inside the one topic that produced it. `BoardConversation` is an
in-memory object; nothing is written to disk by the conversation itself, and
nothing survives the process exiting. In the browser interface a topic ends
with **Close topic**, which asks whether the decision should be written to
memory (section 5); the CLI conversation ends at a blank line, without that
step.

### 3.4 Role profiles

**Decided 8 September 2026.** What a member *is* — character, skills, the
KPIs it watches, the process tasks it owns, how it assesses, what it pushes back on —
is a Markdown note in one folder, written and edited in Obsidian, read
fresh on every board run (`roles.py`, `load_roles`). Nothing caches it: an
edit is in force on the next question. The same notes define *who* sits on
the board (3.1).

| Rule | Reason |
|---|---|
| One folder, chosen by Alex: `knowledge.roles_folder`, picked in Options or by the setup wizard with the folder dialog. Empty means a folder in the vault whose name starts with "Roles" (`Roles`, `Roles&Responsibilities`) or reads "R&R". No folder, no board: the repository ships no member profiles | One place to look, named as Alex names it. Nothing in code can stand in for the board. |
| **What is the same for every member** — character, how to answer, the rule that a member speaks for every role and responsibility under its lead, and that it judges options against its own measures — is one note in the same folder with `kind: conduct` in its front matter (`_Board member conduct.md`), prepended to every member's profile on every call | The personality that is shared is written once, edited in one place, and never duplicated into every profile. |
| **KPIs: the role names them, the network holds the numbers** (decided 9 September 2026; the "KPI Check" member was removed the same day). Each member's profile lists **its own** measures under `## Targets I am judged on` and never a value — they differ from member to member, and the conduct note carries only the rule, not the list. `MG0`, Maturity Gate Zero, is the baseline; later gates and the current state are recorded against it | Each swim lane is held to its own measures and is the one that knows them. Numbers change every few weeks; a role description should not. |
| A member's numbers are a note in the vault with `kind: kpi`, `affected_swimlanes: [<name>, ...]` plus `lead_swimlane: <name>` (the members it is attached to; `member:` is still read) and `updated: YYYY-MM-DD` in its front matter. `knowledge.kpi_notes` attaches every such note to its member's call, on every run, whatever the question, capped at 2,500 tokens per member and excluded from the ranked selection. A member without a note is told so in its prompt and must state its assumption rather than invent a baseline | The data a member is judged against is mandatory context for that member (AP-1: attached by Python, deterministically), not a note that may or may not rank high enough for this question. The confirm screen names which members have a KPI note. |
| **Age is computed, not trusted to the model.** Python puts the note's `updated` date, its age in days, and a warning past `knowledge.kpi_stale_days` (default 30) above the note; the conduct note requires every quoted value to carry its date, and a value whose date cannot be seen to be treated as an assumption | A number without its date is the most expensive kind of confident answer. |
| **Every** note marked `kind: conduct` is prepended, in file-name order, so common ground can be split across files if wanted; one ships (`_Board member conduct.md`) | Behaviour is written once and edited in one place. Programme facts are not written here at all — they belong in the knowledge network (decided 9 September 2026: the `_Programme context.md` note was removed for that reason). |
| **One file, one member.** The official role description is kept word for word under `## Official description`; what the board needs beyond it — targets, what the role protects when it cannot have everything, and the process tasks that name the role (links to the VPDS task pages, decided 9 September 2026: no keyword list, the process is the source) — is written below it in the same file. A second file naming the same member is ignored and reported on the confirm screen, never merged | One place to read a member, and the official text stays recognisable as official. The added sections are what an organisational job description cannot carry: a description written to define a job states no target and says nothing about what the role sacrifices, which is exactly what a decision needs. |
| The conduct note also tells every member to **read the knowledge network first** and to say where a fact came from, and warns that a role description written for the organisation may state no target and may share whole sections with every other member | The vault is young: silence in it is missing information, not evidence. And the shared sections of official descriptions — customer focus, change management, risk management, innovation — are common ground, not what makes a member's view worth hearing. |
| A profile with a heading and nothing under it is *not filled yet*: it is left off the board and named on the confirm screen, in Options and in the CLI | An empty note must never produce an empty opinion, and must never disappear silently either. |
| Every role page carries `Part of Decision Board AI: true` or `false` in its front matter (a checkbox in Obsidian; `board: false` is still read). `false` keeps the page in the folder, linkable from the process, without a seat on the board — Account Management (decided 9 September 2026) | The membership is visible on every role page as one checkbox, and a role the process names but the board does not seat stays linkable. |
| Notes whose name starts with `_`, or whose front matter says `kind: conduct` or `kind: template`, are never members | Support files live next to the profiles without joining the board. |
| One file per role, or one file with several roles. A file whose front matter names a `member:`, or with at most one level-one heading, is one role (named by the front matter or the file). A file with two or more level-one headings is several roles, one per heading, with `key: value` lines directly under the heading as that role's metadata | Alex writes the board the way he thinks about it — six notes, or one note called Board. |
| **A role file carries `level:` and nothing else about itself.** The icon and colour are derived from the member's name in code, and the line the synthesis is told about a member is the first sentence under `## What I protect when I cannot have everything`, falling back to the first sentence of the profile. `perspective`, `icon`, `color`, `short` and `order` are still read when present, but nothing needs them | A role description is about the role, not about how it is drawn. And official descriptions open with wording every swim lane shares, so the first sentence would tell the synthesis nothing — what a member protects is exactly what distinguishes it. |
| Each member receives its own note in full, under `## Role profile`, declared authoritative for that call, and nothing about any other member — not even their names | FR-3.3a: a member never sees another member's material, and no longer knows who else is on the board. |
| The synthesis receives one line per member (`title: perspective`), never the full profiles | It weighs who said what; it does not need to be six people. |
| The roles folder is excluded from the ranked knowledge selection when it lies inside the vault (`knowledge._roles_inside`) | A profile is mandatory context for one member, not a note competing for the token budget of all of them. *Since 5.3 the role pages are in the table of contents, and since 5.6 they are read with the rest of the vault: a question about who is responsible is answered from them. A member's own profile still reaches it as its profile, whatever else is read.* |
| Fewer than two profiles is `RolesUnavailable`, shown as an error with the folder named | A board of one is not a board; a misconfigured folder must not silently become the six examples. |
| The setup wizard proposes the detected folder (else `<vault>/Roles`), accepts any other, and offers to create it with the conduct note, a profile template and, on request, the example board (`roles/examples/`: the nine swim lanes of one programme, Alex's own, shipped with his consent) when it is missing or has fewer than two filled profiles; Options installs the support files. Existing files are never overwritten | Members are Alex's to write; the examples are his own to edit or delete. |

## 4. AI provider and the OpenCode invocation contract

The provider layer (`agent/provider.py`, `agent/opencode_client.py`) is a copy
of the source project's, taken as-is per decision 0005: two repositories that
evolve at different speeds are better served by duplicated code than by a
shared package.

| ID | Requirement |
|---|---|
| AI-1 | Provider, model and token limits are configurable per task type. This repository has one task type, `ai_board`, resolved from `provider.models.board` and, optionally, `provider.token_limits.board` (`resolve_model`, `token_limit`). |
| AI-2 | Every run logs provider, model, token usage and duration. `OpenCodeProvider.complete` returns an `AiResult` carrying all four for every call; `run_board` passes the synthesis call's `AiResult` to `audit.log_run` (section 6). |
| AI-3 | Only approved endpoints may be configured; confidential program content must not leave approved infrastructure. **The approved endpoint since 8 September 2026 is the company's LiteLLM gateway** (`litellm-ai.visteon.com`), reached through a company-provided `opencode.json` that defines it as OpenCode provider `azure` with the model `Opencode-Kimi-K2.7`. `provider.endpoint` in the configuration is not read by any code path — OpenCode is invoked as a subprocess — but stays as the record of that approval. The board never talks to a model directly; every call goes through OpenCode, and OpenCode through that file. A model from another provider is out of bounds unless the company adds it to the same file. |
| AI-5 | Until a second endpoint is attested by name and date, every model configuration key resolves to the same approved endpoint. This repository has one key (`board`); nothing here contradicts that. |

**Invocation.** `opencode run --format json --model <provider>/<model> [--dir <path>] [--auto] "<header>"` with the prompt on standard input, built by `OpenCodeProvider._build_command` and run by `_run`. The two bracketed flags are passed only when the installed version lists them in `opencode run --help` (OC-7); the prompt never travels on the command line (OC-10).

**Output is JSON Lines**, parsed line by line, never as one JSON array (`_parse_output`):

| ID | Requirement |
|---|---|
| OC-1 | A line that is not valid JSON is reported as an `OpenCodeError`, never silently skipped. |
| OC-2 | The answer is the concatenation of every `text` event's `part.text`, in order. |
| OC-3 | Unknown event types are ignored, not treated as errors — only `text` and `step_finish` are inspected; every other type falls through. |
| OC-4 | Provider, model, token counts and duration are taken from the run: tokens from `step_finish` events, provider/model from the `--model` string that was passed. |
| OC-5 | A run that produces no `text` event at all raises `OpenCodeError`, never returns an empty answer. |
| OC-6 | Whether a headless run hangs without `--auto` is unverified, and so is what `--auto` actually approves. The client sends one prompt and reads one text answer back; it registers no tools of its own. But `--auto` auto-approves whatever OpenCode's own built-in tooling can reach in the working directory, which was never tested here, so the flag is not established as harmless on the basis of this repository exposing no tool surface. `provider.opencode.auto_approve` defaults to `True` because a headless run that stops to ask would hang; confirm on the target machine what a run does without it, and what it can touch with it. |
| OC-7 | **Optional flags are probed, not assumed.** On 8 September 2026 the OpenCode installed on the target machine printed its usage text and exited 1 on every call: its `run` command did not know `--auto` (nor `--dir`), and a flag it does not know is a fatal argument error, not an ignored one. The client therefore runs `opencode run --help` once per process and passes `--auto` and `--dir` only when that text lists them. With `--auto` absent, `provider.opencode.auto_approve` has nothing to act on; whether that version stops to ask for permission on a headless run is, per OC-6, still unverified - it did not on the first real runs, because the client registers no tools. On Windows the prompt is one command-line argument and the OS limit is 32,767 characters; the client refuses above about 30,000 with a message that names the knowledge token budget as the thing to lower. |
| OC-8 | **The company `opencode.json` is passed by environment, not by working directory.** OpenCode looks for `opencode.json` in its global config folder and in the working directory; the company file lives in neither, so `provider.opencode.config_file` names it and the client starts every `opencode` process with `OPENCODE_CONFIG` set to that path (`opencode_environment`). The model string in `provider.models.board` is `<provider id>/<model id>` exactly as the file defines them — `azure/Opencode-Kimi-K2.7` — which is why "select Kimi K2.7" is a configuration value, not a code change. The file holds the gateway key and stays outside this repository; `{env:NAME}` placeholders in it are honoured by OpenCode and by the setup wizard. |
| OC-9 | **A run that answers nothing must say what it did instead.** The answer is read from any text-carrying part, not only from an event literally typed `text` (OC-2 stays the shape spec 3.8 verified; a version that wraps the same part in another event is read the same way). When a run still yields no answer, the error names every event type the run produced with counts, the text of any `error` event — a run can report an error and still exit 0 — the tools it called instead of answering, and the path of the raw JSON Lines, written to `<audit_folder>/opencode-debug/`. `provider.opencode.extra_args` appends further CLI arguments (for example `["--agent", "plan"]`) so a fix for such a run is configuration, not a code change. |
| OC-11 | **A run that answers nothing is retried once.** On 9 September 2026 a call came back with reason `stop`, 920 reasoning tokens and 0 output tokens: the model thought and wrote nothing, and because it was the clarifier call the whole session died. `OpenCodeProvider.complete` now retries such a run once with a one-line nudge in front of the same prompt; a second empty run is reported with both attempts and the token story ("finished with reason 'stop', 920 reasoning tokens, 0 output tokens"). Any other failure is not retried. The board's own retry for tool-only answers stays on top of this. |
| OC-10 | **The prompt goes to OpenCode on standard input, never as an argument.** Windows caps a command line at 32,767 characters and a board call is longer (33,033 on 9 September 2026, with a 6,000-token knowledge budget), so the setup wizard's board-shaped test call failed where the one-word test passed. `opencode run` appends piped input to its message; the one positional argument is a fixed header saying that the instruction follows. The knowledge budget is no longer bounded by the operating system. |

**Cost characteristic.** See 3.2's note on FR-3.3a: 8,025 input tokens of fixed
overhead per call, measured once against OpenCode 1.18.11 in the source
project and carried over as the basis for the ≈56,000-token figure quoted
there for one board's initial round.

### 4.1 The live answer, and OpenCode's server mode

**Decided 13 September 2026.** A question that reads the whole vault takes
the best part of a minute, and the page shows a turning circle for all of
it. Alex: "it is not user friendly to watch the waiting cycle turn while
it is generating the answer - can we make it similar to ChatGPT and Claude
chat, where we see live what the AI is building?" Three ways in were
probed on his machine with `python scripts/run.py probe-stream`, the same
configuration, environment and model as every real call:

| Way | What the probe found |
|---|---|
| `opencode run --format json` (what every call uses today) | One text event at 7.4 s of an 8.5 s run: the whole answer in one piece. Nothing to stream. |
| `opencode run`, plain output | The banner `> build - <model>` at 2.6 s, then the whole answer in one piece at 5.7 s of a 6.7 s run. Nothing to stream either; an earlier reading of this probe called the banner an answer and was wrong. |
| `opencode serve` and its event stream | 175 routes. `POST /api/session` opens a session, `POST /session/{id}/message` carries the prompt, and `GET /api/event` streamed 100 `message.part.delta` events and 7 `message.part.updated` between 5.4 s and 7.1 s of a 9.1 s call. This is the way. |

| # | Decision |
|---|---|
| 1 | **The page shows the answer as it is written**, in the card where the finished answer will stand, with the steps above it. Ask the vault first; the board follows when it is proven. |
| 2 | **The way is OpenCode's server mode**: one `opencode serve` process for as long as Program Mind runs, started when the first call needs it and stopped with the program; one OpenCode session per call, aborted on Stop and deleted when the call ends. OpenCode's own session memory is not used for anything: the thread's history travels in the prompt as it always has (5.4, decision 2, stands). |
| 3 | **What is streamed is for the eye only.** The answer Python parses, checks and stores is the one the call returns at the end, exactly as it is today. A stream that gives nothing, or gives something odd, costs nothing but the live view; it can never change an answer, a source or a gap. |
| 4 | **Only the assistant's own answer text is shown.** The prompt comes back as a part of the user's message and the model's reasoning as a part of its own; neither is the answer. Parts are matched to their message and their type, and anything that is not the assistant's `text` is ignored. |
| 5 | **The server gets nothing to touch.** It is started in an empty folder of its own, never in the repository or the vault: the probe's session ran in the repository with tools live in it (OC-6). The prompts say the model has no tools and needs none; this makes that true of the working directory as well. |
| 6 | **`opencode run` stays, and is the fallback.** `provider.opencode.mode`: `auto` (the default: use the server, fall back to the run for the rest of the session if it will not start), `serve`, or `run`. Everything the run contract guarantees holds for the server too: one retry when a call answers nothing (OC-11), an error that names what happened instead (OC-9), provider, model, tokens and duration on every result (AI-2). The status page says which way is in use. |
| 7 | **The page asks more often while a call runs** (twice a second instead of once) and not at all when nothing runs. The live text is held in memory only: it is never written to the thread file, and what is stored at the end is the answer, as before. |

**Built 13 September 2026** in `ai/opencode_server.py` (the shared process,
the session per call, the event stream), `ai/provider.py` (`complete` takes
an optional `on_text`), the shell server (the live text on the session and
in its snapshot) and the thread page.

## 5. Knowledge source and memory

**Decided 8 September 2026, built in `knowledge.py` and `memory_writer.py`.**
Alex selects one knowledge source: a folder of Markdown notes, which is what
an Obsidian vault is on disk. Alex's own is an Obsidian vault in an
offline-synced OneDrive folder. The board reads it for every question and
writes to it only after Alex confirms a note.

| Rule | Reason |
|---|---|
| The source is one folder, chosen in the browser interface's Options with a native folder dialog or typed in; stored as `knowledge.vault_path` | One thing to configure. "Obsidian or a text file" was never a real choice: Obsidian opens any folder of `.md` files, and a single `.md` file in a folder of its own is a vault of one note. |
| No backup source. A configured folder that cannot be read stops the run with an error naming the folder | Alex's decision: an error message, not a fallback. A board that silently answered without its knowledge would look like a board that had read it. |
| The whole vault is read; every `.md` file under the folder, Obsidian's own `.obsidian` and `.trash` folders skipped | Alex's decision: the whole memory, no folder selection. |
| Selection is deterministic Python (AP-1): notes are ranked by how many of the question's terms appear in their title, tags, file name and body, and packed best-first into `knowledge.token_budget` (default 6,000 tokens per call). When every section fits, every section is sent | The model never lists or reads files itself. Reason on what the question touches, not on everything ever written. *Overtaken 13 September 2026 (5.6, 5.8): every page is sent, and this ranking runs only when the vault does not fit one read. What holds unchanged is the sentence's second half - no model lists or reads files; Python decides what goes into the prompt.* |
| The selected notes go to the clarifier and, appended to `Context`, to every member (FR-3.7). The synthesis call does not receive them | The synthesis reasons over the six assessments only, as before. |
| **One vault, several projects** (decided 9 September 2026). A page that belongs to one or more projects lists them in its front matter: `projects: [Dual DCDC]`. A page without the property is common to every project: the process, the roles, the guide. `knowledge.project`, chosen in Options from the vault's `kind: project` pages, names the project every question is about; the ranked selection and the KPI notes (3.4) then take only the common pages and the pages of that project, and every member's prompt states the project. Empty means every page is used. The project name also opens the file name of a project-specific page (`KPIs/Dual DCDC - Maturity Gates`), because Obsidian resolves a wikilink by file name and a second project will want a page of the same name | The process is common, the numbers are not: a second project's maturity gates must never reach this project's Hardware member. The property is the machine-readable scope and survives a rename; the file-name prefix keeps links unambiguous and the folders sorted by project. Folders stay by kind (`KPIs/`, `Projects/`, `Teams/`), not by project, so the guide's one rule per folder holds; a project that grows can get a subfolder under the kind folder without changing the property rule. |
| Every note the board writes carries YAML front matter: `title`, `tags` (always including `decision-board`), `created`, `source: decision-board` | The board's own notes stay findable in Obsidian's search and graph. |
| On **Close topic**, Alex is asked whether the decision should be remembered. Yes: one model call (`memory_proposal.md`) receives the topic, the synthesis, the conversation and an outline of the vault — folders and existing note titles — and proposes a path and a note. Alex sees exactly what would be written and where, edits path, title, tags and body, and confirms; only then is the file written | AP-4: propose, do not execute. "Show him what will be written and where" was the decision, and the proposal step is the small dedicated agent Alex asked for: it finds the spot in the network, Python writes. |
| A proposed path is validated in Python: inside the vault, a `.md` file, no traversal. Proposing an existing note's path appends a new section to that note; the board never overwrites | A note Alex wrote by hand is never replaced by one the board proposed. |
| The `memory/` folder in `.gitignore` stays reserved but is no longer where notes go; notes go into the vault, next to what they are about | The vault is outside this repository by construction. |
| Past topics are not listed in the interface | Alex's decision. The vault is the history; Obsidian is the browser for it. |

The board never writes to the vault on its own initiative — every write goes
through the confirmation step, and the proposal call has no file access.

**Cost.** One topic in the browser interface is one clarifier call, one
call per member plus the consolidation, one call per follow-up, and one
memory-proposal call if Alex says yes to remembering. Each call carries the
fixed overhead described in section 4 plus the knowledge it is sent: since
5.6 and 5.8 that is the whole vault, up to `knowledge.max_read_tokens`, in
every call that answers something. A board of five in the individual mode
therefore reads the vault five times, and once in the combined mode; the
confirm screen states the figure before the run.

### 5.1 Knowledge selection, second generation

> **Overtaken 13 September 2026 by 5.6 and 5.8.** While the whole vault
> fits one read, nothing below runs: every page goes to every call and no
> call is spent choosing. What follows is the record of the decision and
> the description of the fallback, which is still the path a vault larger
> than one read takes.

**Decided 10 September 2026, built the same day in `knowledge.py`, `picker.py`,
`enrich.py`, `evaluate.py`, `board.py`, `server.py` and `web/`.** Goal: better answers through
more *relevant* knowledge per token and wider coverage, with Alex in
control of how much the model decides and how many tokens go out. Time is
not a constraint; cost is watched, not a bottleneck.

**Pipeline per question**

| Step | Rule | Reason |
|---|---|---|
| 1 Python first cut | Every section is scored as today (question terms double, member terms single), plus the page properties: `affected_swimlanes` names the member +3, `lead_swimlane` is the member +6, the task is active in the question's phase +3 (`phases` property on task pages, the phase read from the aligned input). `aliases` on the page and the Abbreviations page expand the question's terms. The top 40 sections per member, each with the page's `summary`, form the candidate list | Free, instant, deterministic. Uses the properties the vault now carries. |
| 2 AI-assisted pick | One call (`knowledge_pick.md`) receives the question, the members and the candidate list (id, page, heading, summary) and returns per member: sections to send in full, sections to send as a one-line summary, and one reason per pick. Python accepts only ids that exist; an empty or failed call falls back to the Python ranking and says so in the picks screen and the statistics | Picks by meaning, not by word count; the reasons make the choice checkable; nothing invented reaches a member. |
| 3 Packing | Per member: the **shared core** first (project page, the phase and gate definitions, the Definition of every task active in the question's phase), then the member's full sections up to the slider, then up to 20 one-line summaries within a fixed 800-token cap on top, then manual picks up to 12,000 as today. KPI notes unchanged | Coverage grows from about 8 to about 30 sections per member for the same slider value. |
| 4 Combined mode | The shared core goes out once per call, each member gets only its delta | Removes the sixfold repetition that dominated the input cost. |
| 5 Role terms | The member-term sections are found by heading text at any level | The `###` headings on role pages broke the end-of-section search. |

**Vault side.** Every page gets a `summary` property: two lines, written by
the board's `summaries` command from the page text, prefixed `AI summary,
not official`, written only for pages without one or older than the page,
and shown to Alex for confirmation before writing, like every write. Task
pages get `aliases` (`[PPAP, Production Part Approval]`) and `phases`
(from the task table). Long Coaching sections get `###` sub-headings at
the sheet's own sub-titles, wording untouched.

**Interface.** Simple and clear: one new dropdown, information behind an
"i" button, no new screen.

| Element | Behaviour |
|---|---|
| Dropdown **Knowledge selection**, under the token slider, styled like the mode dropdown. Options: `AI assisted (recommended)`, `Python only`. Last choice remembered | Decides whether step 2 runs. |
| Its "i" text, two blocks in the style of the mode info. **AI assisted:** the model reads a list of candidate sections with their summaries and picks, per member, what that member needs, with a reason you can read. *Pro:* picks by meaning, knows synonyms, improves as the vault grows. *Con:* one extra call per question, about the size of one member call; the model can pick wrongly, so the picks are shown before the board runs. *Cost:* one call more. **Python only:* the program ranks sections by how often the question's words and the member's words appear. *Pro:* free, instant, the same result every time. *Con:* literal; misses pages that say the same thing in other words; weaker as the vault grows. *Cost:* none | Pros and cons where the choice is made. |
| Slider "i": what the number caps (full sections per member), what rides on top (summaries, manual picks, KPI notes), and that the estimate follows the slider | No hidden budget. |
| Picks screen (the existing "What each member would receive"): per member two groups, **in full** and **as summary**, each section ticked with the model's reason in small text; buttons `Accept all` and `Use Python picks instead`; a banner when the AI pick failed and the Python ranking is shown | Alex sees and can change every pick before a token is spent on the board. |
| Estimate line counts the pick call: `N calls · about T tokens` | Cost visible before the button. |
| Statistics: a row `knowledge pick`; per member the knowledge tokens split into core, own sections and summaries | Effect of each step measurable. |

**Measurement.** `tests/knowledge_eval/`: ten real questions from Alex,
each with the pages a good answer must use. `board eval-knowledge` prints
the hit rate per mode and per step, so every change to the ranking is
judged by a number, not by feel.

**Order of work.** Role terms and property boosts; summaries, aliases,
phases and sub-headings; shared core and summaries tier; the pick call
with dropdown, picks screen and statistics; the evaluation set from the
start.

**As built, where it differs from the table.** The `summaries` command is
`board enrich`: it proposes `phases` (from the task table), `aliases` (the
task table's name when it differs from the title; abbreviations stay on the
Abbreviations page, whose row links tell the selection which page an
abbreviation stands for) and the
summaries in one pass, prints them, and writes only with `--write` after a
yes; `--refresh` rewrites every summary, `--no-summaries` skips the model.
The candidate list per member is 40 sections, the pick returns at most 8 in
full and 20 as one line per member, and the picks screen shows the picks
with the reason in small text; a pick that did not parse shows the banner
and the Python ranking. `board eval-knowledge --selection ai|python`
prints the hit rate per question and in total; the per-step figure is
read from the statistics split (core, own, summaries) of a run. The set
holds three example questions until Alex supplies the ten real ones. Two rules the first measurement forced, both in `knowledge.py`: pages
about the vault itself (`kind: guide`, the abbreviations table) are never
ranked or summarised for a member, the abbreviations table instead
contributes the rows the question uses to the core; and a section larger
than half the budget (and at least 1,500 tokens) is not sent whole unless
picked by hand, since the abbreviations table and the task table of the
process overview had been taking four fifths of every member's block.

**Measured 10 September 2026** on the enriched vault (40 pages, 8 members,
five questions, slider 6,000, Python selection), old code against new: a
member received 2.1 sections from 2.0 pages before, 13.2 sections from 9.0
pages in full plus 8.3 pages as one line after, for 6,759 instead of 5,998
tokens (the one-liners ride on top). Combined mode sends 45,300 knowledge
tokens per question instead of 54,100 by sending the core once. The
review of the same day fixed: a pick made for earlier inputs was reused
after Back, a stopped pick could block the next one, the remembered
dropdown choice started a pick nobody polled, the candidate list followed
page order instead of rank, the abbreviation rows could not be unticked,
a failed pick was counted as a call the run never made, the estimate
rebuilt the candidates on every keystroke, `enrich` stamped KPI dates and
could double the summary marker.

### 5.2 Fresh knowledge on every turn

> **Still in force, and now invisible.** Every call still selects for the
> question it is answering; since 5.6 that selection is the whole vault, so
> nothing is left behind to go stale. Decision 3, the ranking query, applies
> where the ranking still runs.

**Decided and built 10 September 2026**, after the first day of use: the
clarifier's second round asked who the project managers are and could not
read the page that names them, because the vault had been read once, for
the first question, and never again.

| # | Decision |
|---|---|
| 1 | **Every call that answers something new selects its own pages.** No call runs on the selection another question made. That holds for each clarifier round, for every member asked again in a follow-up, for the board's own follow-up answer, and for every question of an Ask the vault thread. |
| 2 | **Python ranks it, never the model.** The AI-assisted pick of 5.1 runs where it always did, on the confirm screen, and nowhere else: a clarifier round and a follow-up must not cost an extra model call. The cost of reading again is zero calls. |
| 3 | **The question being answered now decides.** The ranking counts how often a query word appears in a section, so a subject that has been in the query since the first question outweighs the new question every time - which is exactly why a follow-up kept getting the first question's pages back. The earlier question (a thread) or the topic (the board) joins the query only when the new question carries no word of its own, as "and who approves that?" does. |
| 4 | **The clarifier ranks on everything the rounds have said**: the original question, and the questions and answers of every round so far. The clarifier's own question is part of it - "what are the names of the project managers" is the text that pulls the pages naming them. |
| 5 | **What was sent stays sent.** A member asked again receives the block chosen for the new question, and its earlier assessment is in the prompt as before. A citation is checked against every page that member has been sent in this topic, in any round, so a fact taken from the first round still checks out. |
| 6 | **The page says what was read.** A follow-up turn, in either agent, names the pages that this topic or thread had not seen before ("Read from the vault for this question: ..."). Nothing new to report, nothing shown. |
| 7 | The slider's budget applies per call, as it always has. Reading again costs tokens, not calls. |

### 5.3 The model chooses, from the whole table of contents

> **Partly overtaken.** The choosing call runs only when the vault does not
> fit one read (5.6, decision 3), and the picks screen of decision 1 goes
> with it (5.7, decision 1); the slider of decision 4 went in 5.5. What
> stands is the principle: when everything cannot be read, the model
> decides what is read, not a formula, and Python keeps the last word.

**Decided 10 September 2026, after the second day of use.** The question
"which VPDS tasks is [a named person] responsible for" found nothing: the
word ranking sent the org chart as a pointer and never listed a task page,
because the tasks carry the role, not the name. A better formula cannot
make that hop. The choice of pages moves to the model, in front of every
read, and Alex sees it before anything is read.

| # | Decision |
|---|---|
| 1 | **Three steps for every question, in both agents.** (1) Python writes the table of contents of the vault: every page as one entry, with its title, `kind`, `lead_swimlane`, `affected_swimlanes`, `phases`, `aliases`, the AI summary, and its sections with their headings and sizes. No ranking, nothing left out. (2) One call: the model reads the table of contents and names what it wants to read, most important first, with one line why each. (3) The picks screen, every time: the chosen pages ticked with their reasons, the rest of the vault below to add from, the slider and the estimate; one click reads them and answers. |
| 2 | **The model is told what the list is and how to choose.** Each entry says what the page holds, as far as its title, properties and summary tell. The model takes a page when the answer might be on it, not only when it is sure. When it cannot tell which page of a kind holds the answer - a role it does not know the holder of, a task it cannot name - it takes every page of that kind that could, and says so. Breadth costs tokens, not calls, and the slider and the screen keep it in hand. |
| 3 | **A pick is an id, a page or a rule.** A section id reads that section; a page path reads the whole page; a rule names property values (`kind: process, lead_swimlane: HW Engineering`) and Python expands it to every page that matches. The rule is what makes "all VPDS tasks of that role" one line rather than forty ids the model might get wrong. |
| 4 | **The budget caps the read, not a count.** Python fills the slider's budget in the model's order. What does not fit stays on the list, unticked and marked "over the budget", so Alex sees what was left and can raise the slider. The caps of 5.1 (eight in full, twenty as one line) go. |
| 5 | **Python keeps the last word**, as in 5.1: an id, a page or a rule that matches nothing in the vault is dropped and counted; a pick that fails or returns nothing leaves the word ranking in force, and the picks screen says so. |
| 6 | **Ask the vault waits at the picks screen.** A question starts the choosing call; the thread then rests in a `picks` phase until Alex reads or cancels. Stop works in both calls. A follow-up goes through the same three steps. Decision 3 of section 10 (no pick for the agent, one call is the point) is withdrawn: two calls per question, the first the size of the table of contents. |
| 7 | **The board's pick sees the whole vault too.** On the confirm screen the pick that already runs per member chooses from the table of contents rather than from Python's shortlist. Nothing else on that screen changes. |
| 8 | **The table of contents has a size, and it is shown.** The status page prints it. Above `knowledge.contents_limit_tokens` (default 40,000) Python trims the list to the pages the word ranking puts first for the question, and the picks screen says the list was trimmed. A safety valve, not a mode. |
| 9 | **One line in the vault makes the named-person case direct**: the holder's name in `aliases` on the role page. Then the table of contents shows name, role, and every task with that lead, and the model needs no rule of thumb. That is what `aliases` is for in the vault guide; the enrich command may propose it from the org chart. |

The word ranking of 5.1 stays for what it is good at: the fallback when
the model's pick fails, the trim of an oversized table of contents, the
ranking query of 5.2 for the clarifier, and the evaluation baseline.

**Built 10 September 2026** in `knowledge.contents`, `picker.choose`, the
prompt `knowledge_choose.md`, the server and the thread page. As built:
the table of contents lists the role pages too, which the ranking of 5.1
leaves out for the board (a profile is a member's mandatory context, not a
note competing for the budget); Ask the vault may read them, since "who is
responsible" is answered there. **What the model chose is the block**: the
core and the manual picks come with it, and nothing is topped up from the
ranking behind Alex's back, so the picks screen shows exactly what will be
read; a chosen section is never held back by the big-section rule, only by
the budget. A question asked while a choice waits is refused until the
choice is read or cancelled; cancelling puts the question back in the box.
The choosing call is counted in the statistics as "knowledge pick", for
either agent. The board's pick answers in the shape of 5.1 (`full`,
`brief`) or of 5.3 (`read`); both are understood.

### 5.4 Follow-ups, sticky pages and the second pass

> **Partly overtaken.** Decisions 1, 2, 5, 6 and 10 stand - the chooser's
> memory, the sticky pages, the folded sources, the gap wording (refined by
> 5.7, decision 5) and the core's skip list. The second pass of decisions 7
> and 8 was replaced by the loop of 5.5, the budget line and the four
> groups of decisions 3 and 4 by 5.6 and 5.7, and decision 11 with them;
> the steps of decision 9 are now those of 5.6, decision 6.

**Decided 11 September 2026, after the third day of use.** Two questions
in one thread showed the weak spots of 5.3. The first, "which VPDS tasks
is [a named person] responsible for", was answered well: the chooser took
the role page and that role's tasks. The follow-up, "list the exact
wording of the tasks you listed", went wrong twice over. The chooser saw
only the first 600 characters of each earlier answer, so "the tasks you
listed" pointed at text it could not see, and it chose two pages. And the
slider could not hold twenty task pages, so the chosen pages fell out of
the budget and the answer said the pages "were sent only as summaries",
which the screen had not made plain. Eleven decisions, agreed one by one
in an interview, and one concept chosen over another.

| # | Decision |
|---|---|
| 1 | **The chooser remembers the thread the way the answering call does.** Its prompt carries the earlier questions and answers within the same limit as the answering call (12,000 characters, the latest turns surviving when the thread is long), never a 600-character cut. It also carries the list of pages the thread has read so far. Its instructions say: when the question points back at what the last answer listed ("the tasks you listed", "each of them"), read in full the pages that answer drew on. |
| 2 | **Sticky pages (Concept B).** A page read in full once in a thread stays with the thread: it is offered to the chooser as already read, it is read again for every later question unless the chooser drops it or Alex unticks it, and the answering call gets it again. The chooser may drop a kept page it no longer needs, with a reason, and may name a kept page in its own list to read it first. When the choosing call fails, every earlier page is kept. Kept pages queue behind the chooser's own picks and before nothing else; the budget cuts them like any other pick. The alternative, Concept A - one OpenCode session per thread so the model keeps its own context - was rejected: replaying a session costs the same tokens as sending the pages again, a deleted thread would have to be deleted through OpenCode's API as well, the board's isolation of members contradicts one session, and the coding agent's own system prompt would sit under every call. |
| 3 | **The budget is stated out loud.** When chosen or kept pages do not fit the slider, the picks screen says so in one line - "N pages of the choice do not fit the slider" - with a button that raises the slider to exactly the figure that fits them, rounded to the slider's step. Above the slider's top the line says how much would still be missing. The over-budget rows stay where 5.3 put them. |
| 4 | **The picks screen is four groups, each collapsed with a count**: chosen by the model and within the budget (the core and Alex's own picks among them); kept from earlier turns (only when the thread has earlier turns); chosen or kept but over the budget; not chosen, the rest of the vault with the search box. Nothing is read from the fourth group unless Alex ticks it. |
| 5 | **The answer card folds its sources.** "Sources" is a collapsed dropdown under the answer, with the pages the model cited and, inside the same dropdown, the pages read for this question and which of them were kept from earlier turns. The line "Read from the vault for this question" goes from the answer card; the record stays in the dropdown. The board keeps its line (5.2, decision 6), as nothing else on a board turn lists pages. |
| 6 | **"Not in the vault" becomes "Not in the pages read".** A gap says that the pages sent did not hold it, which is all the model can know; the page may well be in the vault and not chosen. |
| 7 | **The answering call may ask for pages once.** Its JSON gains `missing`: pages it needed and did not get in full - a page it saw as a one-line summary, a page an earlier answer of the thread drew on, a page it knows by name. Python resolves the names against the vault (path or file name, as it checks sources), drops what does not exist or was already sent in full, and counts what it dropped. Pages only, never a search. |
| 8 | **The second pass runs by itself, once per question, with its own budget.** The missing pages are read within a budget equal to the slider, not a share of it, and the model is called again with its first answer, the earlier turns, the question and those pages - not the first pages again: the first answer stands for them. The second answer replaces the first on the card; the first is kept on the record. A `missing` list in the second answer is ignored. Sources of the final answer are checked against the pages of both passes. |
| 9 | **The steps are shown with real numbers.** While a question runs the thread shows its steps and which one it is on: looking through the table of contents; the picks screen; reading N notes; writing the answer; and, when the model asked for more, reading M more notes; writing the final answer. Reading is Python's step and is done the moment the call starts, so "writing" is the step that waits; the count is the pages sent in full. The card stands in for the spinner's one line. The turn names the pages the second pass read. |
| 10 | **The core skips two sections of the project page**: the vehicle concepts and the awarded volumes. They are long, they change the answer to almost no question, and they took the core's share of every budget. `knowledge.core_skip_headings` names the headings (matched without regard to case, by their start), default "Vehicle concepts" and "Awarded volumes". The sections stay in the table of contents and the outline, so the chooser or Alex can still read them. |
| 11 | **The statistics count the second pass as its own call**, "ask the vault, second pass". The estimate on the picks screen says "1 or 2 more model calls": whether the second runs is the model's to decide. |

**Built 11 September 2026** in `picker.choose_prompt` and `parse_choice`
(the history block, the pages already read, `drop`), `ask.ask_prompt`
(`missing`, the second-pass prompt with `## What you asked for`),
`knowledge.core_sections` (the skip list), `knowledge.gather_pages` (the
second pass's block), the server (the `kept` list on the session and the
turn, the `reading_more` phase, the steps in the snapshot, the second
pass in `_ask`), the thread page (four groups, the budget line and its
button, the folded sources, the step card) and the walk. The chooser's
`drop` is applied to the kept list, never to its own picks.

### 5.5 Quality first: whole pages, no budget, and the loop

> **Partly overtaken by 5.6.** The checker and its loop run only when a page
> was left unread; while everything is read there is nothing to check for.
> What stands, and holds everywhere: no budget, whole pages, one ceiling per
> read, and the rule that a stream never becomes an answer.

**Decided 11 September 2026, after the first real run of 5.4.** The
follow-up "what are the exact subtasks within those VPDS tasks" read 13
pages: the slider at 12,000 tokens held the core, the kept pages and three
task pages, and the other task pages fell out and went in as one-line
summaries. The model saw that and said so, in `gaps` rather than in
`missing`, so the second pass never ran. Alex's direction: tokens no
longer matter, quality does; Python should interpret as little as
possible and the model should have the power, with loops that secure the
quality. Decided in one exchange; every recommendation accepted.

| # | Decision |
|---|---|
| 1 | **Ask the vault has no budget.** The slider goes from the thread page. One ceiling stays, `ask.max_read_tokens` (default 120,000), so that a call cannot fail at the model's context limit; it is a config key because the gateway model's limit is not known for sure. What the ceiling cuts is listed on the picks screen as beyond the ceiling, and the turn says when a round could not read everything it wanted. |
| 2 | **A page is read whole or not at all.** The one-line tier goes for Ask the vault. A section id in a pick reads the page it belongs to. The shared core stays as it was (project page without the skipped sections, the gate definitions, the abbreviations the question uses, the phase definitions); it is small and the model may ask for anything beyond it. |
| 3 | **The answering call sees the table of contents.** Every answer call carries the whole table of contents after the pages read, so the model can name any page of the vault in `missing`, not only pages it happened to see. |
| 4 | **A checker call after every answer, and that is the loop.** A separate call gets the question, the thread so far, the answer, the list of pages read and the table of contents, and answers one thing: which pages should also have been read, as paths or rules, with reasons. What it names, and what the answer itself named in `missing`, is read on top of everything read so far, and the answer is written again with all of it, the earlier answer and the checker's note in the prompt. The loop stops when the checker names nothing new, when the reads reach `ask.max_reads` (default 4 per question), or when the ceiling has no room left. A checker that fails or does not parse ends the loop with the answer as it stands and the turn says so. |
| 5 | **Later rounds run by themselves.** Only the first read waits on the picks screen. Stop ends the loop at any point and nothing is recorded. The turn shows the final answer; the earlier answers, what each round added and what the checker said sit folded in the sources. A round cap that was hit is said on the turn, with what the checker still wanted. |
| 6 | **The picks screen is two groups**: chosen by the model, whole pages with the reasons, the core and the kept pages tagged; and not chosen, the rest of the vault with the search box, one tick per page to add it. A third group, beyond the ceiling, appears only when decision 1 cuts. |
| 7 | **The steps carry the rounds.** Looking through the table of contents; your picks; reading N notes; writing the answer; checking the answer; then per further round, reading M more notes, writing the answer again, checking again. |
| 8 | **Python does not second-guess the model.** Two deterministic guarantees were proposed (read every page named in an earlier answer; read every page a named person's role leads) and dropped on Alex's instruction: the loop is the mechanism, not a Python rule. What Python still does is what it always did: resolve names against the vault, expand rules, drop what does not exist, count what it dropped. |
| 9 | **The second pass of 5.4 is withdrawn**, replaced by the loop. The chooser's memory, the kept pages, the folded sources, the gap wording and the core skip list of 5.4 stay. The board is unchanged in this round; it follows once this works. |
| 10 | **The statistics count the checker as "answer check"**; every answer call, first or later, is "ask the vault". |

**Built 11 September 2026** in `knowledge.gather_whole` (the core and
whole pages within the ceiling), `picker.check` and the prompt
`knowledge_check.md`, `ask.ask_prompt` (the table of contents, the
earlier answer and the checker's note), the server (the loop in `_ask`,
the `checking` phase, the rounds on the turn), the thread page (no
slider, two groups, the rounds in the fold) and the walk.

### 5.6 Read the whole vault

**Decided 11 September 2026, after the first real run of 5.5.** The
question "which VPDS tasks are owned by [a named person]" got three lookup
pages from the chooser (the project page, the process overview, the org
chart) and none of the task pages, because the chooser did not yet know
the person's role. The checker then added four or five pages per round
and four rounds were not enough. The model reads timidly, and no prompt
makes it bold reliably. The vault is small: its table of contents is
12,580 tokens, its pages fit in one read. So the choice goes.

| # | Decision |
|---|---|
| 1 | **Every question reads the whole vault** - every page of the chosen project(s), the role pages included, whole - when the whole fits the ceiling `ask.max_read_tokens`. No choosing call, no checker, one answer call. The model cannot miss a page it has in front of it. |
| 2 | **The picks screen stays, inverted.** A question goes to it at once, without a call: every page ticked, "Read for this question · N pages, the whole vault". Alex unticks what he wants left out, or reads. The rest of the machinery of 5.3 to 5.5 - the chooser, the kept pages, the checker loop - runs only when the vault does not fit. |
| 3 | **When the vault outgrows the ceiling, the model ranks.** One call: the chooser of 5.3 is told that the vault is larger than one read and names every page that might bear on the question, most important first. The read order is then Alex's own ticks, the model's list, the kept pages, and the rest of the vault in its own order; Python reads from the top until the ceiling and lists the rest as beyond the ceiling, where a tick moves a page to the front. Python cuts at the ceiling and interprets nothing else. |
| 4 | **The checker runs only when a page was left unread by the ceiling**, and it swaps: the pages it names go to the front of the order and the lowest-ranked pages fall out at the ceiling. `ask.max_reads` defaults to 2 rounds now; four rounds of four pages each were the problem, not the answer. |
| 5 | **The answer call carries the table of contents and the pages read earlier only when the read is partial.** When every page is read, both are noise. |
| 6 | **The steps follow the path**: your picks, reading N notes, writing the answer; the choosing and checking steps appear only on the partial path. The statistics count one call per question on the whole-vault path. |
| 7 | **The ceiling is a guess** (120,000) until the gateway model's context limit is known. If the vault is over it, the ranking path runs and the picks screen says so; then the ceiling should be raised to what the model can take, not the vault trimmed. |

**Built 11 September 2026** in the server (`ask_question` decides the
path from the packer's own answer, `_ask_pages` builds the order, the
checker runs only on a partial read), `picker.choose_prompt` (the ranking
paragraph), the thread page (every page ticked, a filter over the list,
beyond-the-ceiling rows tickable) and the walk.

### 5.7 The question and its pages on one screen

**Decided 13 September 2026**, after the first good run of 5.6 (a named
person's VPDS tasks, then their subtasks, both answered from the whole
vault). Alex: the picks screen "is unnecessary and does not feel smooth.
We don't need this deeper step to be so dominant since we currently read
the whole Obsidian." And for a question asked back: "keep the option to
select or deselect data, but don't make it a separate step."

| # | Decision |
|---|---|
| 1 | **Asking reads and answers**, in one step, whenever the whole vault fits the ceiling. Spec 5.3 decision 6 and 5.6 decision 2 are withdrawn for that case: there is no choice to review when everything is read. |
| 2 | **The pages sit under the question box**, on the new-thread screen and in a thread alike: one folded line, "Pages read for this question - N pages", with the filter and the list inside. Untick a page and it is left out of the read. Nothing about it is modal, and it never opens itself. |
| 3 | **The list is shown before the thread exists.** A question typed on the new-thread screen has no thread to hang an estimate on, so the estimate is served for a question and a project alone (`POST /api/ask/estimate`, no thread), and the first question shows its pages exactly as a later one does. |
| 4 | **The picks screen stays for the one case that needs it**: the vault larger than one read, where the model ranked the pages and the ceiling cut some. Then the question waits, as 5.3 has it, and what was cut is shown. |
| 5 | **The gaps line says what was actually read.** When the whole vault was read and nothing was unticked, the heading is "Not in the vault"; when a page was left out, by the ceiling or by Alex, it stays "Not in the pages read" (5.4, decision 6). The turn records which it was, so an old answer keeps its own wording. |

**Built 13 September 2026** in the shell server (a question that fits goes
straight to `asking`; the estimate without a thread), the thread page (the
picker moved under the question box on both screens) and the walk.

### 5.8 The board on the same footing

**Decided 13 September 2026.** Ask the vault reads the whole vault, shows
its pages under the question box and writes its answer on the screen as it
goes. Alex: "bring the board on the same status / functionally."

| # | Decision |
|---|---|
| 1 | **Every member receives the whole vault**, every page whole, within the same ceiling as Ask the vault - `knowledge.max_read_tokens`, the older `ask.max_read_tokens` still read. The per-member ranking of 5.1 and the AI-assisted pick run only when the vault does not fit that ceiling. |
| 2 | **The isolation is untouched.** Members still answer alone, each with its own role profile, its own KPI notes and its own place in the process; what is now the same for all of them is the knowledge, because everything is read. A member that has nothing to say still says so. |
| 3 | **The slider and the knowledge-selection dropdown leave the confirm screen** while the vault fits, and the pages sit where Ask the vault has them: one folded line, "Pages read for this question - N pages", with a filter and the tick list inside. A page unticked there is left out of every member's read. Both controls come back when the vault outgrows the ceiling, which is the only case they mean anything. |
| 4 | **The cost is shown, not hidden.** In the individual mode a board of five members reads the vault five times, once per call; in the combined mode it is sent once for all of them. The estimate on the confirm screen counts it as it always has, call by call, so the number is in front of Alex before he runs it. |
| 5 | **The board writes on the screen too** (4.1): every member call and the consolidating call stream, so each member's view appears while it is written and the direction appears while it is drawn. The member's card shows what it has so far; the finished, checked entry replaces it when the call ends. As in 4.1, what is streamed is for the eye only - the entry, its sources and the citation check are read from the finished call. |
| 6 | **A follow-up and a clarifier round read the whole vault as well** (5.2 stands: each reads fresh for its own question). |

**Built 13 September 2026** in the shell server (the whole-vault block for
every member, the estimate, the live text per member), `board.py` (an
`on_text` per member call and for the consolidation), `ai/livejson.py`
(the field of a half-written answer, shared with Ask the vault) and the
board page. As built: the combined form does not stream. It writes every
member's entry and the direction in one JSON object, so there is no single
field to follow while it is written; the individual form, which is the
default, streams every call.

## 6. Audit trail

Every completed board run is logged, whether or not the follow-up loop that
comes after it is used. `board.run_board` calls `audit.log_run("board", ...)`
once, after the synthesis call (or after the skip when fewer than two members
answered); follow-up turns are not separately logged.

The trail is `<runtime.audit_folder>/audit_log.jsonl` — append-only JSON
Lines, one object per line, created if it does not exist. Appending, rather
than rewriting the file, means a write cannot corrupt what is already there;
a rewrite-the-whole-file format would put every prior entry at risk on a
failed write. Each entry carries: `timestamp`, `action_class` (always `"B"` —
see section 8, the board has no class C–E action), `target`, a one-line
`description`, `decision` (`"completed"`), `actor`, `pc_name`, `provider`,
`model`, `tokens`, `duration_seconds`, and a structured `counts` object
(`assessments`, `failed_members`, `llm_calls`, `over_token_limit`).

A line that fails to parse when the trail is read back is returned as
`{"malformed": <line>}` rather than dropped, so a hole in the trail is
visible rather than silently absorbed. Logging itself can never take a run
down: if `runtime.audit_folder` is unset, or the write fails, `log_run`
returns `None` and the run's result is still returned to the caller — a run
that did its work and then could not log it has still done its work.

## 7. Language rules

Carried over from the source project (spec chapter 10):

| ID | Requirement |
|---|---|
| LN-1 | Specification, source code, configuration, prompts and log output are in English. |
| LN-2 | Generated correspondence follows the language of the counterpart or source message. This repository generates no correspondence — the board answers Alex, in whatever session he is running, and nothing here is addressed to a third party — so the rule is inherited but has nothing to act on today. It stays recorded in case a future output of this tool is ever addressed to someone other than Alex. |
| LN-3 | No mixing of languages within a single output. |

## 8. Out of scope

Everything below stayed in the source repository (Program Lead Cockpit) under
decision 0005, or was never part of the board to begin with:

| Item | Why it is not here |
|---|---|
| GOV-1..GOV-5, the approval-token gate, action classes C–E | The board has no class C–E action: it never contacts a person and never writes to an external system. Section 6's `action_class: "B"` is the only class this tool ever logs. |
| Reading OIL, Jira, Confluence, SharePoint, Outlook or Teams | This repository has no connector code. Anything the board should know is in the vault (section 5) or is typed into the question by Alex (FR-3.7, 3.2). |
| Adaptive prioritisation, the import flow, the progress-check engine, the email briefing | These are TR-1/TR-2/TR-3 of the source project and never belonged to the board. |
| Model tiering across task types | The source project's `Config` names five task types with different model classes; this repository has exactly one task type (`ai_board`) and one configuration key (`provider.models.board`). |
| Session resumption across separate invocations, and a list of past topics in the interface | The one open question phase 7 of the source project left behind, and answered on 8 September 2026 by section 5 rather than by a session store: what was worth keeping goes into a note in the vault; the rest was not meant to survive the process exiting anyway. Alex decided against a past-topics sidebar. |
| Reaching the browser interface from another machine | Local only (section 9). The vault's content and every prompt would otherwise cross the network. |

## 9. Browser interface

**Decided 8 September 2026** in answer to seventeen questions; the answers
are recorded here so the draft they came from (`docs/hmi-spec-draft.md`)
could be deleted. Built in `server.py` and `web/`.

### 9.1 Decisions

| # | Decision |
|---|---|
| 1 | One text box. The clarifier (FR-3.2) extracts topic, context, options and constraints and shows them for confirmation; the four are editable before the board runs. All six individual answers are kept and shown; the synthesis is shown first. |
| 2 | Always at least one clarification question. |
| 3 | The clarifier is one call in front of the board, independent of the members. Its questions never reach them; the members get only the aligned input. |
| 4 | Simple icons as avatars: a dollar sign for Finance, a chip for HW Engineering, a gear for Mechanical Engineering, a factory for Manufacturing, code brackets for SW Engineering, a target for KPI Check, a check mark for the synthesis. Inline SVG, no image files. |
| 5 | Close topic asks whether to write to memory, shows what would be written and where, and writes only on confirmation (section 5). |
| 6 | No list of past topics. |
| 7 | English throughout (LN-1). |
| 8 | Alex selects the knowledge source: a folder, in his case an Obsidian vault on an offline-synced OneDrive folder. The board reads it and writes to it after approval. |
| 10 | No backup file. A source that cannot be read is an error. |
| 11 | The whole vault. |
| 12 | 6,000 tokens of notes per call, configurable. |
| 13 | Dependencies are allowed if they run on any company machine. None is needed: the server is `http.server`, the page is one HTML file with plain JavaScript and CSS, no build step. |
| 14 | The folder picker is the native folder dialog, opened through tkinter in a separate Python process; on Windows that is the Explorer folder dialog. If tkinter is missing, the path is typed. |
| 15 | Options holds the knowledge source, the token budget, the model string, the token limit, the audit folder, auto-approve and the theme, and writes `config.local.json` on save. |
| 16 | Local only: the server binds to 127.0.0.1 and nothing else, so there is no login. |
| 17 | The CLI `board` command stays, unchanged. Both front ends call the same `run_board`. |
| 18 | Knowledge selection is a dropdown with two options, AI assisted (default) and Python only, with an "i" that states what each does, its pros, its cons and its cost. Decided 10 September 2026, see 5.1. |
| 19 | Every pick the model makes is shown with its reason before the board runs and can be changed; a failed pick falls back to the Python ranking and says so. |
| 20 | The page `summary` is written by AI, marked as such, and confirmed by Alex before it is written. |

### 9.2 Flow

```
Greeting, one text box
  -> clarifying        knowledge selected (section 5), one clarifier call (FR-3.2)
  -> questions         at least one; answers optional
  -> confirm           topic / context / options / constraints, editable
  -> running           six avatars fill in as members return (FR-3.3a)
  -> synthesising      the seventh call
  -> result            synthesis first; click a member's icon to read its
                       view, risks and recommendation; disagreements listed
  -> follow-ups        one call each, until Close topic
  -> close             "write to memory?" -> proposal -> edit -> confirm -> written
```

**Decided 9 September 2026, after the first real runs on the company PC.**

| Rule | Reason |
|---|---|
| **Alex chooses who is asked.** The confirm screen lists every member with a ticked checkbox; unticked members are not called (`run` takes `members`, `run_board(members=...)`). Every follow-up has the same list, empty by default: ticked members are asked again, each in isolation with its earlier assessment, the recommendation so far and the conversation in front of it, and the synthesis answers over their new answers (`ask_follow_up_full`, one call per member plus one). Nobody ticked: the one-call form over the original assessments, as before | "Some changes are not applicable for all swim lanes." A follow-up that can go back to the members is what makes "what if the tooling is free?" answerable with new reasoning instead of a re-reading of old text. |
| **Clarification loops until clear**, up to `clarify.MAX_ROUNDS` (3). Round one always asks at least one question; from round two the clarifier sees every question and answer so far and returns an empty list with `clear: true` when nothing important is missing. "Ask the board now" skips further rounds; the round limit sends the question to the board regardless | One round was not enough when the first answers raised new points, and an endless loop is not clarification. Alex keeps the exit in his hand. |
| **A member may say the topic does not touch it**: `applies: false` in its JSON, with the facts in `view` (which responsibilities and measures it checked), no risks, a one-line recommendation. The synthesis gives such a member no vote and names it under `not_affected`; the interface shows the chip as n/a and the card with its reasons | A short answer is right when the topic is not the member's, but only with an argument - silence would look like agreement. |
| **Plain English, bullets.** Every prompt asks for short sentences and lines starting with "- " (at most five per field, at most 25 words each); risks come back one bullet per risk. The interface renders "- " lines as lists everywhere, including follow-ups, which now answer in a JSON shape (`answer`, `reasons`, `recommendation_now`, `disagreements`) instead of prose | The first real answers were right but long and hard to read. The reader skims. |
| **A tool-only answer is retried once.** When a member call returns tool use and no text, the same prompt is sent once more with a prefix saying that no tools exist and only the JSON is wanted; the prompt itself now says so from the start | Seen on the first real run: Manufacturing called `glob` instead of answering. A retry is cheaper than a lost member. |
| **Every fact is labelled with where it came from, and Python checks the label.** A member returns `facts_from_network` (fact plus the note's path) and `own_judgement` separately. `board.verify_sources` accepts a citation only when the named note is one the board actually sent to that member (the ranked selection or its KPI note) and a distinctive word of the fact appears in it; anything else is kept and flagged, never dropped. The synthesis sees the verdicts, leans on verified facts first, and lists under `rests_on_judgement` the members' own statements its recommendation depends on. The result carries the union of verified notes, every failed citation, and the count of judgement statements; the interface shows them on each card and under the recommendation | "Make sure we know which information was based on our knowledge net and what was invented by AI." A model is a poor witness of its own sources, so the boundary is not left to it: the program knows what it sent, and a citation that does not match is a claim, not a fact. A two-pass form (one strictly network-bound call, one open call, merged) stays the fallback if a real model keeps mislabelling; it doubles the calls, so it is not the default. |
| **Nothing typed is lost to an error or a reload.** The browser remembers the current session's id and reloads it on page open (the server holds the session); everything typed - the question, the answers, the confirm edits and member ticks, a follow-up in progress - is kept as a draft per session in the browser until the topic is closed. A failed session has a "Go back, keep what I typed" action (`/api/sessions/<id>/back`): to the confirm screen when the input was already assembled, else to the last round of questions with its answers; the confirm screen's Back uses the same action. "Start over" keeps the question text | "If you want to go back because an error occurred you lose everything." The server had the data all along; the page just never asked for it back. |
| **An answer can be read as soon as it is in.** `run_board` hands each parsed, source-verified assessment to `on_assessment` from the worker thread; the session keeps them as `partial` and the tile becomes clickable while the other members still think. The direction box waits for all of them as before | Waiting for the slowest member to read the fastest was dead time. Member isolation (FR-3.3a) is untouched: the early answer goes to Alex, never to another member. |
| **Two ways to ask, chosen on the confirm screen and per follow-up.** *Individual* (default): one call per ticked member plus one synthesis, as always. *Combined*: one call (`run_board_combined`, `board_combined.md`) receives the conduct note once, every ticked profile in full with its KPI note, the same knowledge block and input, and returns one entry per member plus the synthesis in one JSON object. Python requires an entry per chosen member (a missing one is failed), checks that each entry names at least one of its own role's terms from its targets, process tasks or title (else flagged "generic"), and verifies every citation per member as in the individual mode. The interface marks a combined result and explains the trade behind an "i" | Eight members are nine calls, each carrying OpenCode's fixed overhead and the knowledge block; combined is roughly one ninth. The trade is stated, not hidden: one model writes all entries and sees the earlier ones while writing the later, so fewer disagreements surface than FR-3.6 would with isolated calls. Combined is for a quick first opinion, follow-ups and topics that concern few swim lanes; individual for a decision that matters. |
| **Statistics per topic**, behind a small bar-chart icon in the top bar: every model call with its step (clarifier, member by name, synthesis, combined board, follow-up by member or board, memory proposal), tokens in and out and model time, grouped and totalled; and wall time per phase from the session's phase marks (board members, consolidation, each follow-up), plus the time from the question to now. Recorded by `RecordingProvider`, a wrapper around the real provider per session; the step is read from the prompt's own markers | "How many tokens since the beginning of the question until the final answer, split by task, and the duration of each step." A retried empty run counts once; time spent answering the clarifier is not model time. |
| Clarification rounds: at most five (raised from three on 9 September 2026) | "The better the info, the better the decision." |
| **Back and Forward on every stage**, in the top bar, and the browser's own Back does the same instead of leaving the page. Back while the model works stops the running calls first (`opencode` processes are started with a handle the session can kill; a stopped call raises "opencode run was stopped" and its late result is discarded), then returns to the confirm screen or the previous round of questions. Back from the result goes to the confirm screen with the result kept behind Forward; Back from a proposal discards it. Forward is greyed unless there is something to go forward to. The header mark is a button: new question, stopping anything still running | "Currently while waiting for the thinking of the disciplines I wanted to go back. The Edge Back goes to Google, and the agents were still running." |
| **Sections, not pages.** Every note is split at its level-one to level-three headings (`knowledge.split_sections`; a note under 1,200 characters stays whole) and the ranking scores sections, so the budget buys the parts that match the question. Sections of one note stay together under the note's path, each labelled with its heading | The VPDS task pages are long tables; a question about timing needed one section of them, not the page. Same budget, far more relevant content, and most topics fit in half the budget. |
| **A knowledge block per member.** At run time the sections are ranked again per chosen member, by the question and by the member's own terms (its targets, process tasks and title, `board.role_terms`), within the budget each; the project page of the active project is pinned first for everyone, within a quarter of the budget (`knowledge.gather_for_members`). Citations are verified against what that member received | Manufacturing gets the manufacturing sections and Finance the finance sections; the identical six-thousand-token block sent seven times was the biggest cost in the first real run. |
| **The knowledge slider on the confirm screen** *(gone since 13 September 2026 while the vault fits one read, 5.8, decision 3)*, 0 to 12,000 tokens per member, defaulting to `knowledge.token_budget`, with a live estimate under it: the exact call count and "about N tokens in", computed by the run's own prompt builders (`board.prompt_sizes`) plus a per-call overhead learned from this topic's real calls (median of real input minus estimated prompt tokens; 6,300 until the first real call), and a fold-out naming what each member would receive. Output tokens and time are not estimated | The cost lever belongs where the decision is made, not in Options. Calls are exact; tokens are labelled an estimate because the character rule is off by up to a fifth against the model's tokenizer. |
| **The prompt is ordered for caching**: rules, conduct note and input first and identical for every member, then the member's knowledge block, then the member's name, profile and KPI data, then one closing line | A gateway that caches a shared prefix, as the company gateway does for OpenCode's own system prompt, serves the shared part from cache. It does not change the token count in the statistics; it can change cost and latency. |
| **The project is chosen on the home page**, in a dropdown with checkboxes like a spreadsheet filter: the vault's `kind: project` pages, "All projects", several at once. The default comes from `knowledge.project` (one name, or several separated by commas), "Save as default" in the picker writes the current choice back; Options no longer carries the field. The choice travels with the question: the clarifier's selection, every member's block, the KPI notes and the pinned project pages are scoped to it (`knowledge.active_projects`, `for_project` with a list) | "The programme selection needs to go from Options to the main page, only where the question is asked; default is the Dual DCDC programme, but several can be selected." |
| **Manual picks under the estimate.** *(Since 13 September 2026 the fold-out is one list of the pages every member receives, with a tick each; see 5.8, decision 3.)* The fold-out lists, per member, the sections the ranking chose with a checkbox each (unticking leaves that section out for every member: `exclude`), and below it the whole outline of the scoped vault with a filter box (ticking sends that section whole to every member on top of the slider: `extra`). The slider does not move; the estimate follows and states what the picks add. Picks and exclusions are kept in the draft and applied at run time | A pick is a deliberate decision that a section matters, so it must not compete with the ranking for the budget, and the reader must see what it costs. |
| **The ask-back list shows the whole board**, unasked members marked "not asked yet". A member asked for the first time in a follow-up gets its knowledge block and KPI data built on demand, and its prompt says it produced no assessment in the first round | The first version listed only the members asked initially. That was a miss: bringing a swim lane in later is exactly what a follow-up is for. |
| The page scrolls to the top only when the screen changes, not on every poll | Polling redraws the result screen once a second; the reader was pulled back up while the board was thinking. |

**Code review of 9 September 2026** (four focused passes over server and page, board and prompts, knowledge and roles, OpenCode client and wizard), decisions taken from it:

| Rule | Reason |
|---|---|
| The server answers only its own page: the Host header must name this machine and port (403 otherwise), a POST must come without an Origin header or from this origin and must carry JSON (403 / 415), a request body is capped at 4 MB, a bad Content-Length is a 400 | A page on any other origin could otherwise issue a "simple" cross-origin POST that rewrites the configuration, starts paid calls or writes into the vault, and a DNS-rebinding page could read every response. |
| The vault is the boundary: symbolic links are not followed, a note over 2 MB is skipped, the memory writer re-validates its fallback path and sanitises before it filters `..` | A link inside the vault must not pull the rest of the disk into a prompt. |
| The gateway model list is asked for over https only, through one GET that follows no redirect | `urllib` re-sends the Authorization header to whatever host a 3xx names, http included. |
| A stopped run is a generation: every worker carries the run id it started for and drops its late writes when the id moved on; a follow-up writes to the turn index it was started for | Back during a run must never resurrect the old result or overwrite a newer turn. |
| Every fact list a model returns is parsed tolerantly (a string where a list was asked, a null, a nested list) and never shown as a Python repr; retries are counted even when both attempts fail; a KPI citation is checked against its own note, not the member's whole KPI block | The first real runs showed where a model's shape drifts; the checks must not fail open. |
| Read once: the vault is cached per process by file mtime and size (a repeat walk stats, it does not read), the vault is split and lower-cased once per selection round, the flag probe runs once per binary, role terms are cached per profile | The estimate on the confirm screen runs on every slider move. |
| Dead code goes: `score_note`, `roles.folder_of`, `roles._first_heading`, `audit.read_entries`, the `roles=` parameter of `run_board`, the `prompt` parameter of `_build_command`, `merge_answers`, `DEFAULT_MODEL`, the unread session fields `sent_notes` and `member_knowledge`, the member-chip styles | What nothing calls is what nobody maintains. |

### 9.3 Setup

`scripts/setup.py` (`setup_wizard.py`) is the one entry point for a new
machine, and is deliberately not written for the machine it was first run on.
It asks once how the machine is set up and records the answer as
`setup.profile`:

| Profile | What it means |
|---|---|
| `company` | An IT-provided `opencode.json` points OpenCode at an internal gateway (AI-3). The file carries the key, so there is no login; its path is stored as `provider.opencode.config_file` and passed as `OPENCODE_CONFIG` (OC-8). The wizard reads the file, lists the provider/model strings it defines, asks the gateway which models it serves, and flags a placeholder key or a plain-`http` endpoint. |
| `private` | OpenCode logs in to a provider account of the user's own. The wizard offers to run `opencode auth login`, then picks a model from `opencode models`. The user is told plainly that the question and the selected notes leave the machine for that provider. |

Everything after that step is identical in both: knowledge source, token
budget, audit folder, port, theme, then two test calls — one word, and one
shaped like a real board call (the member prompt with the knowledge block,
JSON expected), because the first passes on prompts the board never sends.
The wizard installs nothing and changes nothing outside `config.local.json`,
except the OpenCode database it offers to rename when OpenCode's own schema
does not match its binary. Nothing in the shipped configuration names a
company, a person or a machine; `config.example.json` is empty placeholders.

### 9.4 Server

`programmind serve [--port N] [--no-browser]` starts `ThreadingHTTPServer`
on `127.0.0.1` (`server.port`, default 8765) and opens the default browser.
Every model call runs in a background thread; the page polls
`GET /api/sessions/<id>` once a second. `run_board` reports each member's
start and finish through an `on_member` callback that carries state only,
never text — a progress display does not weaken member isolation.

| Route | Purpose |
|---|---|
| `GET /` and `/static/*` | The page, its script and stylesheet, served from `web/`. |
| `GET`/`POST /api/config` | Read and update the options; a write saves `config.local.json`. |
| `POST /api/pick-folder` | Opens the native folder dialog; returns the chosen path or `null`. |
| `POST /api/sessions` | Start a topic from one question: knowledge selection plus the clarifier call. |
| `POST /api/sessions/<id>/answers`, `/run`, `/follow-up`, `/close`, `/memory`, `/discard-memory` | One step of the flow each; a step out of order is refused with 409. |

Sessions live in memory for the life of the process. The server writes
nothing to disk except the audit entry `run_board` already writes and the
one vault note Alex confirms.

### 9.5 Site address and start page

**Decided 10 September 2026 in an interview, built the same day in
`server.py` and `web/`.** The board becomes one use case of several behind
one start page.

| # | Decision |
|---|---|
| 21 | The site's address is `http://ai.localhost:8765/`. Every name under `.localhost` resolves to this machine in Chrome, Edge and Firefox without admin rights or a hosts-file entry, so the name works on a locked-down company laptop. The server still binds to `127.0.0.1` only. `serve` opens the named address; `http://localhost:8765/` keeps working as the fallback and the start page shows both. The name is `server.site_name` (default `ai`). The Host and Origin checks of 9.4 accept `<site_name>.localhost` next to `localhost` and `127.0.0.1`, and nothing else. |
| 22 | The start page at `/` holds the project picker and one tile per use case: **Board** (the tool as it is today, now at `/board`) and **Ask the vault** (`/ask`, section 10). The project is chosen once on the start page, remembered in the browser, and carried into every use case; a use case shows the chosen project with a link back to change it, and no longer carries a picker of its own. Options stays where it is, reachable from every page. |
| 23 | One page, one script, one stylesheet as before: the path decides which screen the script shows (`/`, `/board`, `/ask`), the server serves `index.html` for all three, and the browser's Back button moves between them. Deep links work after a reload. |

## 10. Ask the vault

**Decided 10 September 2026 in an interview, built the same day in
`ask.py`, `agent/prompts/ask.md`, `server.py` and `web/`.** A second use
case on the same infrastructure: one agent, no members, that answers any
question from the Obsidian vault and says where the answer comes from.

### 10.1 Decisions

| # | Decision |
|---|---|
| 1 | **One agent, one call per question.** No clarifier, no confirm screen, no members. The question goes to the model together with the knowledge block; the answer comes back in the same request cycle the board uses (a background thread, the page polls). |
| 2 | **A thread, not a single question.** After an answer the user asks back in the same thread, as often as wanted; each call carries the thread's earlier questions and answers, as the board's follow-up does, so "and who approves that?" works. Leaving the page, reloading or restarting the server does not end a thread: it stays open in the list and continues where it stopped. |
| 3 | **Knowledge: the whole vault, the board's Python ranking, no pick call.** `select_sections` with the question's terms and no member terms; the shared core applies (project page, phases and gates, the abbreviation rows the question uses, the Definitions of tasks active in a named phase); one-line summaries of further pages ride on top as for a member; KPI notes of the chosen project are attached with their age, as for the board. The AI-assisted pick is not used here: one call is the point. *Withdrawn 10 September 2026 by 5.3: the model chooses from the table of contents first, and the picks screen comes before every read.* |
| 4 | **Slider, default 12,000 tokens** (`ask.token_budget`), *withdrawn 11 September 2026 by 5.5, decision 1: a read has no budget, only a ceiling,*  twice a member's default since there is only one call, with the same "i" and the same estimate line ("1 call · about T tokens"). Manual picks and exclusions from the estimate list work as on the confirm screen. |
| 5 | **Answer with sources and gaps.** *(The heading over the gaps says "Not in the vault" when the whole vault was read and "Not in the pages read" when something was left out: 5.7, decision 5.)* The model returns JSON: the answer in Markdown, the sources it used as `path#heading` with one line why each, and the gaps: what the question asked that the vault does not hold. Python keeps only sources that were actually sent (as the board's fact check does), drops the rest and says how many it dropped. The page shows the answer, then "Sources" as links that open the page in Obsidian (`obsidian://open?vault=<name>&file=<path>`, the vault name being the vault folder's name unless `knowledge.vault_name` says otherwise) with the section name beside, then "Not in the vault" when the gaps list is not empty. The prompt (`ask.md`) forbids inventing a rule, a date or an owner: what is not in the block is a gap, never a guess. A question that asks for a decision rather than a fact gets its answer and one line pointing to the Board. |
| 6 | **Threads are kept and listed.** Every thread is saved as one JSON file under `server.threads_folder` (default `threads/` next to `config.local.json`, git-ignored), never in the vault: the questions, the answers with sources and gaps, the knowledge paths sent, the statistics. The `/ask` page lists the threads (first question, date, number of questions) and reopens one for reading and further questions. A thread can be deleted from the list. |
| 7 | **A thread is closed only by the user, after a confirmation.** Nothing closes it on its own. A closed thread stays readable in the list and takes no further questions. **Close thread may propose a vault note**, through the board's memory step unchanged: the same proposal call, the same outline of the vault, the same confirm screen, the same folder and front matter, written only after a yes. Discard leaves the thread as it is. |
| 8 | **Audit trail** as for the board: one `ask` entry per call with the counts and durations, nothing of the text. Statistics per thread as for a topic. |
| 9 | **Language rules (section 7) apply**: English, plain, no invented names. |

### 10.2 Reuse

| Existing part | Used for |
|---|---|
| `knowledge.select_sections`, `core_sections`, `kpi_notes` | The knowledge block, unchanged; `member=""` and no member terms. |
| `Session`, the background jobs, polling, Back, error handling | A session of kind `ask`; the board's sessions are kind `board`. |
| `estimate` and the picks list | The estimate line and the section list under the slider. |
| `memory_writer` and the `/memory` routes | The close step. |
| `audit.log_run`, `RecordingProvider`, statistics | Per call, per thread. |
| `web/index.html`, `app.js`, `style.css` | The start page, the `/ask` screens and the moved board, all in the same files. |

New: `ask.py` (the prompt assembly, the JSON parsing with the source check, the thread store), `agent/prompts/ask.md`, the routes `POST /api/ask` (new thread from a question), `POST /api/ask/<id>/question`, `GET /api/ask`, `GET /api/ask/<id>`, `DELETE /api/ask/<id>`, `POST /api/ask/<id>/close` and the memory routes reused on an ask session, `config`: `server.site_name`, `server.threads_folder`, `ask.token_budget`, `knowledge.vault_name`.

### 10.3 Order of work

1. The address: `site_name`, the Host and Origin checks, `serve` opening the name.
2. The start page, the paths, the project picker moved, the board under `/board`.
3. `ask.py`, the prompt and the routes, with tests against the fake provider.
4. The `/ask` page: question, slider, estimate, answer with sources and gaps, follow-ups.
5. Thread store and list.
6. Close with the memory step.
7. The browser walk extended to the start page and a thread; the spec marked as built.

### 10.4 As built, where it differs from the decisions

> Read 5.5 to 5.7 first: what the agent does today - the whole vault, whole
> pages, the picker under the question box, the answer written on the page -
> was decided there. What follows is how the agent of 10 September differed
> from the decisions of 10.1, and is kept for the reasons it gives.

The ranking query was the question plus the two questions before it in the
thread; since 10 September 2026 (spec 5.2, decision 3) it is the question
alone, with the earlier ones joining only when the new question carries no
word of its own, so "and who approves that?" still finds the pages of the
question it follows while a question of its own subject is no longer
outweighed by the one before it. A running call can be stopped from the page
(`POST /api/ask/<id>/stop`); the question stays typed and nothing is
recorded. `DELETE /api/ask/<id>` removes a thread and its file; a note it
wrote to the vault stays. The slider goes up to 40,000 tokens. The thread
list shows the thread's project(s). The prompt tells the model that a page
listed as a one-line summary was not sent in full, and the source list
marks such a source "summary only". The statistics dialog shows the open
thread's calls when a thread is open, the board's topic otherwise.

### 10.5 Out of scope for now

Any write into the vault other than the confirmed memory note, more than
one agent per question, threads shared between machines. *The first item
of this list - no AI-assisted pick for the agent - was withdrawn on
10 September 2026 by 5.3 and is moot since 5.6: the whole vault is read.*

## 11. Program Mind: the app shell

**Decided 10 September 2026 in an interview of four rounds, built the same
day** in `shell/server.py`, `memory/history.py`, `web/` and each agent's
own script. The site is rebuilt from the ground as an app that hosts
several agents, small and large, all working on the Obsidian vault, which
is the one thing that sets it apart from any other AI. The board and Ask
the vault are the first two.

### 11.1 Decisions

| # | Decision |
|---|---|
| 1 | **Name: Program Mind.** The repository is renamed `program-mind` and the Python package `programmind`, both now, together: the command becomes `programmind` (`programmind serve`, `programmind board`, `programmind enrich`, `programmind eval-knowledge`), `scripts/run_board.py` becomes `scripts/run.py` (not `programmind.py`: a script of the package's name on `sys.path` would shadow the package) and the old script stays one release as a pointer. GitHub redirects the old repository URL; the local clone needs one `git remote set-url`. The site name defaults to `mind` (`http://mind.localhost:8765/`); `server.site_name` still overrides it. |
| 2 | **Mark: a small graph of linked nodes**, three or four connected dots in the accent colour, inline SVG, the same as the tab icon. Wordmark "Program Mind" beside it. |
| 3 | **Look: a calm light workspace, dark mode kept.** White cards on a soft grey ground, one accent colour, generous spacing, app density on desktop, the system theme followed as today. One page, one script, one stylesheet, no build step, as before. |
| 4 | **Shell: a top bar with a burger menu at the right.** From the left: the mark and wordmark as the home button, the project chip (click: back to the home page's picker), the three status icons, the burger. The step arrows go away. |
| 5 | **Home page: the project picker, one card per agent, and the recent open work** (threads and topics, newest first, with agent, title, date, count) to reopen with one click. Agents are reached from here and from the home button, not from the menu. |
| 6 | **Navigation inside a flow: a step line.** The board shows Question · Clarify · Confirm · Result; earlier steps are clickable and go back exactly as the arrows did (the server's back logic stays, as the secondary way, and the browser's Back still steps through it). Ask the vault has no steps: thread list, thread. Every screen can reach home with one click. |
| 7 | **Status icons, three, each red, amber or green**, refreshed on load and every five minutes through one `GET /api/status`, never more often; hovering shows a small card with the detail and a link. **Vault**: green when the folder is reachable and read (hover: path, note count, last read; link opens the vault in Obsidian), amber when reachable but without project pages or a roles folder, red when unset or unreachable. **AI**: green when OpenCode is found, the model string set and the gateway config with a key found; amber when something is set but unverified; red when missing (hover: model, config file; link opens Options). The five-minute check is cheap and makes no model call; a real tiny call runs only when the user clicks the icon and confirms, and its result and time then show in the hover. **Project**: green when a project is chosen (hover: the project and its gate baseline; link to home), amber for "all projects", red when the vault has no project pages. |
| 8 | **The burger menu holds the tools, not the agents**: Options (as today), Statistics (of the open topic or thread), Status details (the three checks written out with their links and a refresh button), Open vault in Obsidian, Archive, Privacy, About, Report a bug. |
| 9 | **History for the board as for the threads.** Every board topic is saved as one JSON file next to the threads (inputs, answers, synthesis, follow-ups, statistics, the knowledge paths), reopened to read or to ask a follow-up, closed only by the user with a confirmation, as a thread is. This replaces decision 6 of section 9.1. The folder becomes `config/history/` with a `kind` per file (`board`, `ask`); a running topic still lives in memory as now and is written after every step that changes it. |
| 10 | **Archive: closed work moves there by itself.** Home and the agent pages list only open work; everything closed is under Archive in the menu, readable, searchable by title, deletable. No archive button. |
| 11 | **Privacy: a written page.** What leaves the machine (the prompts, to the company gateway, with the model named), what never leaves (the vault stays on disk, history and audit stay local, no telemetry), and that every write into the vault is confirmed first. Text only, no live paths. |
| 12 | **About**: the version, the site address, the link to this spec. **Report a bug**: a plain link to the repository's Issues page, opened in a new tab. |
| 13 | The "N notes" chip goes; the Options gear and the Statistics button move into the menu. |

**Built, step 1 (10 September 2026).** The renames and the restructuring:
package `programmind` with `shell/`, `knowledge/`, `memory/`, `ai/` and one
folder per agent under `agents/`, each with its module, its prompts and its
script; the page is `web/index.html` with `shell.js` and one script per
agent that registers itself through `PM.register` (id, `match(pathname)`,
`render(route)`, `onEnter`, `onLeave`, `onPopState`, `boot`, `stats`,
`statsExtra`). The prompt loader searches every prompt folder. The site name
still defaults to `ai` until the shell (step 2) lands. The vault property
`Part of Decision Board AI` keeps its name: it is the vault's convention,
not the tool's.

**Refined, 10 September 2026 (step 3).** A private setup (`setup.profile`
is `private`, OpenCode holding the login itself) is **amber**, not green,
until a test call has answered: green in decision 7 means a gateway
configuration with a key was found, and there is nothing there to find.
The project card's gate baseline is read from the **rows of the project
page's gate table** when the page holds one, and only from its plain
lines when it does not: a page with a table must not have prose about a
gate mixed into the baseline.

**Built, step 2 (10 September 2026).** The shell: the top bar with the
node-graph mark (the same as the tab icon), the wordmark as the home button,
the project chip (click: home, with the picker open), the three status icons
and the burger menu; the home page with the picker, one card per agent and
the open work of every agent (newest first, with agent, title, date and
count; one click reopens a thread at `/ask/<id>` or a topic at
`/board/<id>`, which becomes `/board` once open). `GET /api/status` runs the
three checks; the page asks on load and every five minutes, and once more
after Options are saved or the project is changed, both user actions.
Hovering an icon shows the card with the detail and the link; clicking the
vault or the AI icon opens Status details, clicking the project icon goes
home. The AI icon's card offers the test call: it runs only after a yes on
the status page (`POST /api/status/ai`, one call, logged as `status-check`
in the audit trail) and its outcome outranks the cheap look for as long as
the model string and the gateway file are unchanged. The gateway check reads
the opencode.json for a key-like entry or an `{env:NAME}` placeholder whose
variable is set; the value itself never reaches the page. The project card
shows the project page's summary and every line of it that names a gate or
a phase, as the gate baseline. `GET /api/history?state=open|closed|all`
lists the threads on disk and the board topics the server holds in memory;
the topic files come with step 3. The site name defaults to `mind`. The "N
notes" chip is gone; Options and Statistics sit in the menu next to Status
details, Open vault in Obsidian and Report a bug; Archive, Privacy and About
come with steps 3 and 4. The step arrows stay in the top bar, shown on the
board only, until the step line (step 3) replaces them. The browser walk
covers the icons, the menu, the status page with the test call and the
reopening from the home page.

**Built, step 3 (10 September 2026).** The history, the step line and the
archive. Every board topic is one JSON file in the same folder as the
threads, told apart by `kind` (`board`, `ask`): the folder is
`config/history/`, `server.history_folder` overrides it and the old
`server.threads_folder` is still read, a default `threads/` folder being
renamed once so nothing written before this step is lost. A topic is
written when it is started and after every step that changes it, from
either side (the answer to a request and the background job that finished
it), so the newer of two snapshots never loses to the older. The record
holds the inputs, the rounds, the answers, the synthesis, the follow-ups,
the statistics and the **paths** of the knowledge sent, never the
knowledge text: the vault's words stay in the vault, and a member asked
again in a reopened topic has its block selected anew, the path a member
who was not asked the first time already took. A topic read back rests at
the last step it can be worked from, since a call that was running when
the server stopped cannot be resumed, and its follow-up conversation is
rebuilt from the record when the first follow-up needs it. **Start over is
not closing**: a topic left mid-work is deleted rather than archived. The
step line (Question · Clarify · Confirm · Result) sits above the board's
screens and the arrows are gone from the top bar: an earlier step goes
back exactly as the arrows did, one server-side `back` per step, and the
step after the current one goes forward again; the browser's Back still
steps through it, and clicking Question with nothing behind the first
round lands on the question screen with the question kept. The archive at
`/archive` is in the menu and lists everything closed, of both agents,
searchable by title and deletable (`DELETE /api/history/<id>`, which drops
the file and whatever the server still holds; a note written into the
vault stays). Home and both agent pages list only open work. A closed
topic reopened from the archive is read-only, as a closed thread is: the
result and the follow-ups stay, the follow-up form goes.

**Built, steps 4 and 5 (10 September 2026).** The menu pages and the walk.
Privacy and About are two written screens (`/privacy`, `/about`) reached
from the menu, which now reads Options, Statistics, Status details, Open
vault in Obsidian, Archive, Privacy, About, Report a bug, in that order.
Privacy names what leaves the machine (one thing: the prompt each agent
builds, to the company gateway, answered by the model named in Options,
and what a prompt holds), what never does (the vault, the history, the
audit trail, and anything about how the site is used - no telemetry of any
kind), that no agent writes into the vault without a confirmation, and
that what the gateway does with a prompt is the company's agreement, not
this program's. Text only, no live paths, as decision 11 asks. About shows
the version, the site address, the specification and the repository. **The
version is the date of the release** (`programmind.__version__`,
`2026.09.10`): the specification dates every decision, one person runs one
copy, and there is nothing for two numbers to compare. The specification
is served from this machine at `/spec` as plain text, so About works
without the internet; when the program was copied without `docs/`, the
link goes to the repository instead. Report a bug points at the
repository's Issues page, in a new tab. The browser walk now runs the
whole shell end to end - the status icons and their cards, the menu, the
status page with its test call, the home page and its open work, both
agents, the step line, the archive with its search and its deletion, and
the two written pages - and prints `errors: []`.

**The wart of step 1 is gone (10 September 2026).** `knowledge/` no longer
imports from an agent: `resolve_folder` and `detect_folder` moved out of
`agents/board/roles.py` into `knowledge/knowledge.py` as
`resolve_roles_folder` and `detect_roles_folder`, with
`DEFAULT_ROLES_SUBFOLDER` beside them. Every reader of the vault has to
know which folder to leave out, so the knowledge module owns the question;
`roles.py` keeps the name `DEFAULT_SUBFOLDER` pointing at the new
constant. Behaviour is unchanged.

**The address is `http://program-mind.localhost:8765/` (10 September 2026).**
`server.site_name` defaults to `program-mind` rather than `mind`, and it may
now carry a dot: a plain name is used under `.localhost`, which every
browser resolves to this machine without a hosts file and without admin
rights, while a name with a dot in it (`program-mind.local`,
`mind.visteon.net`) is taken as the whole host name. Nothing in the program
makes such a name resolve - `.local` is mDNS and a company name needs DNS or
a hosts entry, both of which need rights this program does not have - so the
dotted form is for a machine where someone has already arranged it. The
server binds to 127.0.0.1 either way, and `http://localhost:8765/` stays the
fallback.

**Changed after the first day of use (10 September 2026).** Six things the
first real session showed:

- **The browser's Back trapped the board.** A topic opened from the home
  page pushed its `/board/<id>` address into the history and every step of
  the flow pushed another entry, including the steps the Back button itself
  caused: Back then bounced between two screens for ever. The address that
  only opens a topic is now replaced rather than pushed, and a step the
  browser's Back caused adds no entry. Back steps through the flow and then
  leaves for the page behind it, as it does for a thread.
- **A row of work is one target.** The whole row opens the topic or the
  thread, not only its title, and the rows are laid out in columns of their
  own width, so a longer agent name or date no longer shifts the titles out
  of line.
- **Status details left the menu.** The three icons carry it: hovering shows
  the detail, clicking opens the page. Decision 8's list loses that entry.
- **The current step is unmistakable.** The step the topic stands on is a
  filled pill with a ring; the steps behind it carry a check mark instead of
  their number.
- **A citation that could not be confirmed is not an error.** "Citations
  that failed the check", in red, is now "To check", in amber, with a "?"
  and one line saying what it means: the model named a page that was not
  among those sent, the statement may still be right, and nothing the board
  read confirms it.
- **The knowledge is read again on every turn** (spec 5.2).

### 11.2 Reuse

Everything behind the API stays: sessions, threads, the knowledge
selection, the memory step, the audit trail, the statistics. New on the
server: `GET /api/status` (the three checks, cheap), `POST /api/status/ai`
(the confirmed test call), the history store for board topics (the
thread store generalised), `GET /api/history?state=open|closed`. The page
is rewritten around the shell; the screens of the board and of Ask the
vault keep their content and their element ids where the tests and the
browser walk rely on them.

### 11.3 Order of work

1. The renames: repository, package, command, scripts, README, config
   folders, tests. One commit, nothing else in it.
2. The shell: top bar, mark, burger, status API and icons, home page with
   the agent cards and the recent work.
3. Board history and the step line; the archive for both agents.
4. The menu pages: Options and Statistics moved, Status details, Privacy,
   About, Report a bug, Open vault.
5. Browser walk over the shell, both agents and the archive; the spec
   marked as built.

### 11.4 Out of scope for now

An agents list in the menu, company brand colours, a layout made for
phones beyond the responsive rules already in place, a real reachability
call on a timer.

## 12. Look and feel: the design system

**Decided 10 September 2026 from a mockup Alex drew up, built the same day**
in `web/style.css`, `web/index.html` and the three scripts. The
functionality and the information architecture do not change: this is how
the same screens look, not what they do. The rule over everything else is
**consistency before creativity** - one system, used everywhere, rather than
a page designed twice.

### 12.1 Decisions

| # | Decision |
|---|---|
| 1 | **One stylesheet, in three layers**: the tokens (colour, type, space, radius), then the components, then the few screen-specific rules that are left. A component is defined once and used on every screen; there is no page's own version of a button, a card or a list row. Still one file, still no build step and no dependency (principle: stdlib only, section 9.1 decision 13). |
| 2 | **The palette carries meaning, never decoration.** Primary `#2563eb` for the one action that matters on a screen; green for healthy, amber for a warning, red for an error or a destructive act, grey for everything secondary. A colour that says nothing is not used. Every token has a dark-mode value: the light workspace is the default, dark is kept (11.1 decision 3). |
| 3 | **Thin borders, not heavy shadows.** One border colour, one radius scale (10px for controls, 14px for cards, 999px for chips). A shadow appears only where something floats above the page: a menu, a dialog, the picker. |
| 4 | **One font**: Inter where the machine has it, the system sans otherwise. Four sizes carry the hierarchy - page title, section title, body, help text - and headings stay small; weight and colour do more work than size. |
| 5 | **A spacing scale of one step size** (4px), used as tokens. Every gap on every screen comes from it, so the rhythm is the same everywhere. |
| 6 | **The shell**: a white header with the mark and wordmark, the project selector, the three status checks as **labelled chips** (Vault, Model, Project) rather than bare icons, and the menu. Below it one centred column, at most 1120px wide, and every screen opens with a page title and, where it helps, one line of subtitle. |
| 7 | **Every screen states its state.** Loading, empty, success and error are components of the system - a spinner with a line of text, a quiet line in place of a list, a green mark, a red mark - not something each screen invents. |
| 8 | **Responsive by stacking, never by shrinking.** Below about 900px the two-column screens become one column, the header chips lose their labels and keep their dots, and the rows of work stack their meta line under the title. Nothing is hidden that the desktop shows, except a label whose dot carries the same meaning. |

### 12.2 What the mockup asked for and what it got

| From the mockup | Built | Why not, where not |
|---|---|---|
| Design tokens, one accent, status colours, thin borders, small radii, generous space | Yes | |
| Header with project selector and named status chips | Yes | The three checks keep their meaning (11.1 decision 7); they gain their names back. |
| Home: one card per agent with its own action, then the recent work as rows | Yes | |
| Ask the vault: the thread as a conversation, the question as a bubble, the answer with its sources | Yes | |
| Confirm: the question on the left, who is asked and with what on the right | Yes | The screen was one long column; the same fields now read as two. |
| Archive: search and tabs (All, Board, Ask the vault) | Yes | The tabs filter what the archive already held. |
| Options as a two-pane dialog with a section list | Yes | The four sections were already there as fieldsets: Knowledge, Board members, Model, Runtime. |
| Privacy as four cards | Yes | The page already had exactly those four parts. |
| About as labelled rows with links | Yes | |
| Statistics: figures as tiles above the detail | Yes | |
| Status: one row per check with a state chip | Yes | |
| Loading, done, error, dialogs | Yes | |
| Statistics: a bar chart of topics over time | No | The program keeps the calls of the open topic and the audit trail, not a history of topics per day. A chart would have to invent its own numbers. |
| Clarify: a fixed four-step rail (Main question, Context, Constraints, Review) | No | The clarifier asks what it needs, a variable number of questions in up to five rounds; a fixed rail would misstate what is happening. The rounds are shown as they are, as numbered cards. |
| A component library (Tailwind, headless UI) and a modern build | No | No dependency and no build step is a founding rule, and the machine this runs on is a locked-down company laptop with the vault on it. The design system is plain CSS in the same one file. |

## 13. Origin

Decision Board was TR-4 of the Program Lead Cockpit, a single-user automation
system for a Program Lead at Visteon Electronics running the MB32829 Gen6
Dual DCDC program. Decision 0005 of that project, dated 8 September 2026,
split the AI Board out into its own repository — this one — because decision
support on arbitrary topics is a different product from the Open Issue List
that the rest of that system tracks, even though the two share an AI layer.
A reader who meets a stray reference to `OIL`, `MB32829`, or a requirement ID
from a chapter other than the ones this document defines is looking at
something that predates the split; it is not part of this repository.
