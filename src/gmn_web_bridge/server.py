# SPDX-License-Identifier: MIT
"""One HTTP process. OpenAI and Google routes call the Gemini client in-process."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .client import GeminiClient, GeminiError
from .compaction import (
    _SUMMARY_INSTRUCTION,
    PromptTooLarge,
    SummaryFailed,
    compact_chat_body,
    fetch_summary,
)
from .config import Settings
from .google_api import (
    google_response,
    google_text_chunk,
    google_to_openai,
    model_from_path,
)
from .models import MODELS, InvalidThink, resolve_model
from .openai_api import (
    Aliases,
    UnknownPublicModel,
    UnnamedToolResult,
    _openai_error,
    load_aliases,
    rewrite_chat_body,
    translate_upstream_error,
)
from .tools import parse_tool_calls, render_conversation, withhold_tool_fence


@dataclass
class Turn:
    model: str
    prompt: str
    images: list[tuple[bytes, str, str]]
    tools: bool
    stream: bool


class Bridge:
    def __init__(self, settings: Settings, client: GeminiClient) -> None:
        self.settings = settings
        self.client = client
        self.aliases: Aliases = load_aliases(settings.aliases_path or None)

    def prepare(self, body: dict[str, Any]) -> Turn:
        rewritten = rewrite_chat_body(body, self.aliases)
        limit = self.settings.prompt_char_limit
        if limit > 0:
            rewritten = compact_chat_body(
                rewritten,
                limit,
                lambda text: fetch_summary(text, self._complete_summary, limit),
            )
        messages = rewritten.get("messages")
        if not isinstance(messages, list):
            messages = []
        prompt, images = render_conversation(messages, rewritten.get("tools"))
        model = rewritten.get("model")
        if not isinstance(model, str) or not model:
            model = self.settings.default_model
        resolve_model(model, self.settings.default_model)
        return Turn(
            model=model,
            prompt=prompt,
            images=images,
            tools=bool(rewritten.get("tools")),
            stream=rewritten.get("stream") is True,
        )

    def _complete_summary(self, text: str) -> str:
        prompt = f"{_SUMMARY_INSTRUCTION}\n\n{text}"
        return "".join(self.client.generate(prompt, self.settings.summary_model))

    def reply(self, turn: Turn) -> Iterator[str]:
        yield from self.client.generate(turn.prompt, turn.model, turn.images)

    def models_payload(self) -> dict[str, Any]:
        data = [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "google",
                "description": spec.description,
            }
            for name, spec in MODELS.items()
        ]
        present = {item["id"] for item in data}
        for entry in self.aliases.list_entries():
            if entry["id"] in present:
                continue
            data.append(
                {
                    "id": entry["id"],
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "google",
                    "description": entry["description"],
                }
            )
        return {"object": "list", "data": data}

    def google_models_payload(self) -> dict[str, Any]:
        models = []
        for name, spec in MODELS.items():
            models.append(
                {
                    "name": f"models/{name}",
                    "displayName": spec.description,
                    "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
                }
            )
        for entry in self.aliases.list_entries():
            models.append(
                {
                    "name": f"models/{entry['id']}",
                    "displayName": entry["description"],
                    "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
                }
            )
        return {"models": models}


def _authorized(header: str | None, api_key: str) -> bool:
    if not header or not api_key:
        return False
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return False
    return hmac.compare_digest(
        hashlib.sha256(token.strip().encode()).digest(),
        hashlib.sha256(api_key.encode()).digest(),
    )


def handler_class(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "gmn-web-bridge"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(b"")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            self._dispatch(self.rfile.read(length) if length else b"")

        def _dispatch(self, raw: bytes) -> None:
            if not _authorized(self.headers.get("Authorization"), bridge.settings.api_key):
                self._write_json(401, _openai_error("Invalid API key."))
                return
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                if self.command == "GET" and path == "/v1/models":
                    self._write_json(200, json.dumps(bridge.models_payload()).encode())
                    return
                if self.command == "GET" and path == "/v1beta/models":
                    self._write_json(200, json.dumps(bridge.google_models_payload()).encode())
                    return
                if self.command == "POST" and path == "/v1/chat/completions":
                    self._openai(raw)
                    return
                if self.command == "POST" and path.startswith("/v1beta/models/"):
                    self._google(path, raw)
                    return
            except UnknownPublicModel:
                self._write_json(400, _openai_error("unknown model"))
                return
            except UnnamedToolResult as exc:
                self._write_json(400, _openai_error(str(exc)))
                return
            except InvalidThink:
                self._write_json(400, _openai_error("Invalid think level."))
                return
            except PromptTooLarge:
                self._write_json(400, _openai_error("The prompt exceeds the Gemini prompt character guard."))
                return
            except SummaryFailed as exc:
                self._write_json(exc.status, _openai_error(exc.message))
                return
            except GeminiError as exc:
                self._fail(exc.status, exc.message)
                return
            self._write_json(404, _openai_error("Not found."))

        def _openai(self, raw: bytes) -> None:
            body = _json_object(raw)
            if body is None:
                self._write_json(400, _openai_error("Request body must be a JSON object."))
                return
            turn = bridge.prepare(body)
            completion_id = "chatcmpl-" + uuid.uuid4().hex
            if not turn.stream:
                text = "".join(bridge.reply(turn))
                prose, calls = (parse_tool_calls(text) if turn.tools else (text, None))
                message: dict[str, Any] = {"role": "assistant", "content": prose}
                finish = "stop"
                if calls:
                    message["tool_calls"] = calls
                    finish = "tool_calls"
                payload = {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": turn.model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                }
                self._write_json(200, json.dumps(payload).encode())
                return
            self._start_sse()
            self._send_chunk(completion_id, turn.model, {"role": "assistant"}, None)
            if not turn.tools:
                for delta in bridge.reply(turn):
                    self._send_chunk(completion_id, turn.model, {"content": delta}, None)
                self._send_chunk(completion_id, turn.model, {}, "stop")
            else:
                self._stream_tools(completion_id, turn)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def _stream_tools(self, completion_id: str, turn: Turn) -> None:
            held = ""
            for delta in bridge.reply(turn):
                held += delta
                emit, held = withhold_tool_fence(held)
                if emit:
                    self._send_chunk(completion_id, turn.model, {"content": emit}, None)
            if "```tool_call" in held:
                prose, calls = parse_tool_calls(held)
            else:
                prose, calls = held, None
            if prose:
                self._send_chunk(completion_id, turn.model, {"content": prose}, None)
            if calls:
                self._send_chunk(completion_id, turn.model, {"tool_calls": calls}, "tool_calls")
            else:
                self._send_chunk(completion_id, turn.model, {}, "stop")

        def _google(self, path: str, raw: bytes) -> None:
            model, action = model_from_path(path)
            if action not in ("generateContent", "streamGenerateContent"):
                self._write_json(404, _google_error(404, "Not found."))
                return
            body = _json_object(raw)
            if body is None:
                self._write_json(400, _google_error(400, "Request body must be a JSON object."))
                return
            stream = action == "streamGenerateContent"
            turn = bridge.prepare(google_to_openai(model, body, stream))
            if not stream:
                text = "".join(bridge.reply(turn))
                prose, calls = parse_tool_calls(text) if turn.tools else (text, None)
                self._write_json(200, json.dumps(google_response(prose, calls)).encode())
                return
            self._start_sse()
            if not turn.tools:
                for delta in bridge.reply(turn):
                    self._write_sse(google_text_chunk(delta))
                self._write_sse(google_response("", None))
            else:
                held = ""
                for delta in bridge.reply(turn):
                    held += delta
                    emit, held = withhold_tool_fence(held)
                    if emit:
                        self._write_sse(google_text_chunk(emit))
                if "```tool_call" in held:
                    prose, calls = parse_tool_calls(held)
                else:
                    prose, calls = held, None
                if prose:
                    self._write_sse(google_text_chunk(prose))
                self._write_sse(google_response("", calls))
            self.wfile.flush()

        def _send_chunk(self, completion_id: str, model: str, delta: dict[str, Any], finish: str | None) -> None:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self._write_sse(chunk)

        def _write_sse(self, payload: dict[str, Any]) -> None:
            self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n")
            self.wfile.flush()

        def _start_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def _fail(self, status: int, message: str) -> None:
            translated = translate_upstream_error(status, _openai_error(message))
            if translated is not None:
                self._write_json(translated[0], translated[1])
                return
            self._write_json(status, _openai_error(message))

        def _write_json(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def _json_object(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _google_error(status: int, message: str) -> bytes:
    return json.dumps({"error": {"code": status, "message": message, "status": "INVALID_ARGUMENT"}}).encode()


def serve(settings: Settings, client: GeminiClient | None = None) -> None:
    owns_client = client is None
    gemini = client or GeminiClient(settings)
    bridge = Bridge(settings, gemini)
    server = ThreadingHTTPServer((settings.host, settings.port), handler_class(bridge))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if owns_client:
            gemini.close()
