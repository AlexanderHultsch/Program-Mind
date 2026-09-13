"""Can OpenCode give us the model's text as it arrives? (11 September 2026)

A live answer on the page - the text growing while the model writes, the
way a chat does - needs the text before the call ends. ``opencode run
--format json`` does not give it: probed on Alex's machine, one text event
arrived at 6.8 s of a 7.8 s run, the whole answer in one piece. Two ways
are left, and this module tries both with the same configuration, the same
environment and the same model as every real call:

- ``plain``: ``opencode run`` without ``--format json``. If the text comes
  out as it is written, the answer call can stream and nothing else in the
  program has to change but the shape of the answer.
- ``serve``: ``opencode serve`` and its event stream. A bigger change - a
  server process, a session per call - but the way a chat does it.

Nothing here is used by the agents. It is run by hand, prints what it sees
with the seconds since the start, and says what it found.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

LINE = 74                 # how much of a line or a payload is printed
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROMPT = ("Count from 1 to 40, one number per line, and write one short sentence about the weather "
          "after every ten numbers.")


def answer_text(chunk: str) -> str:
    """What of a chunk of plain output is the model's answer: no colour
    codes, and not OpenCode's own banner line (``> build - model``), which
    is printed the moment the run starts and would otherwise look like the
    answer arriving early (seen 13 September 2026, where it made this probe
    report streaming that is not there)."""
    text = _ANSI.sub("", chunk).replace("\r", "")
    return "\n".join(line for line in text.split("\n") if not line.strip().startswith(">")).strip()


def _model(config: dict) -> tuple[str, str, str]:
    from programmind.ai.provider import TASK_BOARD, resolve_model
    model = resolve_model(config, TASK_BOARD)
    if not model:
        raise RuntimeError("No model configured (provider.models.board).")
    provider_id, _, model_id = model.partition("/")
    return model, provider_id, model_id


def _verdict(times: list[float], total: float, what: str) -> bool:
    """True when the text came out over time rather than in one piece."""
    if not times:
        print(f"\n{what}: no text came out at all; see the lines above.")
        return False
    spread = times[-1] - times[0]
    if len(times) > 1 and spread > 1.0:
        print(f"\n{what}: {len(times)} pieces of text over {spread:.1f} s of a {total:.1f} s run. "
              "It streams - a live answer is possible this way.")
        return True
    print(f"\n{what}: {len(times)} piece(s), the first at {times[0]:.1f} s of a {total:.1f} s run. "
          "The text arrives when the call is done; nothing to stream.")
    return False


# -- the two run formats -----------------------------------------------------

def _chunks(proc: subprocess.Popen, started: float):
    """Whatever has been written, the moment it is written: ``os.read`` on
    the raw pipe, so no buffer of ours holds the text back."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        yield time.monotonic() - started, chunk


def probe_run(config: dict, *, plain: bool) -> bool:
    from programmind.ai.opencode_client import OpenCodeProvider, opencode_environment
    model, _provider_id, _model_id = _model(config)
    command = OpenCodeProvider(config)._build_command(model)
    if plain:
        while "--format" in command:                  # the plain output, whatever this version calls it
            at = command.index("--format")
            del command[at:at + 2]
    name = "plain output" if plain else "--format json"
    print(f"\n=== {name} ===")
    print("command:", " ".join(command[:-1]), "(the prompt on standard input)")
    started = time.monotonic()
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=opencode_environment(config))
    assert proc.stdin is not None
    proc.stdin.write(PROMPT.encode("utf-8"))
    proc.stdin.close()
    times: list[float] = []
    buffer = b""
    for at, chunk in _chunks(proc, started):
        if plain:
            text = chunk.decode("utf-8", "replace")
            answer = answer_text(text)
            if answer:
                times.append(at)
            mark = "text " if answer else "other"
            print(f"{at:6.1f}s  {mark} {len(chunk):5d} bytes  {text[:LINE]!r}")
            continue
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"{at:6.1f}s  {line[:LINE + 40]}")
                continue
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            kind = str(event.get("type", "?"))
            text = part.get("text") if isinstance(part.get("text"), str) else ""
            if kind == "text" or part.get("type") == "text":
                times.append(at)
                print(f"{at:6.1f}s  text  {len(text):5d} chars  {text[:LINE]!r}")
            else:
                print(f"{at:6.1f}s  {kind}")
    proc.wait()
    return _verdict(times, time.monotonic() - started, name)


# -- the server and its event stream -----------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, timeout: float = 10.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace") or "null")


def _post(url: str, body: dict, timeout: float = 180.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(text or "null")
            except json.JSONDecodeError:
                return response.status, text[:LINE * 4]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:LINE * 4]


def _routes(spec: Any) -> list[str]:
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        return []
    out = []
    for path, methods in paths.items():
        for method in (methods if isinstance(methods, dict) else {}):
            if method.lower() in ("get", "post", "put", "delete", "patch"):
                out.append(f"{method.upper()} {path}")
    return sorted(out)


def _find(routes: list[str], method: str, *words: str) -> str | None:
    for route in routes:
        verb, _, path = route.partition(" ")
        low = path.lower()
        if verb == method and all(word in low for word in words):
            return path
    return None


def _unwrap(value: Any) -> Any:
    """``{"data": {...}}`` is how this OpenCode wraps an answer; the older
    shape is the object itself (seen 13 September 2026)."""
    if isinstance(value, dict) and "id" not in value and isinstance(value.get("data"), dict):
        return value["data"]
    return value


