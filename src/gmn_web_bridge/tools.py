# SPDX-License-Identifier: MIT
"""Render tools into the prompt and parse ```tool_call fences from the reply."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import unquote_to_bytes

FENCE = "```tool_call"

_PREAMBLE = (
    "You can call tools. To call a tool, end your reply with one fenced block "
    "and write nothing after it:\n\n"
    "```tool_call\n"
    '{"name": "function_name", "arguments": {}}\n'
    "```\n\n"
    "Available tools:\n"
)


def withhold_tool_fence(held: str) -> tuple[str, str]:
    """Return text that cannot be part of a tool call, and the suffix still in doubt."""
    cut = held.find(FENCE)
    if cut != -1:
        return held[:cut], held[cut:]
    keep = 0
    for size in range(min(len(FENCE) - 1, len(held)), 0, -1):
        if FENCE.startswith(held[-size:]):
            keep = size
            break
    if keep:
        return held[:-keep], held[-keep:]
    return held, ""


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]] | None]:
    """Split prose from one or more tool-call fences. A bad fence stays as text."""
    if FENCE not in text:
        return text, None
    prose, rest = text.split(FENCE, 1)
    body, _, _after = rest.partition("```")
    try:
        payload = json.loads(body.strip())
    except json.JSONDecodeError:
        return text, None
    items = payload if isinstance(payload, list) else [payload]
    calls: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            continue
        arguments = item.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(
            {
                "id": f"call_{len(calls)}_{item['name']}",
                "type": "function",
                "function": {"name": item["name"], "arguments": arguments},
            }
        )
    if not calls:
        return text, None
    return prose.rstrip(), calls


def render_conversation(messages: list[Any], tools: Any) -> tuple[str, list[tuple[bytes | str, str, str]]]:
    """Fold history into one prompt. Images are returned separately for upload."""
    images: list[tuple[bytes, str, str]] = []
    blocks: list[str] = []
    if tools:
        blocks.append(_PREAMBLE + json.dumps(tools, ensure_ascii=False))
    for message in messages:
        blocks.append(_render_message(message, images))
    return "\n\n".join(block for block in blocks if block), images


def _render_message(message: Any, images: list[tuple[bytes | str, str, str]]) -> str:
    if not isinstance(message, dict):
        return str(message)
    role = str(message.get("role") or "user")
    name = message.get("name")
    label = f"{role} {name}" if isinstance(name, str) and name else role
    extra = ""
    if message.get("tool_calls"):
        extra = "\n" + _calls_as_fence(message["tool_calls"])
    return f"[{label}]: {_content_text(message.get('content'), images)}{extra}"


def _calls_as_fence(tool_calls: Any) -> str:
    calls: list[dict[str, Any]] = []
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            calls.append({"name": function.get("name"), "arguments": arguments})
    body = json.dumps(calls[0] if len(calls) == 1 else calls, ensure_ascii=False)
    return f"{FENCE}\n{body}\n```"


def _content_text(content: Any, images: list[tuple[bytes | str, str, str]]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            image = _image_bytes(part)
            if image is not None:
                images.append(image)
                parts.append("[image]")
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return str(content)


def _image_name(mime: str) -> str:
    extension = mime.split("/")[-1] or "bin"
    return f"image.{extension}"


def _decode_data_url(url: str, mime_hint: str) -> tuple[bytes, str, str] | None:
    header, _, data = url.partition(",")
    mime = header[5:].split(";", 1)[0] or mime_hint or "application/octet-stream"
    try:
        raw = base64.b64decode(data, validate=True) if ";base64" in header else unquote_to_bytes(data)
    except (ValueError, TypeError):
        return None
    return raw, mime, _image_name(mime)


def _image_bytes(part: dict[str, Any]) -> tuple[bytes | str, str, str] | None:
    url = None
    mime_hint = ""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        candidate = image_url.get("url")
        url = candidate if isinstance(candidate, str) else None
        hint = image_url.get("mime_type") or image_url.get("mimeType")
        if isinstance(hint, str):
            mime_hint = hint
    elif isinstance(image_url, str):
        url = image_url
    if part.get("type") in ("input_image", "image"):
        if url is None and isinstance(part.get("url"), str):
            url = part["url"]
        if url is None and isinstance(part.get("image_url"), str):
            url = part["image_url"]
        hint = part.get("mime_type") or part.get("mimeType") or part.get("media_type")
        if isinstance(hint, str) and not mime_hint:
            mime_hint = hint
        raw_b64 = part.get("data") or part.get("base64")
        if url is None and isinstance(raw_b64, str):
            if raw_b64.startswith("data:"):
                url = raw_b64
            else:
                try:
                    raw = base64.b64decode(raw_b64, validate=True)
                except (ValueError, TypeError):
                    return None
                mime = mime_hint or "image/png"
                return raw, mime, _image_name(mime)
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        return _decode_data_url(url, mime_hint)
    if url.startswith(("http://", "https://")):
        mime = mime_hint or "image/png"
        return url, mime, _image_name(mime)
    return None
