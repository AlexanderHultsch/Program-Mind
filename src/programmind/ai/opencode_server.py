"""Program Mind through ``opencode serve`` (spec 4.1, 13 September 2026).

``opencode run`` hands the answer over in one piece when the call is done,
so the page can only turn a circle while the model writes. The server mode
streams: one process for as long as Program Mind runs, one OpenCode session
per call, and an event stream that carries the answer as it is written.

Two things are kept apart on purpose (4.1, decision 3). **The answer** is
what the call returns at the end - parsed, checked and stored exactly as
the run's answer is. **The live text** is what the event stream happened to
show while the call ran; it is handed to a callback for the page and to
nothing else. A stream that says nothing, or says something odd, costs the
live view and never an answer.

The process is started in an empty folder of its own (4.1, decision 5):
OpenCode's built-in tools reach the working directory, and the probe's
session ran in the repository.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from programmind.ai.opencode_client import (
    EMPTY_RETRY_PREFIX,
    _NO_TEXT,
    OpenCodeError,
    _config_key,
    finish_call,
    opencode_environment,
    register_call,
)
from programmind.ai.provider import (
    TASK_MODEL_KEYS,
    AiNotConfiguredError,
    AiProvider,
    AiResult,
    resolve_model,
    token_limit,
)

START_TIMEOUT = 45.0          # seconds to wait for the server to answer on its port
CALL_TIMEOUT = 900.0          # seconds for one question; the whole vault in one call is slow
_SERVER_LINES = 40            # of the server's own output, kept for an error message
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _unwrap(value: Any) -> Any:
    """``{"data": {...}}`` is how this OpenCode wraps an answer (13 September
    2026); an older one is the object itself."""
    if isinstance(value, dict) and "id" not in value and isinstance(value.get("data"), dict):
        return value["data"]
    return value


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _get(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace") or "null")


def _post(url: str, body: dict, timeout: float) -> Any:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise OpenCodeError(f"{url} answered {exc.code}: {detail}") from exc


def _delete(url: str, timeout: float = 10.0) -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="DELETE"), timeout=timeout).close()
    except Exception:
        pass          # a session that will not be deleted is the server's housekeeping, not the answer's


class _Routes:
    """Which paths this OpenCode version really has, read once from its own
    ``/doc`` (13 September 2026: the routes that work live under ``/api``
    for some calls and not for others, so neither prefix is assumed)."""

    def __init__(self, base: str) -> None:
        paths: dict[str, Any] = {}
        try:
            spec = _get(f"{base}/doc", timeout=10)
            if isinstance(spec, dict) and isinstance(spec.get("paths"), dict):
                paths = spec["paths"]
        except Exception:
            paths = {}
        # ``/session/{sessionID}`` and ``/session/{id}`` are the same route:
        # every placeholder is read as ``{id}`` on both sides of the match.
        have = {(method.upper(), _PLACEHOLDER.sub("{id}", path)) for path, methods in paths.items()
                for method in (methods if isinstance(methods, dict) else {})}

        def pick(method: str, *candidates: str) -> str:
            for candidate in candidates:
                if not have or (method, candidate) in have:
                    return candidate
            return candidates[0]

        self.session = pick("POST", "/api/session", "/session")
        self.message = pick("POST", "/session/{id}/message", "/api/session/{id}/message")
        self.event = pick("GET", "/api/event", "/event")
        self.abort = pick("POST", "/session/{id}/abort", "/api/session/{id}/abort")
        self.delete = pick("DELETE", "/session/{id}", "/api/session/{id}")
        self.messages = pick("GET", "/session/{id}/message", "/api/session/{id}/message")

    @staticmethod
    def fill(route: str, session_id: str) -> str:
        return route.replace("{id}", session_id).replace("{sessionID}", session_id)


class OpenCodeServer:
    """The one ``opencode serve`` process, started when a call first needs
    it and stopped with the program."""

    def __init__(self, config: dict, *, binary: str | None = None, start_timeout: float = START_TIMEOUT) -> None:
        self._config = config
        self._binary = binary or str(_config_key(config, "provider.opencode.binary", "opencode") or "opencode")
        self._start_timeout = start_timeout
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._base: str | None = None
        self._workdir: Path | None = None
        self._lines: list[str] = []
        self.routes: _Routes | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def base(self) -> str:
        """The server's address, starting it if it is not up. Raises
        ``OpenCodeError`` with the server's own output when it will not
        start - the caller decides whether to fall back to the run."""
        with self._lock:
            if self.running and self._base:
                return self._base
            self._start()
            assert self._base is not None
            return self._base

    def _start(self) -> None:
        self.stop()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        self._workdir = Path(tempfile.mkdtemp(prefix="programmind-opencode-"))
        base = f"http://127.0.0.1:{port}"
        try:
            proc = subprocess.Popen(
                [self._binary, "serve", "--port", str(port)], cwd=str(self._workdir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", env=opencode_environment(self._config),
            )
        except FileNotFoundError as exc:
            raise OpenCodeError(f"{self._binary!r} is not on PATH - OpenCode must be installed on this machine") from exc
        self._proc = proc
        self._lines = []
        threading.Thread(target=self._read_output, args=(proc,), daemon=True).start()
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise OpenCodeError(f"opencode serve stopped at once: {self._output() or '(no output)'}")
            try:
                _get(f"{base}/doc", timeout=2)
                self._base = base
                self.routes = _Routes(base)
                return
            except Exception:
                time.sleep(0.3)
        self.stop()
        raise OpenCodeError(f"opencode serve did not answer on {base} within {self._start_timeout:.0f} s: "
                            f"{self._output() or '(no output)'}")

    def _read_output(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout or []:
            with self._lock:
                self._lines.append(line.rstrip())
                del self._lines[:-_SERVER_LINES]

    def _output(self) -> str:
        with self._lock:
            return " | ".join(self._lines[-6:])

    def stop(self) -> None:
        with self._lock:
            proc, workdir = self._proc, self._workdir
            self._proc, self._base, self._workdir, self.routes = None, None, None, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


_SHARED: OpenCodeServer | None = None
_SHARED_LOCK = threading.Lock()


def shared_server(config: dict) -> OpenCodeServer:
    """One server for the whole program (the shell builds a provider per
    call, so the process cannot hang off a provider instance)."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = OpenCodeServer(config)
        return _SHARED


