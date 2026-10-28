# SPDX-License-Identifier: MIT
"""OpenAI Responses API bodies mapped onto the chat pipeline."""

from __future__ import annotations

import json
import time
from typing import Any

from .openai_api import estimate_usage


def responses_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Translate one Responses request into a chat-completions body."""
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    messages.extend(_input_messages(body.get("input")))
    translated: dict[str, Any] = {
        "model": body.get("model") if isinstance(body.get("model"), str) else "",
        "messages": messages,
        "stream": body.get("stream") is True,
    }
    tools = _responses_tools(body.get("tools"))
    if tools:
        translated["tools"] = tools
    return translated


def completed_response(
    response_id: str,
    model: str,
    prose: str,
    calls: list[dict[str, Any]] | None,
    prompt: str,
    completion: str,
    message_id: str,
) -> dict[str, Any]:
    """A finished Responses object. Text is omitted when the reply is only tool calls."""
    output: list[dict[str, Any]] = []
    if prose or not calls:
        output.append(_message_item(message_id, prose, "completed"))
    for call in calls or []:
        output.append(_function_item(call, "completed"))
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": _responses_usage(prompt, completion),
    }


def created_event(response_id: str, model: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
    }


def _responses_usage(prompt: str, completion: str) -> dict[str, int]:
    usage = estimate_usage(prompt, completion)
    return {
        "input_tokens": usage["prompt_tokens"],
        "output_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
    }


def _message_item(message_id: str, text: str, status: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": message_id,
        "role": "assistant",
        "status": status,
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _function_item(call: dict[str, Any], status: str) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    call_id = call.get("id") if isinstance(call.get("id"), str) else ""
    arguments = function.get("arguments") if isinstance(function.get("arguments"), str) else "{}"
    return {
        "type": "function_call",
        "id": call_id,
        "call_id": call_id,
        "name": function.get("name") if isinstance(function.get("name"), str) else "",
        "arguments": arguments,
        "status": status,
    }


def _input_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        return [{"role": "user", "content": payload}]
    if not isinstance(payload, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call_output":
            output = item.get("output", "")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") if isinstance(item.get("call_id"), str) else "",
                    "name": item.get("name") if isinstance(item.get("name"), str) else "",
                    "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                }
            )
            continue
        if kind == "function_call":
            messages.append({"role": "assistant", "content": None, "tool_calls": [_flat_call(item)]})
            continue
        if kind in ("input_text", "input_image", "image"):
            messages.append({"role": "user", "content": [item]})
            continue
        if item.get("role") == "assistant" or (kind == "message" and item.get("role") == "assistant"):
            messages.append(_assistant_item(item))
            continue
        role = item.get("role") if isinstance(item.get("role"), str) else "user"
        messages.append({"role": role, "content": item.get("content", "")})
    return messages


def _assistant_item(item: dict[str, Any]) -> dict[str, Any]:
    content = item.get("content", "")
    text = ""
    calls: list[dict[str, Any]] = []
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text += part["text"]
            elif part.get("type") == "function_call":
                calls.append(_flat_call(part))
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls
    return message


def _flat_call(item: dict[str, Any]) -> dict[str, Any]:
    call_id = item.get("call_id") or item.get("id") or ""
    if not isinstance(call_id, str):
        call_id = ""
    arguments = item.get("arguments", "{}")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    name = item.get("name") if isinstance(item.get("name"), str) else ""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _responses_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        if isinstance(tool.get("function"), dict):
            converted.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = tool.get("description")
        parameters = tool.get("parameters")
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description if isinstance(description, str) else "",
                    "parameters": parameters if isinstance(parameters, dict) else {},
                },
            }
        )
    return converted
