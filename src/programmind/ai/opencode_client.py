"""OpenCode client behind the ``AiProvider`` interface (spec 3.8).

Spec 3.8 is the contract this module implements, verified against OpenCode
1.18.11 on the target machine. ``opencode run --format json`` prints **JSON
Lines** - one JSON object per line, not a JSON array - so the client parses
line by line rather than feeding the whole of stdout to ``json.loads``.

Spec 3.8 also records the cost characteristic that makes batching (AP-3)
worth keeping: the verification run - a four-word prompt, a two-token
answer - reported 8 025 input tokens of fixed overhead (OpenCode's own
system prompt and tool definitions), charged on every call regardless of
prompt size.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .provider import (
    AiNotConfiguredError,
    AiProvider,
    AiResult,
    TASK_MODEL_KEYS,
    resolve_model,
    token_limit,
)

_STDERR_TRIM = 2000
_LINE_TRIM = 200
# Windows' CreateProcess rejects a command line above 32,767 characters;
# the prompt is passed as one argument, so a large knowledge block can
# reach it. Refuse with a clear message before that happens.
# The prompt travels on standard input (OC-10), never on the command line:
# on Windows the whole command line is capped at 32,767 characters and a
# board call is longer than that (33,033 on 9 September 2026). The one
# positional argument tells the model where the instruction is.
PROMPT_HEADER = "The complete instruction follows on standard input. Follow it exactly."
# A run that exits cleanly with no text is retried once with this in front
# of the prompt (9 September 2026: Kimi K2.7 spent 920 reasoning tokens and
# wrote 0 output tokens, reason "stop" - it thought and said nothing).
EMPTY_RETRY_PREFIX = ("IMPORTANT: your previous attempt at this call produced reasoning but no answer text. "
                      "Write the answer now, in the shape the instruction asks for, and nothing else.\n\n")
_NO_TEXT = "produced no answer text"

# What each thread is waiting on, so a session can stop its own calls when
# Alex goes back (9 September 2026): an ``opencode run`` process, or the
# object that aborts a server-mode session (spec 4.1). Anything with a
# ``kill()`` will do.
_ACTIVE: dict[int, Any] = {}
_STOPPED: set[int] = set()
_ACTIVE_LOCK = threading.Lock()
# The optional flags each opencode binary accepts, probed once per process
# (OC-7): eight member threads must not each spawn `opencode run --help`.
_FLAGS: dict[str, frozenset[str]] = {}
_FLAGS_LOCK = threading.Lock()


def register_call(thread_id: int, stoppable: Any) -> None:
    """What Stop should reach for while this thread waits (spec 4.1)."""
    with _ACTIVE_LOCK:
        _ACTIVE[thread_id] = stoppable


def finish_call(thread_id: int) -> bool:
    """The call is over; says whether ``stop_call`` was used on it."""
    with _ACTIVE_LOCK:
        _ACTIVE.pop(thread_id, None)
        stopped = thread_id in _STOPPED
        _STOPPED.discard(thread_id)
    return stopped


def stop_call(thread_id: int) -> bool:
    """Terminate the opencode process the given thread is waiting on, if
    any. Returns whether there was one. The waiting thread then raises
    ``OpenCodeError("opencode run was stopped")`` instead of a failure."""
    with _ACTIVE_LOCK:
        proc = _ACTIVE.get(thread_id)
        if proc is None:
            return False
        _STOPPED.add(thread_id)
    try:
        proc.kill()
    except OSError:
        return False
    return True
_OPTIONAL_FLAGS = ("--auto", "--dir")


class OpenCodeError(RuntimeError):
    """An ``opencode run`` invocation failed or returned an unusable result.

    Raised rather than swallowed (NFR-8) for every failure mode the client
    recognises: the binary is missing, the run timed out, it exited
    non-zero, its output contains a line that is not valid JSON (OC-1), or
    it produced no ``text`` event at all (OC-5).
    """


# Keys only Decision Board's own configuration has. "server" and "ui" are
# left out on purpose: a legitimate opencode.json may carry them.
_BOARD_CONFIG_KEYS = ("knowledge", "setup", "storage", "runtime")
_CONFIG_REJECTED = ("unrecognized key", "unrecognized_keys", "invalid config", "config file is invalid")


def opencode_config_problem(path: str | Path | None) -> str | None:
    """Why ``path`` cannot serve as OpenCode's configuration, or ``None``
    when it can. Seen 9 September 2026: Decision Board's own
    ``config.local.json`` was handed to OpenCode as OPENCODE_CONFIG, and
    OpenCode refused it with "unrecognized keys: _comment, setup, storage,
    runtime, knowledge, ui" - which the wizard then blamed on the model
    string. The two files share the key ``provider``; the check looks at
    the keys only one of them has."""
    if path in (None, ""):
        return None
    file = Path(str(path)).expanduser()
    if not file.is_file():
        return f"not found: {file}"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return f"cannot be read: {exc}"
    except json.JSONDecodeError as exc:
        return f"is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return "is not a JSON object"
    board_keys = [key for key in _BOARD_CONFIG_KEYS if key in data]
    provider = data.get("provider")
    if board_keys or (isinstance(provider, dict) and isinstance(provider.get("models"), dict)
                      and "board" in provider["models"]):
        return (f"is Decision Board's own configuration (keys {', '.join(board_keys) or 'provider.models.board'}), "
                "not an opencode.json. OpenCode needs the file from IT that defines the provider and the gateway "
                "(keys: $schema, provider, model)")
    if not any(key in data for key in ("provider", "model", "$schema", "mcp", "agent")):
        return "defines no provider and no model - OpenCode would run on its own defaults, not on the gateway"
    return None


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def describe_failure(stdout: str | None, stderr: str | None) -> str:
    """What a failed ``opencode run`` actually said. With ``--format json``
    OpenCode reports errors on **stdout**, as JSON events, and often writes
    nothing to stderr at all - seen on the target machine on 8 September
    2026 as an error message that ended after the exit code. So the
    message is assembled from every ``error``-like event's text, then from
    whatever else stdout and stderr carry, never from stderr alone."""
    messages: list[str] = []
    other_lines: list[str] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            other_lines.append(line.strip())
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        if "error" in event_type.lower():
            messages.append(_error_text(event))
        elif event_type == "text":
            other_lines.append(str(event.get("part", {}).get("text", "")).strip())
    stderr_text = (stderr or "").strip()
    parts = [m for m in messages if m]
    if stderr_text:
        parts.append(stderr_text[:_STDERR_TRIM])
    if not parts and other_lines:
        parts.append(" ".join(other_lines)[:_STDERR_TRIM])
    if not parts:
        return "(no output - run `opencode auth list` and `opencode models` to check login and model name)"
    text = " | ".join(parts)
    if any(marker in text.lower() for marker in _CONFIG_REJECTED):
        text += (" - OpenCode rejected its configuration file: OPENCODE_CONFIG (Options: OpenCode configuration "
                 "file) must point at the opencode.json from IT, not at Decision Board's config.local.json")
    return text


def _error_text(event: dict) -> str:
    """The human-readable part of an error event, whatever nesting the
    OpenCode version used."""
    for key in ("message", "error", "part", "data", "properties"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = _error_text(value)
            if inner:
                return inner
    name = event.get("name")
    return str(name) if name else json.dumps(event, ensure_ascii=False)[:_LINE_TRIM]


def opencode_environment(config: dict) -> dict[str, str]:
    """The environment ``opencode`` is started with (OC-8). Identical to the
    server's own, plus ``OPENCODE_CONFIG`` when ``provider.opencode.config_file``
    names an ``opencode.json`` - the way a company-provided provider
    definition kept outside the repository (the LiteLLM gateway, decided
    8 September 2026) reaches OpenCode regardless of the working directory
    the board runs from. An empty setting leaves OpenCode's own lookup
    (global config, then the working directory) untouched.

    Every variable of the server process is inherited, including ones
    unrelated to OpenCode: that is what ``{env:NAME}`` placeholders in an
    opencode.json need, and the price is that a secret in the environment
    is visible to the opencode process too.
    """
    env = dict(os.environ)
    config_file = _config_key(config, "provider.opencode.config_file", "")
    if config_file:
        env["OPENCODE_CONFIG"] = str(Path(str(config_file)).expanduser())
    return env


def _config_key(config: dict, dotted: str, default: Any = None) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


class OpenCodeProvider(AiProvider):
    """Runs a task through the ``opencode run`` CLI (spec 3.8)."""

    def __init__(
        self,
        config: dict,
        *,
        binary: str = "opencode",
        cwd: Path | str | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self._config = config
        self._binary = binary
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._supported: frozenset[str] | None = None

    def complete(self, task: str, prompt: str, on_text: Callable[[str], None] | None = None) -> AiResult:
        """Runs ``task`` through ``opencode run`` and returns its ``AiResult``.

        ``on_text`` is ignored here: a run hands its answer over in one
        piece when it is done, which is why spec 4.1 has the server mode.

        AI-1's configured token limit (``provider.token_limits.<key>``) is
        observed here, never enforced: nothing is truncated and the run is
        never aborted for exceeding it - a run that did its work must not be
        thrown away for going over a budget, and OpenCode's own flag for
        capping tokens is unverified, so passing one would be a guess. What
        the limit buys is that going over it is visible afterwards, in
        ``AiResult.over_token_limit`` and from there in the Audit Log.
        """
        model_string = resolve_model(self._config, task)
        if model_string is None:
            key = TASK_MODEL_KEYS.get(task, task)
            raise AiNotConfiguredError(
                f"task {task!r} has no model configured - set provider.models.{key}"
            )

        command = self._build_command(model_string)
        try:
            stdout, _duration = self._run(command, prompt)
            text, input_tokens, output_tokens = self._parse_output(stdout)
        except OpenCodeError as first:
            if _NO_TEXT not in str(first):
                raise
            # One retry, same prompt with a nudge in front (OC-11). A second
            # empty run is reported with both attempts named.
            try:
                stdout, _duration = self._run(command, EMPTY_RETRY_PREFIX + prompt)
                text, input_tokens, output_tokens = self._parse_output(stdout)
            except OpenCodeError as second:
                raise OpenCodeError(f"{second} (this was the retry; the first attempt: {first})") from second

        parts = model_string.split("/", 1)
        provider_name, model_name = parts if len(parts) == 2 else ("", parts[0])
        result = AiResult(
            text=text,
            provider=provider_name,
            model=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=_duration,
        )

        limit = token_limit(self._config, task)
        if limit is not None and result.total_tokens is not None and result.total_tokens > limit:
            result = AiResult(
                text=result.text,
                provider=result.provider,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_seconds=result.duration_seconds,
                over_token_limit=True,
            )
        return result

    def _supported_flags(self) -> frozenset[str]:
        """Which of the optional flags this OpenCode version accepts, read
        once from ``opencode run --help`` (OC-7). A version that does not
        know a flag prints its usage text and exits 1 instead of running -
        seen on the target machine on 8 September 2026, where ``--auto``
        was not in the list - so a flag is passed only when the installed
        binary lists it. If the probe itself fails, no optional flag is
        passed and the run proceeds on the flags every version has."""
        if self._supported is None:
            with _FLAGS_LOCK:
                cached = _FLAGS.get(self._binary)
                if cached is None:
                    try:
                        probe = subprocess.run(
                            [self._binary, "run", "--help"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60,
                            env=opencode_environment(self._config),
                        )
                        help_text = (probe.stdout or "") + (probe.stderr or "")
                    except (OSError, subprocess.TimeoutExpired):
                        help_text = ""
                    cached = frozenset(flag for flag in _OPTIONAL_FLAGS if flag in help_text)
                    _FLAGS[self._binary] = cached
            self._supported = cached
        return self._supported

    def _build_command(self, model_string: str) -> list[str]:
        """The command line without the prompt: the prompt goes to ``_run``
        as standard input (OC-10)."""
        command = [self._binary, "run", "--format", "json", "--model", model_string]
        supported = self._supported_flags()
        if self._cwd is not None and "--dir" in supported:
            command += ["--dir", str(self._cwd)]
        # Auto-approval defaults to True: a headless run that stops to ask
        # for permission would hang (OC-6). The prompts tell the model it
        # has no tools and needs none; what --auto would approve is whatever
        # file or shell tool OpenCode offers in its working directory, which
        # is why the board never runs OpenCode inside the vault. The flag is
        # only passed when the installed version accepts it (OC-7).
        auto_approve = _config_key(self._config, "provider.opencode.auto_approve", True)
        if auto_approve and "--auto" in supported:
            command += ["--auto"]
        # Anything else this OpenCode version needs, without a code change
        # (OC-9): e.g. ["--agent", "plan"] to stop the run using tools.
        extra = _config_key(self._config, "provider.opencode.extra_args", []) or []
        if isinstance(extra, list):
            command += [str(item) for item in extra]
        command.append(PROMPT_HEADER)
        return command

    def _run(self, command: list[str], prompt: str) -> tuple[str, float]:
        """Run ``command`` with ``prompt`` on standard input. ``opencode run``
        appends piped input to its message, so the model receives the
        header from the command line followed by the whole prompt."""
        started = time.monotonic()
        try:
            # OpenCode writes UTF-8; without saying so, Windows decodes it as
            # cp1252 and a German umlaut in a member's answer becomes mojibake
            # or, for some byte values, a UnicodeDecodeError that takes the
            # run down.
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=opencode_environment(self._config),
            )
            tid = threading.get_ident()
            with _ACTIVE_LOCK:
                _ACTIVE[tid] = proc
            try:
                out, err = proc.communicate(input=prompt, timeout=self._timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass          # a grandchild holding the pipes must not hang the board
                raise
            finally:
                with _ACTIVE_LOCK:
                    _ACTIVE.pop(tid, None)
                    stopped = tid in _STOPPED
                    _STOPPED.discard(tid)
            if stopped:
                raise OpenCodeError("opencode run was stopped")
            result = subprocess.CompletedProcess(command, proc.returncode, out, err)
        except FileNotFoundError as exc:
            raise OpenCodeError(
                f"{self._binary!r} is not on PATH - OpenCode must be installed on this "
                "machine (OP-4)"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise OpenCodeError(
                f"opencode run did not finish within {self._timeout_seconds} seconds"
            ) from exc
        duration = time.monotonic() - started

        if result.returncode != 0:
            raise OpenCodeError(
                f"opencode run exited with code {result.returncode}: "
                + describe_failure(result.stdout, result.stderr)
            )
        return result.stdout, duration

    def _parse_output(self, stdout: str) -> tuple[str, int | None, int | None]:
        text_parts: list[str] = []
        input_tokens: int | None = None
        output_tokens: int | None = None
        saw_text = False
        seen: dict[str, int] = {}
        tool_names: list[str] = []
        errors: list[str] = []
        finishes: list[str] = []

        for line_number, line in enumerate(stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                snippet = line if len(line) <= _LINE_TRIM else line[:_LINE_TRIM] + "..."
                raise OpenCodeError(
                    f"opencode run produced invalid JSON on line {line_number}: {snippet}"
                ) from exc

            event_type = str(event.get("type", "?"))
            seen[event_type] = seen.get(event_type, 0) + 1
            part = event.get("part") if isinstance(event.get("part"), dict) else {}

            # OC-9: the answer is any text-carrying part, whatever the event
            # is called. 'text' is the shape spec 3.8 verified; a version
            # that wraps the same part in another event (part.type == "text")
            # is read the same way rather than reported as "no answer".
            if event_type == "text" or part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    saw_text = True
                    text_parts.append(text)
            elif event_type == "step_finish":
                tokens = part.get("tokens")
                if not isinstance(tokens, dict):
                    tokens = {}
                step_input = _int_or_none(tokens.get("input"))
                step_output = _int_or_none(tokens.get("output"))
                if step_input is not None:
                    input_tokens = (input_tokens or 0) + step_input
                if step_output is not None:
                    output_tokens = (output_tokens or 0) + step_output
                finish = (f"reason '{part.get('reason', '?')}', {tokens.get('reasoning') or 0} reasoning tokens, "
                          f"{step_output or 0} output tokens")
                finishes.append(finish)
            elif "error" in event_type.lower():
                errors.append(_error_text(event))
            elif event_type == "tool" or part.get("type") == "tool":
                name = part.get("tool") or part.get("name") or (part.get("state") or {}).get("title")
                if name:
                    tool_names.append(str(name))
            # every other type is ignored entirely (OC-3)

        if not saw_text:
            raise OpenCodeError(self._no_text_message(stdout, seen, tool_names, errors, finishes))

        return "".join(text_parts), input_tokens, output_tokens

    def _no_text_message(self, stdout: str, seen: dict[str, int], tool_names: list[str],
                         errors: list[str] | None = None, finishes: list[str] | None = None) -> str:
        """Why a run that exited cleanly has no answer in it (OC-9). Names
        the events the run did produce, the tools it called, and the file the
        raw output was written to - a run that answered nothing is otherwise
        indistinguishable from a run that was never made."""
        events = ", ".join(f"{name} x{count}" for name, count in sorted(seen.items())) or "none at all"
        parts = [f"opencode run produced no answer text. Events it did produce: {events}."]
        reported = [message for message in (errors or []) if message]
        if reported:
            # A run can report an error and still exit 0; that message is the
            # answer to "why is there nothing here".
            parts.append("It reported: " + " | ".join(dict.fromkeys(reported)) + ".")
        if finishes and not tool_names:
            parts.append("The model finished with " + "; ".join(finishes) +
                         ": it thought and wrote nothing.")
        if tool_names:
            parts.append(
                f"It called tool(s) instead of answering: {', '.join(dict.fromkeys(tool_names))}. "
                "Set provider.opencode.extra_args in the configuration to pin a non-agentic agent "
                "(for example [\"--agent\", \"plan\"]) if this repeats."
            )
        path = self._save_raw(stdout)
        if path is not None:
            parts.append(f"Raw output: {path}")
        return " ".join(parts)

    def _save_raw(self, stdout: str) -> Path | None:
        """The run's raw JSON Lines, under ``<audit_folder>/opencode-debug``.
        Best effort: a diagnostic that cannot be written must not replace the
        error it was meant to explain."""
        folder = _config_key(self._config, "runtime.audit_folder")
        if not folder:
            return None
        try:
            target = Path(str(folder)) / "opencode-debug"
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{threading.get_ident()}.jsonl"
            path.write_text(stdout or "", encoding="utf-8")
            return path
        except OSError:
            return None