def stop_shared() -> None:
    global _SHARED
    with _SHARED_LOCK:
        server, _SHARED = _SHARED, None
    if server is not None:
        server.stop()


class _Live:
    """What the event stream says the model has written so far (4.1,
    decision 4): the assistant's own ``text`` parts, in the order they
    appeared. The prompt comes back as a part of the user's message and the
    reasoning as a part of the model's; neither is the answer."""

    def __init__(self, session_id: str, prompt: str, on_text: Callable[[str], None] | None) -> None:
        self.session_id = session_id
        self._head = " ".join(prompt.split())[:120]
        self._on_text = on_text
        self._lock = threading.Lock()
        self._roles: dict[str, str] = {}          # message id -> role
        self._text_parts: set[str] = set()        # part ids known to be the assistant's answer text
        self._order: list[str] = []
        self._parts: dict[str, str] = {}
        self.kinds: dict[str, int] = {}           # event types seen, for an error that has to say what happened
        self.text = ""

    def _is_echo(self, text: str) -> bool:
        return bool(self._head) and " ".join(text.split())[:120] == self._head

    def feed(self, event: dict) -> None:
        kind = str(event.get("type") or "?")
        with self._lock:
            self.kinds[kind] = self.kinds.get(kind, 0) + 1
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not isinstance(data, dict):
            return
        if data.get("sessionID") not in (None, self.session_id):
            return                                 # another session on the same server
        if kind == "message.updated":
            info = data.get("info") if isinstance(data.get("info"), dict) else {}
            if info.get("id"):
                with self._lock:
                    self._roles[str(info["id"])] = str(info.get("role") or "")
            return
        if not kind.startswith("message.part."):
            return
        part = data.get("part") if isinstance(data.get("part"), dict) else {}
        part_id = str(part.get("id") or data.get("partID") or "")
        if not part_id:
            return
        message_id = str(part.get("messageID") or data.get("messageID") or "")
        if kind == "message.part.delta":
            delta = data.get("delta")
            if isinstance(delta, dict):
                delta = delta.get("text")
            with self._lock:
                if isinstance(delta, str) and delta and part_id in self._text_parts:
                    self._parts[part_id] = self._parts.get(part_id, "") + delta
                    self._emit()
                    return
            if not isinstance(part.get("text"), str):
                return                             # a delta of something else, or a shape we do not know
        text = part.get("text")
        if not isinstance(text, str):
            return
        with self._lock:
            role = self._roles.get(message_id, "")
            if role == "user" or (part_id not in self._text_parts and self._is_echo(text)):
                return                             # the prompt coming back
            if part.get("type") not in (None, "text"):
                return                             # reasoning, a tool call, a step marker
            self._text_parts.add(part_id)
            if part_id not in self._parts:
                self._order.append(part_id)
            self._parts[part_id] = text            # the whole part so far, each event replacing the last
            self._emit()

    def _emit(self) -> None:
        """Under the lock."""
        text = "".join(self._parts.get(part_id, "") for part_id in self._order)
        if text == self.text:
            return
        self.text = text
        if self._on_text is not None:
            try:
                self._on_text(text)
            except Exception:
                pass          # the page's business, never the call's


