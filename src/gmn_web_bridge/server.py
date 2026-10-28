# SPDX-License-Identifier: MIT
"""One HTTP process. OpenAI and Google routes call the Gemini client in-process."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

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
    estimate_usage,
    load_aliases,
    rewrite_chat_body,
    translate_upstream_error,
)
from .responses_api import completed_response, created_event, responses_to_chat
from .tools import parse_tool_calls, render_conversation, withhold_tool_fence

_COOKIE_NOTE_MODELS = {"gemini-3.1-pro", "gemini-4-argon"}
_COOKIE_NOTE = " Without a cookie, Gemini may answer with Flash."


@dataclass
class Turn:
    model: str
    prompt: str
    images: list[tuple[bytes | str, str, str]]
    tools: bool
    stream: bool
    include_usage: bool


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
        options = rewritten.get("stream_options")
        include_usage = isinstance(options, dict) and options.get("include_usage") is True
        return Turn(
            model=model,
            prompt=prompt,
            images=images,
            tools=bool(rewritten.get("tools")),
            stream=rewritten.get("stream") is True,
            include_usage=include_usage,
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
                "description": self._describe(name, spec.description),
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
            description = self._describe(name, spec.description)
            models.append(
                {
                    "name": f"models/{name}",
                    "displayName": description,
                    "description": description,
                    "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
                }
            )
        for entry in self.aliases.list_entries():
            models.append(
                {
                    "name": f"models/{entry['id']}",
                    "displayName": entry["description"],
                    "description": entry["description"],
                    "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
                }
            )
        return {"models": models}

    def _describe(self, name: str, description: str) -> str:
        if not self.settings.cookie and name in _COOKIE_NOTE_MODELS:
            return description + _COOKIE_NOTE
        return description


def _authorized(presented: str, api_key: str) -> bool:
    if not presented or not api_key:
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(),
        hashlib.sha256(api_key.encode()).digest(),
    )


def _presented_key(headers: Any, path: str) -> str:
    """The local key from Bearer, x-api-key, x-goog-api-key, or ?key=."""
    authorization = headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    for name in ("x-api-key", "x-goog-api-key"):
        value = headers.get(name)
        if value and value.strip():
            return value.strip()
    if "?" not in path:
        return ""
    for pair in path.split("?", 1)[1].split("&"):
        name, sep, value = pair.partition("=")
        if name == "key" and sep and value:
            return unquote(value)
    return ""


def handler_class(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "gmn-web-bridge"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(b"")

        def do_POST(self) -> None:  # noqa: N802
            try:
                raw = _read_body(self)
            except ValueError:
                self._dispatch(b"", invalid_body=True)
                return
            self._dispatch(raw)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch(b"")

        def _dispatch(self, raw: bytes, invalid_body: bool = False) -> None:
            started = time.monotonic()
            self._status = 500
            self._model = ""
            try:
                self._route(raw, invalid_body)
            finally:
                self._log_request(started)

        def _route(self, raw: bytes, invalid_body: bool) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if self.command == "OPTIONS":
                self._options()
                return
            if self.command == "GET" and path == "/health":
                self._write_json(200, b'{"status": "ok"}')
                return
            if not _authorized(_presented_key(self.headers, self.path), bridge.settings.api_key):
                self._write_json(401, _openai_error("Invalid API key."))
                return
            if invalid_body:
                self._write_json(400, _openai_error("Request body must be a JSON object."))
                return
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
                if self.command == "POST" and path == "/v1/responses":
                    self._responses(raw)
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

        def _options(self) -> None:
            if not bridge.settings.cors:
                self._write_json(404, _openai_error("Not found."))
                return
            self.send_response(204)
            self._status = 204
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def _openai(self, raw: bytes) -> None:
            body = _json_object(raw)
            if body is None:
                self._write_json(400, _openai_error("Request body must be a JSON object."))
                return
            turn = bridge.prepare(body)
            self._model = turn.model
            completion_id = "chatcmpl-" + uuid.uuid4().hex
            if not turn.stream:
                text = "".join(bridge.reply(turn))
                prose, calls = (parse_tool_calls(text) if turn.tools else (text, None))
                message: dict[str, Any] = {"role": "assistant", "content": prose if prose or not calls else None}
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
                    "usage": estimate_usage(turn.prompt, text),
                }
                self._write_json(200, json.dumps(payload).encode())
                return
            self._start_sse()
            self._send_chunk(completion_id, turn.model, {"role": "assistant"}, None)
            full = ""
            if not turn.tools:
                for delta in bridge.reply(turn):
                    full += delta
                    self._send_chunk(completion_id, turn.model, {"content": delta}, None)
                self._send_chunk(completion_id, turn.model, {}, "stop")
            else:
                full = self._stream_tools(completion_id, turn)
            if turn.include_usage:
                self._write_sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": turn.model,
                        "choices": [],
                        "usage": estimate_usage(turn.prompt, full),
                    }
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def _stream_tools(self, completion_id: str, turn: Turn) -> str:
            held = ""
            full = ""
            for delta in bridge.reply(turn):
                full += delta
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
            return full

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
            self._model = turn.model
            if not stream:
                text = "".join(bridge.reply(turn))
                prose, calls = parse_tool_calls(text) if turn.tools else (text, None)
                payload = google_response(prose, calls, prompt=turn.prompt, completion=text, model=turn.model)
                self._write_json(200, json.dumps(payload).encode())
                return
            self._start_sse()
            full = ""
            if not turn.tools:
                for delta in bridge.reply(turn):
                    full += delta
                    self._write_sse(google_text_chunk(delta))
                self._write_sse(google_response("", None, prompt=turn.prompt, completion=full, model=turn.model))
            else:
                held = ""
                for delta in bridge.reply(turn):
                    full += delta
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
                self._write_sse(google_response("", calls, prompt=turn.prompt, completion=full, model=turn.model))
            self.wfile.flush()

        def _responses(self, raw: bytes) -> None:
            body = _json_object(raw)
            if body is None:
                self._write_json(400, _openai_error("Request body must be a JSON object."))
                return
            turn = bridge.prepare(responses_to_chat(body))
            self._model = turn.model
            if not turn.prompt.strip():
                self._write_json(400, _openai_error("empty input"))
                return
            response_id = "resp_" + uuid.uuid4().hex[:16]
            message_id = "msg_" + uuid.uuid4().hex[:12]
            if not turn.stream:
                text = "".join(bridge.reply(turn))
                prose, calls = parse_tool_calls(text) if turn.tools else (text, None)
                payload = completed_response(response_id, turn.model, prose, calls, turn.prompt, text, message_id)
                self._write_json(200, json.dumps(payload).encode())
                return
            self._start_sse()
            sequence = 0

            def emit(event_type: str, **fields: Any) -> None:
                nonlocal sequence
                sequence += 1
                payload = {"type": event_type, "sequence_number": sequence, **fields}
                self.wfile.write(f"event: {event_type}\n".encode())
                self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()

            emit("response.created", response=created_event(response_id, turn.model))
            held = ""
            full = ""
            prose_parts: list[str] = []
            opened = False
            for delta in bridge.reply(turn):
                full += delta
                if not turn.tools:
                    if not opened:
                        emit("response.output_item.added", output_index=0, item=_pending_message(message_id))
                        opened = True
                    prose_parts.append(delta)
                    emit(
                        "response.output_text.delta",
                        item_id=message_id,
                        output_index=0,
                        content_index=0,
                        delta=delta,
                    )
                    continue
                held += delta
                emit_text, held = withhold_tool_fence(held)
                if emit_text:
                    if not opened:
                        emit("response.output_item.added", output_index=0, item=_pending_message(message_id))
                        opened = True
                    prose_parts.append(emit_text)
                    emit(
                        "response.output_text.delta",
                        item_id=message_id,
                        output_index=0,
                        content_index=0,
                        delta=emit_text,
                    )
            calls = None
            if turn.tools and "```tool_call" in held:
                prose, calls = parse_tool_calls(held)
            else:
                prose = held
            if prose:
                if not opened:
                    emit("response.output_item.added", output_index=0, item=_pending_message(message_id))
                    opened = True
                prose_parts.append(prose)
                emit(
                    "response.output_text.delta",
                    item_id=message_id,
                    output_index=0,
                    content_index=0,
                    delta=prose,
                )
            text_out = "".join(prose_parts)
            if not opened and not calls:
                emit("response.output_item.added", output_index=0, item=_pending_message(message_id))
                opened = True
            output_index = 0
            output: list[dict[str, Any]] = []
            if opened:
                emit(
                    "response.output_text.done",
                    item_id=message_id,
                    output_index=0,
                    content_index=0,
                    text=text_out,
                )
                item = {
                    "type": "message",
                    "id": message_id,
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text_out, "annotations": []}],
                }
                emit("response.output_item.done", output_index=0, item=item)
                output.append(item)
                output_index = 1
            for call in calls or []:
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                pending = {
                    "type": "function_call",
                    "id": call.get("id"),
                    "call_id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": "",
                    "status": "in_progress",
                }
                emit("response.output_item.added", output_index=output_index, item=pending)
                arguments = function.get("arguments") if isinstance(function.get("arguments"), str) else "{}"
                emit(
                    "response.function_call_arguments.done",
                    item_id=call.get("id"),
                    output_index=output_index,
                    arguments=arguments,
                )
                done = {
                    "type": "function_call",
                    "id": call.get("id"),
                    "call_id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": arguments,
                    "status": "completed",
                }
                emit("response.output_item.done", output_index=output_index, item=done)
                output.append(done)
                output_index += 1
            finished = completed_response(response_id, turn.model, text_out, calls, turn.prompt, full, message_id)
            finished["output"] = output
            emit("response.completed", response=finished)

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
            self._status = 200
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors_header()
            self.end_headers()

        def _fail(self, status: int, message: str) -> None:
            translated = translate_upstream_error(status, _openai_error(message))
            if translated is not None:
                self._write_json(translated[0], translated[1])
                return
            self._write_json(status, _openai_error(message))

        def _write_json(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self._status = status
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors_header()
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def _cors_header(self) -> None:
            if bridge.settings.cors:
                self.send_header("Access-Control-Allow-Origin", "*")

        def _log_request(self, started: float) -> None:
            if not bridge.settings.log_requests:
                return
            path = self.path.split("?", 1)[0]
            elapsed = time.monotonic() - started
            sys.stderr.write(
                f"{time.strftime('%H:%M:%S')} {self.command} {path} {self._status} {self._model} {elapsed:.3f}s\n"
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def _pending_message(message_id: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": message_id,
        "role": "assistant",
        "status": "in_progress",
        "content": [],
    }


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    encoding = handler.headers.get("Transfer-Encoding", "")
    if "chunked" in encoding.lower():
        return _read_chunked(handler.rfile)
    length = int(handler.headers.get("Content-Length", "0") or "0")
    return handler.rfile.read(length) if length else b""


def _read_chunked(rfile: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        size_line = rfile.readline()
        if not size_line:
            break
        size_text = size_line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise ValueError("invalid chunked request body") from exc
        if size == 0:
            while True:
                trailer = rfile.readline()
                if trailer in (b"\r\n", b"\n", b""):
                    break
            break
        chunks.append(rfile.read(size))
        rfile.read(2)
    return b"".join(chunks)


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