def _texts(node: Any, found: list[str]) -> None:
    """Every ``text`` string anywhere in an event, however it is nested."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "text" and isinstance(value, str) and value:
                found.append(value)
            else:
                _texts(value, found)
    elif isinstance(node, list):
        for item in node:
            _texts(item, found)


def probe_serve(config: dict) -> bool:
    from programmind.ai.opencode_client import _config_key, opencode_environment
    model, provider_id, model_id = _model(config)
    binary = str(_config_key(config, "provider.opencode.binary", "opencode") or "opencode")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    print("\n=== server mode ===")
    print("command:", binary, "serve --port", port)
    proc = subprocess.Popen([binary, "serve", "--port", str(port)], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            env=opencode_environment(config))
    server_lines: list[str] = []
    threading.Thread(target=lambda: [server_lines.append(l.rstrip()) for l in proc.stdout or []], daemon=True).start()
    started = time.monotonic()
    spec = None
    while time.monotonic() - started < 30:
        if proc.poll() is not None:
            print("the server stopped:", " | ".join(server_lines[-4:]) or "(no output)")
            return False
        try:
            spec = _get(f"{base}/doc", timeout=2)
            break
        except Exception:
            time.sleep(0.4)
    if spec is None:
        print(f"no answer from {base}/doc within 30 s. What the server printed:")
        for line in server_lines[:8]:
            print("   ", line[:LINE + 40])
        proc.terminate()
        return False
    routes = _routes(spec)
    print(f"the server answers; {len(routes)} route(s).")

    # The event stream, read in the background: what arrives, and when.
    times: list[float] = []
    kinds: dict[str, int] = {}
    samples: list[str] = []
    pieces: list[str] = []
    stop = threading.Event()
    stream_started = time.monotonic()
    stream_url = None
    for candidate in ("/api/event", "/event", "/global/event"):
        if _find(routes, "GET", candidate.rstrip("/")) or candidate == "/event":
            stream_url = base + candidate
            break

    def stream(url: str) -> None:
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                for raw in response:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    at = time.monotonic() - stream_started
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    body = _unwrap(event)
                    kind = str(body.get("type") or event.get("type") or "?")
                    kinds[kind] = kinds.get(kind, 0) + 1
                    found: list[str] = []
                    _texts(body, found)
                    text = max(found, key=len) if found else ""
                    if text and kind not in ("session.updated", "session.created"):
                        times.append(at)
                        pieces.append(text)
                        if len(samples) < 2:
                            samples.append(json.dumps(event, ensure_ascii=False)[:LINE * 6])
                        print(f"{at:6.1f}s  {kind}  {len(text):5d} chars  {text[-LINE:]!r}")
                    elif kind not in ("storage.write", "server.connected"):
                        print(f"{at:6.1f}s  {kind}")
        except Exception as exc:
            print("the event stream ended:", str(exc)[:LINE])

    print("event stream:", stream_url)
    threading.Thread(target=stream, args=(stream_url,), daemon=True).start()
    time.sleep(0.5)

    session_id = None
    for route in ("/api/session", "/session"):
        status, answer = _post(f"{base}{route}", {}, timeout=30)
        body = _unwrap(answer)
        session_id = body.get("id") if isinstance(body, dict) else None
        print(f"POST {route} -> {status} {('session ' + str(session_id)) if session_id else str(answer)[:LINE]}")
        if session_id:
            break
    if not session_id:
        stop.set()
        proc.terminate()
        return False

    part = {"type": "text", "text": PROMPT}
    shapes = [("model nested", {"model": {"providerID": provider_id, "modelID": model_id}, "parts": [part]}),
              ("model flat", {"providerID": provider_id, "modelID": model_id, "parts": [part]})]
    status, answer = 0, ""
    for route in ("/api/session/{id}/prompt", "/session/{id}/message", "/session/{id}/prompt_async"):
        if not _find(routes, "POST", route.split("/{")[0].lower()):
            continue
        url = f"{base}{route.replace('{id}', session_id)}"
        for name, body in shapes:
            at = time.monotonic() - stream_started
            print(f"{at:6.1f}s  POST {route} ({name}) ...")
            status, answer = _post(url, body)
            print(f"{time.monotonic() - stream_started:6.1f}s  -> {status} {str(answer)[:LINE * 2]}")
            if status < 400:
                break
        if status < 400:
            break
    time.sleep(1.5)
    stop.set()
    proc.terminate()
    print("event types seen:", ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items())) or "(none)")
    for sample in samples:
        print("an event carrying text, as it came:")
        print("   ", sample)
    if len(pieces) > 1:
        grows = pieces[1].startswith(pieces[0][:40])
        print("the text arrives as", "the whole answer so far, each event replacing the last" if grows
              else "pieces to be joined")
    if status >= 400 or not status:
        print("\nserver mode: the question was refused or no route matched; the shapes above are what to fix.")
        return False
    return _verdict(times, time.monotonic() - stream_started, "server mode")


def probe(config: dict, mode: str = "json") -> int:
    """``mode``: json, plain, serve, or all."""
    ok = False
    if mode in ("json", "all"):
        ok = probe_run(config, plain=False) or ok
    if mode in ("plain", "all"):
        ok = probe_run(config, plain=True) or ok
    if mode in ("serve", "all"):
        ok = probe_serve(config) or ok
    print("\n" + ("At least one way streams: a live answer can be built." if ok
                  else "No way streams here: the page cannot show the answer as it is written."))
    return 0 if ok else 1
