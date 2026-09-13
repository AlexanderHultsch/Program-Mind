# Handover

*Last written 13 September 2026. This file is always the current one: it
has no date in its name, and every session updates it.*

For a fresh Claude Code session on `AlexanderHultsch/program-mind`. Read
this file first, then `docs/spec.md` — it opens with what the program does
today and where each piece was decided — and the vault guide
`_How this vault works.md` (the vault's own specification, in the vault,
not in this repository).

## Who and what

Alex (Program Lead, Visteon, project MB32829 Dual DCDC) builds Program
Mind: a local site hosting AI agents on his Obsidian vault. The company
gateway model is `azure/Opencode-Kimi-K2.7` through OpenCode; the gateway
config `opencode.json` with the key lives outside the repository.

Two agents exist: the **Board** (a decision, every swim lane answering in
isolation, one consolidation) and **Ask the vault** (one agent, any
question, answers with sources and gaps, threads that stay open until
closed). They sit inside the **shell**: a top bar, a home page, a menu, an
archive. Everything in the specification is built; nothing is outstanding.

**How a question is answered, in both agents.** The whole vault is read —
every page of the chosen project, whole, role pages included — as long as
it fits `knowledge.max_read_tokens` (120,000). The pages are shown under
the question box with a tick each before anything is read, and asking
reads and answers in one step. The answer appears on the page as the model
writes it, through OpenCode's server mode; on the board each member writes
in its own card. What is streamed is display only: the answer that is
kept, its sources and every check come from the finished call.

**Only when the vault outgrows the ceiling** does the older machinery run:
the model ranks the pages from the table of contents (5.3), the read fills
from the top until the ceiling (5.6), a check after the answer swaps in
pages left unread (5.5), and the board falls back to its per-member
ranking with the slider and the selection dropdown (5.1, 5.8). Tests force
that path with a small ceiling; on Alex's vault it does not happen.

**What it costs.** Ask the vault: one call of roughly the vault's size. The
board in the individual mode: one call per member plus one, each carrying
the whole vault, so a board of five reads it five times; the combined mode
sends it once for all members. The confirm screen says the figure before
the run.

## Rules that never change

- The vault is company confidential. Its content never enters the
  repository, a commit, a test fixture or a screenshot. Alex shares it as a
  zip in the chat when needed and gets a zip back.
- `config/config.local.json`, `config/history/`, `audit/` and the vault are
  git-ignored and stay so. The company `opencode.json` stays outside the
  repository. The internal model is used for data security.
- Stdlib only, Python 3.11, no build step for the page, no dependencies.
- Every write into the vault is confirmed by Alex first. Topics and threads
  are stored next to the config, never in the vault.
- English throughout, plain, short sentences (spec section 7).
- Write the spec section before building. Alex asks for it every time; it
  is how the decisions keep their reasons.
- Commit messages, PR bodies, code and comments carry no model name.

## Repository and workflow

- The repository is `program-mind` on GitHub. The old `Program-Mind` URL
  still redirects, so an old remote keeps working.
- Branches: work on `claude/continue-2ulsy2`, then fast-forward main
  (`git checkout main && git merge --ff-only <branch> && git push origin main`).
  Alex has authorised pushing without asking each time. Every commit ends
  with the two trailers the harness gives you (`Co-Authored-By`,
  `Claude-Session`).
- The gate before every commit: `sh tools/precommit.sh` — the example
  config parses and the whole suite is green (322 tests at the 5.8 commit).
- The browser walk (Playwright, Chromium preinstalled in the cloud
  environment at `/opt/pw-browsers`): start
  `python3 tools/browser_walk/demo_server.py` and run
  `NODE_PATH=/opt/node22/lib/node_modules node tools/browser_walk/walk.js`
  from a folder where `shots/` may be written. It walks the shell, both
  agents and the archive against a fake model and prints `errors: []` when
  clean. **Restart the demo server before each run**: it keeps its topics
  and threads in a temporary folder, and a second run against the same
  server sees the first run's work. Stop it with
  `kill $(pgrep -f "^python3 .*demo_server")`.

## Layout (at the 5.8 commit)

```
src/programmind/
  __init__.py               __version__, the date of the release
  cli.py, setup_wizard.py
  shell/server.py           API routing, sessions, status, history, static files
  knowledge/                knowledge.py (the whole-vault read, the ranking behind the ceiling, the core, the table of contents),
                            picker.py (the ranking call and the answer check), enrich.py, evaluate.py, prompts/
  memory/                   memory_writer.py, audit.py, history.py (one JSON file per topic and thread), prompts/
  ai/                       provider.py (the interface and the mode switch), opencode_client.py (a run per call),
                            opencode_server.py (the live answer), livejson.py (a field of half-written JSON), probe.py, prompts.py
  agents/board/             board.py, clarify.py, roles.py, prompts/, web/board.js
  agents/ask/               ask.py, prompts/ask.md, web/ask.js
  web/                      index.html, shell.js, style.css
tests/                      unittest; _roles_fixture.py; knowledge_eval/questions.json (3 example questions)
tools/                      precommit.sh, browser_walk/ (demo_server.py, walk.js)
scripts/                    run.py (serve, board, enrich, eval-knowledge, probe-stream), setup.py, install.ps1
roles/                      the conduct note and templates the wizard installs (no members)
docs/spec.md                the specification; docs/handover.md is this file
```

