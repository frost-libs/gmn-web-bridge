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
        mode="local",
        upstream_proxy="",
        log_requests=False,
        cors=False,
        fetch_image_urls=True,
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


def _raw(port: int, request: bytes) -> bytes:
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
    return received


def _serve(settings, client):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(Bridge(settings, client)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class CapabilityTest(unittest.TestCase):
    def test_the_key_is_accepted_from_each_client_header(self):
        client = FakeClient()
        server = _serve(_settings(), client)
        port = server.server_address[1]
        body = json.dumps({"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Hi"}]}).encode()
        requests = [
            b"GET /v1/models HTTP/1.0\r\nHost: localhost\r\nx-api-key: sk-test\r\n\r\n",
            b"GET /v1/models HTTP/1.0\r\nHost: localhost\r\nx-goog-api-key: sk-test\r\n\r\n",
            b"GET /v1/models?key=sk-test HTTP/1.0\r\nHost: localhost\r\n\r\n",
            (
                b"POST /v1/chat/completions HTTP/1.0\r\nHost: localhost\r\n"
                b"Authorization: Bearer sk-test\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            ),
        ]
        try:
            for request in requests:
                received = _raw(port, request)
                self.assertIn(b"200", received.split(b"\r\n", 1)[0])
            missing = _raw(port, b"GET /v1/models HTTP/1.0\r\nHost: localhost\r\n\r\n")
            self.assertIn(b"401", missing.split(b"\r\n", 1)[0])
        finally:
            server.shutdown()
            server.server_close()

    def test_anonymous_mode_still_requires_the_local_key(self):
        client = FakeClient()
        server = _serve(_settings(mode="anonymous", cookie=""), client)
        try:
            received = _raw(server.server_address[1], b"GET /v1/models HTTP/1.0\r\nHost: localhost\r\n\r\n")
        finally:
            server.shutdown()
            server.server_close()

        self.assertIn(b"401", received.split(b"\r\n", 1)[0])

    def test_health_needs_no_key_and_cors_is_optional(self):
        client = FakeClient()
        closed = _serve(_settings(), client)
        try:
            health = _raw(closed.server_address[1], b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            options = _raw(closed.server_address[1], b"OPTIONS /v1/chat/completions HTTP/1.0\r\nHost: localhost\r\n\r\n")
        finally:
            closed.shutdown()
            closed.server_close()
        self.assertIn(b"200", health.split(b"\r\n", 1)[0])
        self.assertIn(b'"status": "ok"', health)
        self.assertIn(b"404", options.split(b"\r\n", 1)[0])

        opened = _serve(_settings(cors=True), client)
        try:
            options = _raw(opened.server_address[1], b"OPTIONS /v1/chat/completions HTTP/1.0\r\nHost: localhost\r\n\r\n")
            models = _raw(
                opened.server_address[1],
                b"GET /v1/models HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer sk-test\r\n\r\n",
            )
        finally:
            opened.shutdown()
            opened.server_close()
        self.assertIn(b"204", options.split(b"\r\n", 1)[0])
        self.assertIn(b"Access-Control-Allow-Origin: *", options)
        self.assertIn(b"Access-Control-Allow-Origin: *", models)

    def test_a_chunked_body_is_read(self):
        client = FakeClient()
        server = _serve(_settings(), client)
        payload = json.dumps(
            {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Chunked"}]}
        ).encode()
        chunk = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
        request = (
            b"POST /v1/chat/completions HTTP/1.0\r\nHost: localhost\r\n"
            b"Authorization: Bearer sk-test\r\nTransfer-Encoding: chunked\r\n\r\n"
            + chunk
        )
        try:
            received = _raw(server.server_address[1], request)
        finally:
            server.shutdown()
            server.server_close()

        self.assertIn(b"200", received.split(b"\r\n", 1)[0])
        self.assertIn("Chunked", client.calls[0]["prompt"])

    def test_usage_is_reported_and_a_tool_only_reply_has_null_content(self):
        client = FakeClient()
        client.reply = '```tool_call\n{"name":"ls","arguments":{}}\n```'
        server = _serve(_settings(), client)
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
            server.shutdown()
            server.server_close()

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNone(payload["choices"][0]["message"]["content"])
        self.assertEqual(payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "ls")
        self.assertIn("prompt_tokens", payload["usage"])
        self.assertGreater(payload["usage"]["total_tokens"], 0)

    def test_include_usage_adds_a_final_chunk(self):
        client = FakeClient()
        server = _serve(_settings(), client)
        try:
            status, body = _post(
                server.server_address[1],
                "/v1/chat/completions",
                {
                    "model": "gemini-3.8-flash",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 200)
        self.assertIn(b'"usage"', body)
        self.assertIn(b"[DONE]", body)

    def test_responses_covers_text_and_tool_calls(self):
        client = FakeClient()
        server = _serve(_settings(), client)
        port = server.server_address[1]
        try:
            status, body = _post(
                port,
                "/v1/responses",
                {
                    "model": "gemini-3.8-flash",
                    "instructions": "Be brief",
                    "input": "Hello",
                    "tools": [{"type": "function", "name": "ls", "parameters": {"type": "object"}}],
                },
            )
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["output"][0]["content"][0]["text"], "Hi")
            self.assertIn("input_tokens", payload["usage"])
            self.assertIn("Be brief", client.calls[0]["prompt"])
            self.assertIn("ls", client.calls[0]["prompt"])

            client.reply = 'Answer\n```tool_call\n{"name":"ls","arguments":{"path":"."}}\n```'
            status, body = _post(
                port,
                "/v1/responses",
                {
                    "model": "gemini-3.8-flash",
                    "input": "List",
                    "stream": True,
                    "tools": [{"type": "function", "name": "ls", "parameters": {}}],
                },
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 200)
        self.assertIn(b"event: response.created", body)
        self.assertIn(b"event: response.output_text.delta", body)
        self.assertNotIn(b"tool_call", body.split(b"event: response.function_call_arguments.done")[0])
        self.assertIn(b"event: response.function_call_arguments.done", body)
        self.assertIn(b"event: response.completed", body)

    def test_google_response_carries_usage(self):
        client = FakeClient()
        server = _serve(_settings(), client)
        try:
            status, body = _post(
                server.server_address[1],
                "/v1beta/models/gemini-3.8-flash:generateContent",
                {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
            )
        finally:
            server.shutdown()
            server.server_close()

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"][0]["index"], 0)
        self.assertEqual(payload["modelVersion"], "gemini-3.8-flash")
        self.assertIn("totalTokenCount", payload["usageMetadata"])

    def test_pro_and_argon_say_so_when_there_is_no_cookie(self):
        client = FakeClient()
        server = _serve(_settings(mode="anonymous", cookie=""), client)
        try:
            received = _raw(
                server.server_address[1],
                b"GET /v1/models HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer sk-test\r\n\r\n",
            )
        finally:
            server.shutdown()
            server.server_close()

        payload = json.loads(received.split(b"\r\n\r\n", 1)[1])
        notes = {item["id"]: item["description"] for item in payload["data"]}
        self.assertIn("Without a cookie", notes["gemini-3.1-pro"])
        self.assertIn("Without a cookie", notes["gemini-4-argon"])
        self.assertNotIn("Without a cookie", notes["gemini-3.8-flash"])

    def test_the_request_log_omits_the_key_and_the_body(self):
        client = FakeClient()
        server = _serve(_settings(log_requests=True), client)
        port = server.server_address[1]
        try:
            from contextlib import redirect_stderr
            from io import StringIO

            buffer = StringIO()
            with redirect_stderr(buffer):
                _raw(port, b"GET /v1/models?key=sk-test HTTP/1.0\r\nHost: localhost\r\n\r\n")
                deadline = time.monotonic() + 1
                while "GET /v1/models" not in buffer.getvalue() and time.monotonic() < deadline:
                    time.sleep(0.01)
            logged = buffer.getvalue()
        finally:
            server.shutdown()
            server.server_close()

        self.assertIn("GET /v1/models", logged)
        self.assertNotIn("sk-test", logged)
        self.assertNotIn("?", logged)
