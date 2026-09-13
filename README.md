# Program Mind

Program Mind is a local site that hosts AI agents on your Obsidian vault.
The vault is the point: every agent reads the same folder of Markdown
notes, answers from what the notes say, tells you which notes it used, and
writes into the vault only after you have confirmed a note. Two agents
exist today:

* **Board.** You give it a decision. It asks what it needs to know, then
  every board member answers from its own professional perspective, each in
  a model call that cannot see any other member's answer. One more call
  consolidates them into one direction, you ask follow-ups against it, and
  when you close the topic the board offers to write the decision into the
  vault. Who sits on the board is decided by role profiles in the vault, not
  by code.
* **Ask the vault.** Any question, answered by one agent from the vault, in a
  thread you can ask back in. Every answer names the pages it used, says what
  the pages did not hold, and never invents a rule, a date or an owner.

Both agents work the same way underneath:

* **The whole vault is read for every question.** Every page of the project,
  whole, for Ask the vault and for every board member alike, as long as the
  whole fits one read. Nothing is ranked, nothing is guessed at, and no call
  is spent choosing what to read.
* **You see the pages before they are read.** Under the question box, one
  folded line lists them with a tick each. Untick one and it stays out of
  that read. Asking then reads and answers in one step.
* **The answer appears as it is written**, the way a chat does. On the board
  each member writes in its own card. What you watch is for the eye only:
  the answer that is kept, its sources and every check come from the
  finished call.

The full specification, including every decision and the reason behind it,
is `docs/spec.md`. It opens with what the program does today and where each
piece was decided. `docs/handover.md` is the working note for whoever picks
the project up next.

## Start here

Three commands on any machine, Windows, macOS or Linux:

```
git clone https://github.com/AlexanderHultsch/program-mind.git
cd program-mind
python scripts/setup.py
```

The third one is not optional. Program Mind has no usable defaults: it does
not know which model you may call, where your notes are, or who sits on your
board. That belongs in `config/config.local.json`, which is deliberately not
in this repository. It holds paths from your machine and, in a company
setup, the endpoint your IT approved. Every clone starts without it, and the
setup wizard writes it. Run the wizard again whenever something changes; it
keeps what is set and saves the previous version as `config.local.json.bak`.

Then start the site:

```
python scripts/run.py serve
```

It listens on `127.0.0.1` only and opens `http://program-mind.localhost:8765/`
in your browser (`http://localhost:8765/` works too). The name is
`server.site_name`: a plain name is used under `.localhost`, which resolves
to your own machine without admin rights, and a name with a dot in it
(`program-mind.local`) is used as it stands, but you have to make that one
resolve yourself. The home page holds the project picker, one
card per agent and the open work of every agent. The top bar shows the
project and three status lights (vault, AI, project; hover for the detail);
the menu at the right holds Options, Statistics, the vault in Obsidian, the
Archive, Privacy, About and the link to report a bug. Hovering a status chip
shows its detail; clicking one opens the status page. Inside the board a step
line (Question, Clarify, Confirm, Result) goes back to any earlier step with
everything typed kept. Options changes the knowledge source, the model, the
audit folder or the theme.

The command line is still there for the board:

```
python scripts/run.py board
```

## Requirements