class _Abort:
    """What ``stop_call`` reaches for when Alex presses Stop: the session,
    not the process - the process is shared by every call."""

    def __init__(self, base: str, route: str) -> None:
        self._url = base + route

    def kill(self) -> None:
        try:
            _post(self._url, {}, timeout=10)
        except Exception as exc:
            raise OSError(str(exc)) from exc


class OpenCodeServerProvider(AiProvider):
    """One call through the running server, with the answer as the call
    returns it and the live text as the stream showed it."""

    def __init__(self, config: dict, *, server: OpenCodeServer | None = None, base_url: str | None = None,
                 timeout_seconds: float = CALL_TIMEOUT, fallback: AiProvider | None = None) -> None:
        self._config = config
        self._server = server
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._fallback = fallback

    def _base(self) -> tuple[str, _Routes]:
        if self._base_url is not None:
            return self._base_url, _Routes(self._base_url)
        server = self._server or shared_server(self._config)
        base = server.base()
        return base, (server.routes or _Routes(base))

    def complete(self, task: str, prompt: str, on_text: Callable[[str], None] | None = None) -> AiResult:
        model_string = resolve_model(self._config, task)
        if model_string is None:
            key = TASK_MODEL_KEYS.get(task, task)
            raise AiNotConfiguredError(f"task {task!r} has no model configured - set provider.models.{key}")
        try:
            base, routes = self._base()
        except OpenCodeError:
            if self._fallback is None:
                raise
            return self._fallback.complete(task, prompt)    # 4.1, decision 6: the run carries on without a live view
        started = time.monotonic()
        try:
            text, input_tokens, output_tokens = self._call(base, routes, model_string, prompt, on_text)
        except OpenCodeError as first:
            if _NO_TEXT not in str(first):
                raise
            try:                                            # OC-11: one retry with a nudge, as the run has
                text, input_tokens, output_tokens = self._call(base, routes, model_string,
                                                               EMPTY_RETRY_PREFIX + prompt, on_text)
            except OpenCodeError as second:
                raise OpenCodeError(f"{second} (this was the retry; the first attempt: {first})") from second
        provider_name, _, model_name = model_string.partition("/")
        if not model_name:
            provider_name, model_name = "", provider_name
        result = AiResult(text=text, provider=provider_name, model=model_name, input_tokens=input_tokens,
                          output_tokens=output_tokens, duration_seconds=time.monotonic() - started)
        limit = token_limit(self._config, task)
        if limit is not None and result.total_tokens is not None and result.total_tokens > limit:
            result = AiResult(text=result.text, provider=result.provider, model=result.model,
                              input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                              duration_seconds=result.duration_seconds, over_token_limit=True)
        return result

    def _call(self, base: str, routes: _Routes, model_string: str, prompt: str,
              on_text: Callable[[str], None] | None) -> tuple[str, int | None, int | None]:
        session = _unwrap(_post(base + routes.session, {}, timeout=60))
        session_id = str(session.get("id") or "") if isinstance(session, dict) else ""
        if not session_id:
            raise OpenCodeError(f"opencode serve opened no session: {str(session)[:200]}")
        live = _Live(session_id, prompt, on_text)
        stream = _Stream(base + routes.event, live)
        stream.start()
        provider_id, _, model_id = model_string.partition("/")
        body = {"model": {"providerID": provider_id, "modelID": model_id},
                "parts": [{"type": "text", "text": prompt}]}
        tid = threading.get_ident()
        register_call(tid, _Abort(base, _Routes.fill(routes.abort, session_id)))
        try:
            answer = _post(base + _Routes.fill(routes.message, session_id), body, timeout=self._timeout)
        finally:
            stopped = finish_call(tid)
            stream.stop()
            _delete(base + _Routes.fill(routes.delete, session_id))
        if stopped:
            raise OpenCodeError("opencode was stopped")
        text, input_tokens, output_tokens = _answer_of(answer)
        if not text:
            # The answer may live on the message rather than in the reply.
            try:
                messages = _unwrap(_get(base + _Routes.fill(routes.messages, session_id), timeout=30))
            except Exception:
                messages = None
            for message in reversed(messages if isinstance(messages, list) else []):
                text, input_tokens, output_tokens = _answer_of(_unwrap(message))
                if text:
                    break
        if not text:
            seen = ", ".join(f"{k} x{v}" for k, v in sorted(live.kinds.items())) or "no events"
            raise OpenCodeError(f"the call {_NO_TEXT} (opencode serve; the event stream carried {seen})")
        return text, input_tokens, output_tokens