The page: `shell.js` defines `window.PM` (helpers, config, project picker,
status icons, the burger menu, options, statistics, the status page, the
proposal screen, the archive, the home page, the routes) and boots on
`DOMContentLoaded`. Each agent script calls `PM.register({ id,
match(pathname), render(route), onEnter, onLeave, onPopState, boot, stats,
statsExtra })` and owns its screens. Agent scripts are served from
`/static/agents/<agent>/<file>`. Routes: `/`, `/board`, `/board/<id>`,
`/ask`, `/ask/<id>`, `/archive`, `/privacy`, `/about`, plus `/spec` (the
specification as plain text) and the `/api/...` calls.

The work on disk: `config/history/`, one JSON file per board topic and per
Ask the vault thread, told apart by `kind`. A topic or a turn keeps the
paths of the pages it was sent, never their text.
`server.history_folder` overrides the folder; the older
`server.threads_folder` is still read.

**An Ask the vault question** runs: `POST .../question` packs the whole
vault with `knowledge.gather_whole`; when everything fits it goes straight
to phase `asking` (one answer call) and the turn is written. When it does
not fit, the ranking call runs first (phase `choosing`, `picker.choose`
with `overflow=True`), the thread waits in phase `picks` until
`POST .../read`, and after the answer come `checking` (`picker.check`),
`reading_more` and `asking` again, up to `ask.max_reads` (2). The turn
records every round (`rounds`), what the ceiling left out (`left`) and
what the check still wanted at the cap (`still_wanted`). `POST
/api/ask/estimate`, without a thread, serves the page list for a question
that has no thread yet. The snapshot's `steps` list is what the page shows
while it runs, and `live` is what the model has written so far.

**A board run**: the clarifier reads the whole vault too, the confirm
screen shows one page list with a tick each, and `_run_board` hands
`run_board` an `on_text(member, text)` so each member's card fills as it
writes. `session.live` carries it; the empty member name is the
consolidation.

## The vault side

- The vault guide `_How this vault works.md` is the specification for
  every page: folders, properties (`kind`, `updated`, `projects`,
  `lead_swimlane`, `affected_swimlanes`, `phases`, `aliases`, `summary`,
  `Part of Decision Board AI`, `status`, `task`), role pages, VPDS task
  pages with six sections, the owner-label mapping, links to role
  headings, Coaching sub-headings. Stick to it exactly; "no errors allowed".
- Latest delivered baseline: `DCDC-reviewed-2026-09-10c.zip` (phases,
  aliases, AI summaries on every page, Coaching sub-headings, guide
  updated). Alex may have edited it since; always ask for the current zip
  before touching the vault.
- `python scripts/run.py enrich` proposes `phases`, `aliases` and
  summaries for new pages and writes only with `--write` after a yes.
- Abbreviations live once, on the Abbreviations page; a page's `aliases`
  hold other names only.
- The page properties still earn their keep with the whole vault read: the
  model uses `kind`, the swim lanes, the phases and the summaries to know
  what it is looking at, and the ranking needs them if the vault ever
  outgrows one read.

## Open points

1. **Ten real evaluation questions from Alex** for `tests/knowledge_eval/`
   (three examples there now). `python scripts/run.py eval-knowledge`.
2. **Which phases the vault page "Maturity Gate Reviews (MP1 to MP7)"**
   should carry: the task table says MP0 to MP10, the page title MP1 to MP7.
3. **The gate baseline in the project status card** reads the rows of the
   project page's gate table when the page has one, and its plain lines
   when it does not. If the baseline lives under a particular heading, ask
   Alex for that heading and narrow the rule to it.
4. **The ceiling** `knowledge.max_read_tokens` (120,000) is a guess at the
   gateway model's context window; nobody has confirmed it. A read that
   fails with a context error means lower it; a vault that outgrows it
   means raise it rather than trim the vault.
5. **The live answer** runs through `opencode serve`. If it ever
   misbehaves, `provider.opencode.mode: "run"` in the local config puts
   every call back on `opencode run` and costs only the live view. What
   the stream carries can be seen with
   `python scripts/run.py probe-stream --mode serve`; the shapes it prints
   are the ones `ai/opencode_server.py` reads. The combined board mode does
   not stream: it writes every entry in one JSON object, so there is no
   single field to follow.
6. Obsidian links use the vault folder's name; `knowledge.vault_name`
   overrides it if the vault is registered under another name.
7. Firefox before about version 90 does not resolve `*.localhost`; the
   fallback `localhost:8765` always works.
8. The Ask agent attaches every KPI page of the project; that block is
   capped at 2,500 tokens and rides on top of the read.
9. Deleting a thread or a piece of the archive uses the browser's plain
   confirm dialog. The archive never prunes itself: everything closed stays
   until Alex deletes it.
10. A reopened topic asks a member again with the vault read fresh, because
    the record keeps paths and not text. The answer can therefore differ
    from the first run if the vault changed. That is deliberate; say so if
    it ever surprises anyone.
11. Parked ideas, not to be started unless Alex asks: an embedding index,
    prompt caching through the gateway, a local small model for the cheap
    calls, a leaner table of contents, a prompt order that keeps the long
    stable part first for caching, a contents view on the status page.

## What a new session should do first

1. Check that `sh tools/precommit.sh` is green and `git log --oneline -3`
   shows the commits this file names.
2. Ask Alex what he wants next. The specification has no outstanding work,
   so the next thing is his: a new agent, the evaluation questions, a vault
   pass, or something not yet written down. A new agent is a folder under
   `src/programmind/agents/` with its module, its prompts and one script
   that calls `PM.register`; nothing in the shell needs to change for it
   except a card on the home page.
3. Write the spec section first, build, run the gate and the walk, commit,
   push, fast-forward main. Report issues and questions at the end of each
   step, as before.
