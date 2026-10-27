# SPDX-License-Identifier: MIT
"""OpenAI chat-completions helpers: aliases, tool names, and stream splitting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class UnknownPublicModel(Exception):
    """An alias that this process was configured to reject."""

class UnnamedToolResult(Exception):
    """A tool result has no function name and no matching earlier call."""

class Aliases:
    """Optional public model ids. Empty unless a JSON file is configured."""

    def __init__(self, models: dict[str, dict[str, str]], reject_prefix: str) -> None:
        self.models = models
        self.reject_prefix = reject_prefix

    def apply(self, model: str) -> str:
        public_id, think = _split_think(model)
        mapped = self.models.get(public_id)
        if mapped is not None:
            upstream = mapped["id"]
            return upstream if think is None else f"{upstream}@think={think}"
        if self.reject_prefix and public_id.startswith(self.reject_prefix):
            raise UnknownPublicModel(model)
        return model

    def list_entries(self) -> list[dict[str, str]]:
        return [
            {"id": key, "description": value.get("description", "")}
            for key, value in self.models.items()
        ]


def load_aliases(path: str | None) -> Aliases:
    """Read an alias file. A missing path means no aliases and no rejected prefix."""
    if not path:
        return Aliases({}, "")
    payload = json.loads(Path(path).read_text())
    models = payload.get("models") or {}
    cleaned: dict[str, dict[str, str]] = {}
    for key, value in models.items():
        if isinstance(value, str):
            cleaned[key] = {"id": value, "description": ""}
        elif isinstance(value, dict) and isinstance(value.get("id"), str):
            description = value.get("description")
            cleaned[key] = {
                "id": value["id"],
                "description": description if isinstance(description, str) else "",
            }
    prefix = payload.get("reject_prefix")
    return Aliases(cleaned, prefix if isinstance(prefix, str) else "")


def rewrite_chat_body(body: dict[str, Any], aliases: Aliases | None = None) -> dict[str, Any]:
    """Map a configured alias and fill tool-result names. Other fields stay.

    A tool result that has only tool_call_id gains the function name from the
    matching assistant call. A tool result with no name and no matching call
    is rejected.
    """
    return _with_tool_names(_apply_alias(body, aliases or Aliases({}, "")))


def _apply_alias(body: dict[str, Any], aliases: Aliases) -> dict[str, Any]:
    model = body.get("model")
    if not isinstance(model, str):
        return body
    rewritten_model = aliases.apply(model)
    if rewritten_model == model:
        return body
    rewritten = dict(body)
    rewritten["model"] = rewritten_model
    return rewritten


def _split_think(model: str) -> tuple[str, str | None]:
    if "@think=" not in model:
        return model, None
    public_id, think = model.rsplit("@think=", 1)
    return public_id, think

def _with_tool_names(body: dict[str, Any]) -> dict[str, Any]:
    named = _name_tool_results(body.get("messages"))
    if named is body.get("messages"):
        return body
    rewritten = dict(body)
    rewritten["messages"] = named
    return rewritten

def _name_tool_results(messages: Any) -> Any:
    """Copy tool messages that lack a name, using an earlier assistant tool call."""
    if not isinstance(messages, list):
        return messages
    names_by_id: dict[str, str] = {}
    rewritten: list[Any] | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            if rewritten is not None:
                rewritten.append(message)
            continue
        if message.get("role") == "assistant":
            _remember_tool_names(message.get("tool_calls"), names_by_id)
        elif message.get("role") == "tool" and not message.get("name"):
            call_id = message.get("tool_call_id")
            name = names_by_id.get(call_id) if isinstance(call_id, str) else None
            if not name:
                raise UnnamedToolResult("A tool result has no function name and no matching tool call.")
            if rewritten is None:
                rewritten = list(messages[:index])
            copied = dict(message)
            copied["name"] = name
            rewritten.append(copied)
            continue
        if rewritten is not None:
            rewritten.append(message)
    return messages if rewritten is None else rewritten

def _remember_tool_names(tool_calls: Any, names_by_id: dict[str, str]) -> None:
    if not isinstance(tool_calls, list):
        return
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names_by_id[call_id] = name

def rewrite_tool_sse(raw: bytes) -> bytes:
    """Split a streamed tool reply so tool_calls are not sitting on a content delta.

    Some clients return as soon as delta.content is non-empty and never read
    tool_calls on that same chunk. A prose-only chunk is left as text.
    """
    lines = raw.splitlines(keepends=True)
    rewritten: list[bytes] = []
    for line in lines:
        payload = _sse_data(line)
        if payload is None or payload == b"[DONE]":
            rewritten.append(line)
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue
        chunks = _split_tool_chunk(event)
        if chunks is None:
            rewritten.append(line)
            continue
        for chunk in chunks:
            rewritten.append(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
    return b"".join(rewritten)

def _sse_data(line: bytes) -> bytes | None:
    stripped = line.lstrip()
    if not stripped.startswith(b"data:"):
        return None
    return stripped[5:].strip()

def _split_tool_chunk(event: Any) -> list[dict[str, Any]] | None:
    if not isinstance(event, dict):
        return None
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return None
    tool_calls = _indexed_tool_calls(delta.get("tool_calls"))
    if not tool_calls:
        return None

    content = delta.get("content")
    has_content = isinstance(content, str) and content
    finish = choice.get("finish_reason")
    tool_delta = {key: value for key, value in delta.items() if key != "content"}
    tool_delta["tool_calls"] = tool_calls
    if not has_content:
        return [_choice_event(event, choice, tool_delta, finish)]

    content_delta = {key: value for key, value in delta.items() if key != "tool_calls"}
    content_delta["content"] = content
    return [
        _choice_event(event, choice, content_delta, None),
        _choice_event(event, choice, tool_delta, finish),
    ]

def _choice_event(
    event: dict[str, Any],
    choice: dict[str, Any],
    delta: dict[str, Any],
    finish_reason: str | None,
) -> dict[str, Any]:
    copied_choice = dict(choice)
    copied_choice["delta"] = delta
    copied_choice["finish_reason"] = finish_reason
    copied = dict(event)
    copied["choices"] = [copied_choice]
    return copied

def _indexed_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    indexed: list[dict[str, Any]] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        indexed.append(
            {
                "index": len(indexed),
                "id": call.get("id"),
                "type": call.get("type") or "function",
                "function": {
                    "name": function.get("name"),
                    "arguments": function.get("arguments"),
                },
            }
        )
    return indexed

def translate_upstream_error(status: int, raw: bytes) -> tuple[int, bytes] | None:
    """Map a web or session failure to a short OpenAI error. None means pass through."""
    del status
    stripped = raw.lstrip()
    head = stripped[:64].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return 502, _openai_error("Gemini returned a web page instead of a completion.")
    message = _error_message(raw)
    if not message:
        return None
    lowered = message.lower()
    if "xsrf" in lowered or _is_session_failure(lowered):
        return 401, _openai_error(
            "Gemini session rejected the request. Refresh the cookie and the XSRF token, then run setup again."
        )
    if "context window" in lowered or "context-window" in lowered or "prompt exceed" in lowered:
        return 400, _openai_error("The prompt exceeds Gemini's context window.")
    return None

def _is_session_failure(lowered: str) -> bool:
    return any(
        phrase in lowered
        for phrase in ("cookie", "unauthorized", "not authenticated", "invalid authentication", "sapisid")
    )

def _error_message(raw: bytes) -> str | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return message if isinstance(message, str) else None
    if isinstance(error, str):
        return error
    return None

def _openai_error(message: str) -> bytes:
    return json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode()