def _answer_of(answer: Any) -> tuple[str, int | None, int | None]:
    body = _unwrap(answer)
    if not isinstance(body, dict):
        return "", None, None
    parts = body.get("parts") if isinstance(body.get("parts"), list) else []
    texts = [part.get("text") for part in parts
             if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)]
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    return "".join(texts).strip(), _int(tokens.get("input")), _int(tokens.get("output"))


class _Stream:
    """The event stream of one call, read in a thread of its own.

    A plain socket rather than ``urllib``: the call has to be able to drop
    the stream the moment it is done, and closing an ``HTTPResponse`` while
    another thread reads it blocks both (seen 13 September 2026). Shutting
    the socket down wakes the reader instead.
    """

    def __init__(self, url: str, live: _Live) -> None:
        parts = urlsplit(url)
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 80
        self._path = parts.path or "/"
        self._live = live
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            sock = socket.create_connection((self._host, self._port), timeout=10)
        except OSError:
            return                # no live view; the call carries on
        request = (f"GET {self._path} HTTP/1.1\r\nHost: {self._host}:{self._port}\r\n"
                   "Accept: text/event-stream\r\nConnection: close\r\n\r\n")
        try:
            sock.sendall(request.encode("ascii"))
        except OSError:
            sock.close()
            return
        sock.settimeout(None)
        self._sock = sock
        self._thread = threading.Thread(target=self._read, args=(sock,), daemon=True)
        self._thread.start()
        time.sleep(0.05)          # let the stream be open before the question goes out

    def _read(self, sock: socket.socket) -> None:
        try:
            fp = sock.makefile("rb")
            chunked = False
            while True:                                   # the response headers
                line = fp.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                low = line.lower()
                if low.startswith(b"transfer-encoding:") and b"chunked" in low:
                    chunked = True
            buffer = b""
            while not self._stop.is_set():
                if chunked:
                    head = fp.readline()
                    if not head:
                        return
                    token = head.strip().split(b";")[0]
                    if not token:
                        continue
                    try:
                        size = int(token, 16)
                    except ValueError:
                        return
                    if size == 0:
                        return
                    data = fp.read(size)
                    fp.read(2)                            # the chunk's own line break
                else:
                    data = fp.read1(65536)
                if not data:
                    return
                buffer += data
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    self._line(raw)
        except Exception:
            return                # a stream that fails costs the live view, never the answer

    def _line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            return
        if isinstance(event, dict):
            self._live.feed(event)

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
