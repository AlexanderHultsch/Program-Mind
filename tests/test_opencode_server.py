#!/usr/bin/env python3
"""Ask the vault through ``opencode serve`` (spec 4.1, 13 September 2026):
the answer is the one the call returns, the live text is what the event
stream showed while it ran, and neither the prompt coming back nor the
model's reasoning is mistaken for the answer.

The OpenCode server is faked in this process - the shapes are the ones its
own ``/doc`` and event stream showed on the target machine."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from programmind.ai.opencode_client import OpenCodeError, stop_call  # noqa: E402
from programmind.ai.opencode_server import OpenCodeServerProvider, _Routes  # noqa: E402
from programmind.ai.provider import TASK_BOARD  # noqa: E402

CONFIG = {"provider": {"models": {"board": "azure/Opencode-Kimi-K2.7"}}}
SPEC = {"paths": {"/doc": {"get": {}}, "/api/event": {"get": {}}, "/event": {"get": {}},
                  "/api/session": {"post": {}}, "/session": {"post": {}},
                  "/session/{sessionID}/message": {"post": {}, "get": {}},
                  "/session/{sessionID}/abort": {"post": {}},
                  "/session/{sessionID}": {"delete": {}}}}


class FakeOpenCode:
    """The routes, the ``data`` wrapper and the event shapes of the real
    server, with the answer written a piece at a time."""

    def __init__(self, *, answer: str = "Hello world!", tokens=(11, 3), silent: bool = False, slow: float = 0.0):
        self.answer, self.tokens, self.silent, self.slow = answer, tokens, silent, slow
        self.prompts: list[str] = []
        self.deleted: list[str] = []
        self.aborted: list[str] = []
        self.listeners: list[list] = []
        self.started = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"        # as the real server speaks, so the stream is chunked

            def log_message(self, *args):
                pass

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/doc":
                    return self._json(SPEC)
                if self.path == "/api/event":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    queue: list = []
                    outer.listeners.append(queue)
                    outer.started.set()
                    try:
                        while True:
                            if queue:
                                payload = b"data: " + json.dumps(queue.pop(0)).encode() + b"\n\n"
                                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                                self.wfile.flush()
                            else:
                                time.sleep(0.01)
                    except OSError:
                        return                  # the call dropped the stream, as it does when it is done
                if self.path.endswith("/message"):
                    return self._json({"data": []})
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path in ("/api/session", "/session"):
                    return self._json({"data": {"id": "ses_fake", "projectID": "p1"}})
                if self.path.endswith("/abort"):
                    outer.aborted.append(self.path)
                    return self._json({"data": True})
                if self.path.endswith("/message"):
                    prompt = json.loads(body)["parts"][0]["text"]
                    outer.prompts.append(prompt)
                    return self._json({"data": outer.answer_for(prompt)})
                self.send_response(404)
                self.end_headers()

            def do_DELETE(self):
                outer.deleted.append(self.path)
                self._json({"data": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def send(self, event: dict) -> None:
        for queue in self.listeners:
            queue.append(event)
        time.sleep(0.08)

    def answer_for(self, prompt: str) -> dict:
        """What the model writes, event by event, as the real one does."""
        self.send({"type": "message.updated", "data": {"sessionID": "ses_fake", "info": {"id": "msg_user", "role": "user"}}})
        self.send({"type": "message.part.updated",                      # the prompt, coming back
                   "data": {"sessionID": "ses_fake", "part": {"id": "prt_q", "messageID": "msg_user",
                                                              "type": "text", "text": prompt}}})
        self.send({"type": "message.updated", "data": {"sessionID": "ses_fake", "info": {"id": "msg_a", "role": "assistant"}}})
        self.send({"type": "message.part.updated",                      # the model thinking
                   "data": {"sessionID": "ses_fake", "part": {"id": "prt_r", "messageID": "msg_a",
                                                              "type": "reasoning", "text": "thinking hard"}}})
        parts = [{"type": "reasoning", "text": "thinking hard"}]
        if self.silent:
            time.sleep(self.slow)
            return {"info": {"id": "msg_a", "tokens": {"input": 9, "output": 0}}, "parts": parts}
        head, tail = self.answer[:5], self.answer[5:]
        self.send({"type": "message.part.updated",
                   "data": {"sessionID": "ses_fake", "part": {"id": "prt_t", "messageID": "msg_a",
                                                              "type": "text", "text": head}}})
        self.send({"type": "message.part.delta",                        # a piece to join on
                   "data": {"sessionID": "ses_fake", "partID": "prt_t", "messageID": "msg_a", "delta": tail}})
        self.send({"type": "message.part.updated",                      # the whole part again
                   "data": {"sessionID": "ses_fake", "part": {"id": "prt_t", "messageID": "msg_a",
                                                              "type": "text", "text": self.answer}}})
        time.sleep(self.slow or 0.15)
        return {"info": {"id": "msg_a", "tokens": {"input": self.tokens[0], "output": self.tokens[1]}},
                "parts": parts + [{"type": "text", "text": self.answer}]}

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestRoutes(unittest.TestCase):
    def test_the_routes_come_from_the_servers_own_doc_whatever_it_calls_its_placeholder(self):
        fake = FakeOpenCode()
        self.addCleanup(fake.stop)
        routes = _Routes(fake.base)
        self.assertEqual(routes.session, "/api/session")          # this OpenCode creates under /api ...
        self.assertEqual(routes.message, "/session/{id}/message")  # ... and takes the question outside it
        self.assertEqual(routes.event, "/api/event")
        self.assertEqual(_Routes.fill(routes.abort, "ses_1"), "/session/ses_1/abort")

    def test_a_server_without_a_doc_falls_back_to_the_known_routes(self):
        routes = _Routes("http://127.0.0.1:1")                    # nothing answers there
        self.assertEqual((routes.session, routes.event), ("/api/session", "/api/event"))


class TestStreamedAnswer(unittest.TestCase):
    def test_the_answer_is_the_call_and_the_live_text_is_the_stream(self):
        fake = FakeOpenCode(answer="Hello world!")
        self.addCleanup(fake.stop)
        seen: list[str] = []
        provider = OpenCodeServerProvider(CONFIG, base_url=fake.base)
        result = provider.complete(TASK_BOARD, "What is the design freeze?", seen.append)
        self.assertEqual(result.text, "Hello world!")             # the answer is what the call returned
        self.assertEqual((result.input_tokens, result.output_tokens), (11, 3))
        self.assertEqual((result.provider, result.model), ("azure", "Opencode-Kimi-K2.7"))
        self.assertGreater(result.duration_seconds, 0)
        self.assertEqual(seen, ["Hello", "Hello world!", "Hello world!"][:len(seen)])
        self.assertEqual(seen[0], "Hello")                        # ... and grew as the model wrote
        self.assertEqual(seen[-1], "Hello world!")
        joined = " ".join(seen)
        self.assertNotIn("thinking hard", joined)                 # 4.1, decision 4: not the reasoning
        self.assertNotIn("design freeze", joined)                 # ... and not the prompt coming back
        self.assertEqual(fake.deleted, ["/session/ses_fake"])      # the session is cleared up
        self.assertEqual(len(fake.prompts), 1)

    def test_no_callback_is_no_trouble(self):
        fake = FakeOpenCode(answer="Plain answer")
        self.addCleanup(fake.stop)
        result = OpenCodeServerProvider(CONFIG, base_url=fake.base).complete(TASK_BOARD, "Question?")
        self.assertEqual(result.text, "Plain answer")

    def test_a_call_that_answers_nothing_is_retried_once_and_then_reported(self):
        fake = FakeOpenCode(silent=True)
        self.addCleanup(fake.stop)
        with self.assertRaises(OpenCodeError) as caught:
            OpenCodeServerProvider(CONFIG, base_url=fake.base).complete(TASK_BOARD, "Question?")
        self.assertEqual(len(fake.prompts), 2)                    # OC-11: one retry, with the nudge in front
        self.assertIn("previous attempt", fake.prompts[1])
        self.assertIn("this was the retry", str(caught.exception))
        self.assertIn("message.part.updated", str(caught.exception))   # OC-9: what happened instead
        self.assertEqual(fake.deleted, ["/session/ses_fake", "/session/ses_fake"])

    def test_stop_aborts_the_session_and_the_call_says_so(self):
        fake = FakeOpenCode(answer="Too late", slow=2.5)
        self.addCleanup(fake.stop)
        provider = OpenCodeServerProvider(CONFIG, base_url=fake.base)
        failure: list[Exception] = []

        def run() -> None:
            try:
                provider.complete(TASK_BOARD, "Question?")
            except Exception as exc:
                failure.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not fake.prompts:
            time.sleep(0.02)
        time.sleep(0.1)
        self.assertTrue(stop_call(worker.ident))
        worker.join(timeout=10)
        self.assertEqual([str(exc) for exc in failure], ["opencode was stopped"])
        self.assertEqual(fake.aborted, ["/session/ses_fake/abort"])

    def test_an_unreachable_server_is_an_error_not_a_silent_pass(self):
        provider = OpenCodeServerProvider(CONFIG, base_url="http://127.0.0.1:1")
        with self.assertRaises(Exception):
            provider.complete(TASK_BOARD, "Question?")


if __name__ == "__main__":
    unittest.main()