Python 3.11 or newer and git. The standard library only, no `pip`
dependencies, no build step for the page. You also need
[OpenCode](https://opencode.ai) on the machine; the wizard prints the install
command for your platform if it is missing.

For the answer to appear as it is written, Program Mind keeps one
`opencode serve` process beside itself and reads its event stream
(`provider.opencode.mode`, `auto` by default). If that server will not
start, every call falls back to one `opencode run` and only the live view
is lost. `python scripts/run.py probe-stream --mode serve` shows what your
OpenCode streams.

The wizard asks first how you will use the site:

* **Company or organisation.** Your IT provides an `opencode.json` that points
  OpenCode at an internal gateway. Prompts and notes stay on approved
  infrastructure, and the file carries the key, so there is nothing to log
  in to. Its path is stored as `provider.opencode.config_file` and passed to
  every OpenCode call as `OPENCODE_CONFIG`.
* **Private or your own account.** OpenCode logs in to a provider you choose.
  Your question and the notes an agent selects are sent to that provider, so
  point the knowledge source at a vault you may share with it.

The wizard checks Python, git and OpenCode, writes the configuration, lets
you choose the vault with the native folder dialog, and makes two real test
calls to the model so you know it is reachable before anything is asked.
Non-interactive use: `python scripts/setup.py --yes --profile private
--vault "/path/to/vault"`. On a machine without the repository,
`scripts/install.ps1` clones it first and then runs the wizard.

## The vault

Every agent reads the `.md` files under the configured vault folder for each
question, whole, with the KPI pages and the date their numbers were checked.
One ceiling stands over a single call, `knowledge.max_read_tokens`
(120,000 by default), so a call cannot fail at the model's context limit;
set it to what your model can take. The pages get that figure less a fixed
15,000 tokens, which is what a call carries besides them: the role
profile, the conduct note, the KPI numbers, the prompt itself and the
gateway's own overhead. A vault that cannot be read stops the
run with an error; there is no fallback source.

If a vault ever outgrows that ceiling, the older machinery takes over and
only then: the model reads the vault's table of contents and ranks the
pages, the read fills from the top until the ceiling, a check after the
answer swaps in pages that were left out, and the board falls back to one
ranked selection per member with its slider. The screen says which of the
two is happening.

Nothing is written to the vault without your confirmation. When you close a
topic or a thread the agent proposes a note and a place for it; you edit and
confirm, or do not write. The work itself is kept as one JSON file per topic
and per thread under `config/history/`, never in the vault: the home page
lists what is open, the Archive in the menu holds what you have closed, and
both can be reopened to read. A topic keeps the paths of the vault pages it
was sent, never their text.

The role profiles the board reads live in a folder of the vault whose name
starts with "Roles", or any folder you choose in Options: one note per
member, or one note with several members, plus one conduct note that is the
same for every member. The repository ships no members, only the conduct
note and the templates the wizard installs. See `roles/README.md`.

Three helpers on the command line:

```
python scripts/run.py enrich          # phases, aliases and AI summaries on the pages, after a yes
python scripts/run.py eval-knowledge  # hit rate of the ranked selection over tests/knowledge_eval/
python scripts/run.py probe-stream    # can this OpenCode hand over the text as it is written?
```

## How the code is laid out

One shared shell, one folder per agent, on both sides:

```
src/programmind/
  shell/        the server and the API routing
  knowledge/    the vault: reading it whole, the ranking behind the ceiling, enrichment, evaluation
  memory/       the memory step, the audit trail, the work kept on disk
  ai/           the model provider, the two OpenCode clients, the prompt loader
  agents/
    board/      board.py, clarify.py, roles.py, prompts/, web/board.js
    ask/        ask.py, prompts/ask.md, web/ask.js
  web/          index.html, shell.js, style.css
```

The shell owns the top bar, the project picker, the options and statistics
dialogs, the proposal screen and the routes. Each agent is one Python module
with its prompts and one script that registers itself with the shell
(`PM.register`) and owns its own screens. A new agent is a new folder under
`agents/`, one script tag in `index.html` and its routes in the server;
nothing else changes.

## Tests

```
python -m unittest discover -s tests
```

One suite for everything: the knowledge selection, the board, the clarifier,
the memory step, Ask the vault, the live answer over OpenCode's server mode,
the server and its routes, the setup wizard. No test calls a model: a fake
provider answers by the markers each prompt carries, and a faked OpenCode
server answers the streaming client.

Before a commit, `sh tools/precommit.sh` runs the suite and checks that the
example configuration still parses. The browser walk drives the whole site
against a fake model in a real browser:

```
python3 tools/browser_walk/demo_server.py                 # in one terminal
node tools/browser_walk/walk.js                           # in another, from a folder it may write shots/ into
```
