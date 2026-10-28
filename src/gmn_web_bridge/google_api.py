# SPDX-License-Identifier: MIT
"""Google generateContent bodies mapped onto the same chat pipeline."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

from .openai_api import estimate_usage
from .tools import parse_tool_calls


def google_to_openai(model: str, body: dict[str, Any], stream: bool) -> dict[str, Any]:
    """Translate one generateContent request into a chat-completions body."""
    messages: list[dict[str, Any]] = []
    system = body.get("systemInstruction") or body.get("system_instruction")
    system_text = _parts_text(system.get("parts") if isinstance(system, dict) else None)
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for content in body.get("contents") or []:
        if not isinstance(content, dict):
            continue
        messages.extend(_content_messages(content))
    tools = _function_tools(body.get("tools"))
    translated: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
    if tools:
        translated["tools"] = tools
    return translated


def google_response(
    text: str,
    tool_calls: list[dict[str, Any]] | None,
    *,
    prompt: str = "",
    completion: str = "",
    model: str = "",
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"text": text})
    for call in tool_calls or []:
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments") or "{}"
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                args = {"raw": arguments}
        else:
            args = arguments
        parts.append({"functionCall": {"name": function.get("name"), "args": args}})
    finish = "STOP"
    usage = estimate_usage(prompt, completion or text)
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts or [{"text": ""}]},
                "finishReason": finish,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": usage["prompt_tokens"],
            "candidatesTokenCount": usage["completion_tokens"],
            "totalTokenCount": usage["total_tokens"],
        },
        "modelVersion": model,
    }


def google_text_chunk(text: str) -> dict[str, Any]:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}


def split_reply(reply: str) -> tuple[str, list[dict[str, Any]] | None]:
    return parse_tool_calls(reply)


def model_from_path(path: str) -> tuple[str, str]:
    """Return `(model, action)` from `/v1beta/models/<model>:<action>`."""
    stripped = path.split("?", 1)[0].rstrip("/")
    marker = "/models/"
    if marker not in stripped:
        return "", ""
    rest = unquote(stripped.split(marker, 1)[1])
    if ":" not in rest:
        return rest.removeprefix("models/"), ""
    model, action = rest.split(":", 1)
    return model.removeprefix("models/"), action


def _content_messages(content: dict[str, Any]) -> list[dict[str, Any]]:
    role = content.get("role") or "user"
    if role == "model":
        role = "assistant"
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    messages: list[dict[str, Any]] = []
    text_parts: list[Any] = []
    tool_calls: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            text_parts.append({"type": "text", "text": part["text"]})
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and isinstance(inline.get("data"), str):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            text_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{inline['data']}"},
                }
            )
            continue
        call = part.get("functionCall") or part.get("function_call")
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            tool_calls.append(
                {
                    "id": f"call_{len(tool_calls)}_{call['name']}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                    },
                }
            )
            continue
        response = part.get("functionResponse") or part.get("function_response")
        if isinstance(response, dict) and isinstance(response.get("name"), str):
            if text_parts or tool_calls:
                messages.append(_assistant(role, text_parts, tool_calls))
                text_parts, tool_calls = [], []
            payload = response.get("response")
            messages.append(
                {
                    "role": "tool",
                    "name": response["name"],
                    "tool_call_id": f"call_{response['name']}",
                    "content": payload if isinstance(payload, str) else json.dumps(payload or {}, ensure_ascii=False),
                }
            )
    if text_parts or tool_calls or not messages:
        messages.append(_assistant(role, text_parts, tool_calls))
    return messages


def _assistant(role: str, text_parts: list[Any], tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": text_parts or ""}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _parts_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    texts = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return "\n".join(texts)


def _function_tools(tools: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        declarations = tool.get("functionDeclarations") or tool.get("function_declarations") or []
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if isinstance(declaration, dict) and isinstance(declaration.get("name"), str):
                converted.append({"type": "function", "function": declaration})
    return converted
