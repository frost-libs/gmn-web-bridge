# SPDX-License-Identifier: MIT
import json
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from gmn_web_bridge.config import Settings
from gmn_web_bridge.server import Bridge, handler_class


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.release = threading.Event()
        self.reply = "Hi"
        self.hold = False

    def generate(self, prompt, model, files=None):
        self.calls.append({"prompt": prompt, "model": model, "files": files})
        if prompt.startswith("Summarize this coding conversation"):
            yield "summary of src/old.py"
            return
        yield self.reply
        if self.hold:
            self.release.wait(2)


def _settings(**overrides) -> Settings:
    values = dict(
        host="127.0.0.1",
        port=8091,
        api_key="sk-test",
        cookie="SID=a; SAPISID=b",
        xsrf_token="",
        auth_user="0",
        temporary_chats=True,
        request_timeout_sec=30,
        prompt_char_limit=32000,
        aliases_path="",
        build_label="",
        retry_attempts=1,
        retry_delay_sec=0,
        default_model="gemini-3.8-flash",
        summary_model="gemini-3.5-flash-lite",
        send_model_header=False,
    )
    values.update(overrides)
    return Settings(**values)


def _post(port: int, path: str, payload: dict, stream: bool = False) -> tuple[int, bytes]:
    raw = json.dumps(payload).encode()
    request = (
        f"POST {path} HTTP/1.0\r\n"
        "Host: localhost\r\n"
        "Authorization: Bearer sk-test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(raw)}\r\n"
        "\r\n"
    ).encode() + raw
    with socket.create_connection(("127.0.0.1", port), 2) as sock:
        sock.sendall(request)
        sock.settimeout(1)
        received = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            received += chunk
            if stream and b"Hi" in received and b"[DONE]" not in received:
                break
    status = int(received.split(b" ", 2)[1])
    body = received.split(b"\r\n\r\n", 1)[1]
    return status, body


class ServerTest(unittest.TestCase):
    def test_stream_flushes_before_the_client_finishes(self):
        client = FakeClient()
        client.hold = True
        bridge = Bridge(_settings(), client)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            started = time.monotonic()
            status, body = _post(
                server.server_address[1],
                "/v1/chat/completions",
                {"model": "gemini-3.8-flash", "stream": True, "messages": [{"role": "user", "content": "Hello"}]},
                stream=True,
            )
            elapsed = time.monotonic() - started
        finally:
            client.release.set()
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 200)
        self.assertIn(b"Hi", body)
        self.assertLess(elapsed, 0.8)
        self.assertIn("Hello", client.calls[0]["prompt"])

    def test_a_tool_reply_puts_text_before_the_call(self):
        client = FakeClient()
        client.reply = 'Answer\n```tool_call\n{"name":"ls","arguments":{"path":"."}}\n```'
        bridge = Bridge(_settings(), client)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, body = _post(
                server.server_address[1],
                "/v1/chat/completions",
                {
                    "model": "gemini-3.8-flash",
                    "stream": False,
                    "tools": [{"type": "function", "function": {"name": "ls"}}],
                    "messages": [{"role": "user", "content": "List"}],
                },
            )
        finally:
            client.release.set()
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 200)
        payload = json.loads(body)
        message = payload["choices"][0]["message"]
        self.assertIn("Answer", message["content"])
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "ls")
        self.assertEqual(payload["choices"][0]["finish_reason"], "tool_calls")

    def test_google_generate_content(self):
        client = FakeClient()
        bridge = Bridge(_settings(), client)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, body = _post(
                server.server_address[1],
                "/v1beta/models/gemini-3.8-flash:generateContent",
                {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
            )
        finally:
            client.release.set()
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["candidates"][0]["content"]["parts"][0]["text"], "Hi")

    def test_missing_key_is_rejected(self):
        client = FakeClient()
        bridge = Bridge(_settings(), client)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            request = b"GET /v1/models HTTP/1.0\r\nHost: localhost\r\n\r\n"
            with socket.create_connection(("127.0.0.1", port), 2) as sock:
                sock.sendall(request)
                received = sock.recv(4096)
        finally:
            client.release.set()
            server.shutdown()
            server.server_close()

        self.assertIn(b"401", received)
        self.assertEqual(client.calls, [])

    def test_over_the_guard_summarizes_before_the_reply(self):
        client = FakeClient()
        messages = [
            {"role": "user", "content": "OLD " + ("x" * 2000)},
            {"role": "user", "content": "please continue"},
        ]
        limit = 400
        bridge = Bridge(_settings(prompt_char_limit=limit), client)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, body = _post(
                server.server_address[1],
                "/v1/chat/completions",
                {"model": "gemini-3.1-pro", "stream": False, "messages": messages},
            )
        finally:
            client.release.set()
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 200)
        self.assertIn(b"Hi", body)
        self.assertEqual(client.calls[0]["model"], "gemini-3.5-flash-lite")
        self.assertEqual(client.calls[1]["model"], "gemini-3.1-pro")
        self.assertIn("summary of src/old.py", client.calls[1]["prompt"])
        self.assertNotIn("OLD ", client.calls[1]["prompt"])
