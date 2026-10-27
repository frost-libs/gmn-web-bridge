# SPDX-License-Identifier: MIT
"""Prompt character guard. Older turns become one summary."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from typing import Any, Callable

DEFAULT_PROMPT_CHAR_LIMIT = 32000

DEFAULT_REQUEST_TIMEOUT_SEC = 180

SUMMARY_MODEL = "gemini-3.5-flash-lite"

_SUMMARY_CACHE_SIZE = 32

_summary_cache_lock = threading.Lock()
_summary_cache: OrderedDict[str, str] = OrderedDict()

_SUMMARY_INSTRUCTION = (
    "Summarize this coding conversation for a later turn. "
    "Preserve file paths, decisions, errors, and open questions. "
    "Do not call tools."
)

_SUMMARY_PREFIX = "[Earlier conversation summary]\n"

_OMITTED_NOTE = "The start of this excerpt was omitted.\n"

_END_OMITTED_NOTE = "The end of this excerpt was omitted.\n"

_IMAGE_PLACEHOLDER = "[image]"

class PromptTooLarge(Exception):
    """Tools and message text still exceed the character guard after trimming."""

class SummaryFailed(Exception):
    """The summary call failed. The trimmed thread must not be forwarded."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

def prompt_char_limit() -> int:
    """Character guard for one web prompt. Zero disables compaction."""
    return _positive_int_env("GMN_PROMPT_CHAR_LIMIT", DEFAULT_PROMPT_CHAR_LIMIT, allow_zero=True)

def request_timeout_sec() -> int:
    """Shared request timeout, in seconds."""
    return _positive_int_env("GMN_REQUEST_TIMEOUT_SEC", DEFAULT_REQUEST_TIMEOUT_SEC, allow_zero=False)

def _positive_int_env(name: str, default: int, allow_zero: bool) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value == 0 and allow_zero:
        return 0
    if value <= 0:
        return default
    return value

def estimate_prompt_chars(messages: Any, tools: Any) -> int:
    """Approximate the prompt that will be pasted, without image bytes."""
    total = 0
    if isinstance(messages, list):
        for message in messages:
            total += _message_chars(message)
    if tools:
        total += len(json.dumps(tools, ensure_ascii=False))
    return total

def compact_chat_body(body: dict[str, Any], limit: int, summarize) -> dict[str, Any]:
    """Replace dropped turns with one summary. `summarize` receives the clipped text.

    A turn starts at a user message and includes the assistant and tool messages
    that follow it. When the latest turn still does not fit, its text is trimmed
    to the guard. System text keeps its start. Other text keeps its end.
    """
    messages = body.get("messages")
    if limit <= 0 or not isinstance(messages, list):
        return body
    tools = body.get("tools")
    if estimate_prompt_chars(messages, tools) <= limit:
        return body
    message_chars = estimate_prompt_chars(messages, None)
    if tools:
        tool_budget = limit - message_chars if message_chars < limit else limit // 4
        if _tools_chars(tools) > tool_budget:
            body = dict(body)
            body["tools"] = _shrink_tools_to(tools, tool_budget)
            tools = body["tools"]
            if estimate_prompt_chars(messages, tools) <= limit:
                return body
    systems, turns = _system_and_turns(messages)
    if not turns:
        return _with_messages(body, _clip_messages_to_limit(systems, tools, limit))
    suffix_count = _fitting_suffix_count(systems, turns, tools, limit)
    if suffix_count is None:
        kept = [message for message in turns[-1]]
        return _with_messages(body, _clip_messages_to_limit([*systems, *kept], tools, limit))
    while suffix_count > 1:
        kept = [message for turn in turns[-suffix_count:] for message in turn]
        if limit - estimate_prompt_chars([*systems, *kept], tools) > len(_SUMMARY_PREFIX):
            break
        suffix_count -= 1
    kept = [message for turn in turns[-suffix_count:] for message in turn]
    dropped = [message for turn in turns[:-suffix_count] for message in turn]
    if not dropped or limit - estimate_prompt_chars([*systems, *kept], tools) <= len(_SUMMARY_PREFIX):
        return _with_messages(body, [*systems, *kept])
    summary = summarize(_summary_source(_render_messages(dropped), limit))
    if not isinstance(summary, str) or not summary.strip():
        raise SummaryFailed(502, "Summary request returned no text.")
    content = _fit_summary(summary.strip(), systems, kept, tools, limit)
    return _with_messages(body, [*systems, {"role": "system", "content": content}, *kept])

def _fitting_suffix_count(
    systems: list[Any], turns: list[list[Any]], tools: Any, limit: int
) -> int | None:
    for count in range(len(turns), 0, -1):
        kept = [message for turn in turns[-count:] for message in turn]
        if estimate_prompt_chars([*systems, *kept], tools) <= limit:
            return count
    return None

def _with_messages(body: dict[str, Any], messages: list[Any]) -> dict[str, Any]:
    compacted = dict(body)
    compacted["messages"] = messages
    return compacted

def _clip_messages_to_limit(messages: list[Any], tools: Any, limit: int) -> list[Any]:
    """Trim text until the prompt fits.

    The last user message keeps its end, so a long context prefix goes before the
    question does. System text keeps its start. A user message that itself exceeds
    the guard still leaves the system text up to a quarter of the guard.
    """
    clipped = [_copy_message(message) for message in messages]
    text_budget = limit - _tools_chars(tools)
    if text_budget < 0:
        raise PromptTooLarge()
    last_user = _last_role_index(clipped, "user")
    if last_user is None:
        _fit_group(clipped, list(range(len(clipped))), text_budget, keep_tail=False)
    else:
        others = [index for index in range(len(clipped)) if index != last_user]
        user_chars = _message_chars(clipped[last_user])
        if user_chars >= text_budget:
            system_indexes = [index for index in others if _role(clipped[index]) == "system"]
            system_chars = sum(_message_chars(clipped[index]) for index in system_indexes)
            floor = min(system_chars, text_budget // 4)
            _fit_group(clipped, system_indexes, floor, keep_tail=False)
            for index in others:
                if index not in system_indexes:
                    _set_fitted_content(clipped[index], 0, keep_tail=True)
            user = clipped[last_user]
            user_fixed = _message_chars(user) - _content_chars(user.get("content") if isinstance(user, dict) else None)
            user_budget = text_budget - _group_chars(clipped, others) - user_fixed
            _set_fitted_content(user, user_budget, keep_tail=True)
        else:
            _fit_group(clipped, others, text_budget - user_chars, keep_tail=False)
    if estimate_prompt_chars(clipped, tools) > limit:
        raise PromptTooLarge()
    return clipped

def _copy_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    copied = dict(message)
    content = copied.get("content")
    if isinstance(content, list):
        copied["content"] = [dict(part) if isinstance(part, dict) else part for part in content]
    return copied

def _last_role_index(messages: list[Any], role: str) -> int | None:
    found: int | None = None
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == role:
            found = index
    return found

def _role(message: Any) -> str:
    if isinstance(message, dict) and isinstance(message.get("role"), str):
        return message["role"]
    return ""

def _tools_chars(tools: Any) -> int:
    if not tools:
        return 0
    return len(json.dumps(tools, ensure_ascii=False))

def _shrink_tools_to(tools: Any, max_chars: int) -> Any:
    """Drop tool description text, then whole tools, until the tool list fits."""
    if not tools or max_chars < 0 or _tools_chars(tools) <= max_chars:
        return tools
    copied = json.loads(json.dumps(tools))
    _delete_key(copied, "description")
    if _tools_chars(copied) <= max_chars:
        return copied
    _blank_parameters(copied)
    if _tools_chars(copied) <= max_chars or not isinstance(copied, list):
        return copied
    kept: list[Any] = []
    for tool in copied:
        if _tools_chars([*kept, tool]) > max_chars:
            break
        kept.append(tool)
    return kept

def _delete_key(node: Any, key: str) -> None:
    if isinstance(node, dict):
        node.pop(key, None)
        for value in node.values():
            _delete_key(value, key)
    elif isinstance(node, list):
        for item in node:
            _delete_key(item, key)

def _blank_parameters(tools: Any) -> None:
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            function["parameters"] = {"type": "object", "properties": {}}

def _group_chars(messages: list[Any], indexes: list[int]) -> int:
    return sum(_message_chars(messages[index]) for index in indexes)

def _fit_group(messages: list[Any], indexes: list[int], budget: int, keep_tail: bool) -> None:
    """Fit a group into `budget` characters. System groups keep the first message's start."""
    if not indexes:
        return
    if _group_chars(messages, indexes) <= budget:
        return
    favored = indexes[-1] if keep_tail else indexes[0]
    for index in indexes:
        if index != favored:
            _set_fitted_content(messages[index], 0, keep_tail=keep_tail)
    favored_fixed = _message_chars(messages[favored]) - _content_chars(messages[favored].get("content"))
    others = _group_chars(messages, [index for index in indexes if index != favored])
    _set_fitted_content(messages[favored], budget - others - favored_fixed, keep_tail=keep_tail)

def _set_fitted_content(message: Any, max_len: int, keep_tail: bool) -> None:
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = _fit_text(content, max_len, keep_tail=keep_tail)
        return
    if isinstance(content, list):
        texts = [index for index, part in enumerate(content) if isinstance(part, dict) and isinstance(part.get("text"), str)]
        if not texts:
            return
        favored = texts[-1] if keep_tail else texts[0]
        for index in texts:
            if index != favored:
                content[index]["text"] = ""
        content[favored]["text"] = _fit_text(content[favored]["text"], max_len, keep_tail=keep_tail)

def _fit_text(text: str, max_len: int, keep_tail: bool) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 0:
        return ""
    note = _OMITTED_NOTE if keep_tail else _END_OMITTED_NOTE
    if max_len <= len(note):
        return text[-max_len:] if keep_tail else text[:max_len]
    if keep_tail:
        return note + text[-(max_len - len(note)) :]
    return text[: max_len - len(note)] + note

def _system_and_turns(messages: list[Any]) -> tuple[list[Any], list[list[Any]]]:
    systems: list[Any] = []
    turns: list[list[Any]] = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            systems.append(message)
            continue
        if isinstance(message, dict) and message.get("role") == "user":
            turns.append([message])
            continue
        if not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return systems, turns

def _fit_summary(summary: str, systems: list[Any], kept: list[Any], tools: Any, limit: int) -> str:
    room = limit - estimate_prompt_chars([*systems, *kept], tools)
    if room <= len(_SUMMARY_PREFIX):
        raise PromptTooLarge()
    body = summary[: room - len(_SUMMARY_PREFIX)]
    return _SUMMARY_PREFIX + body

def _summary_source(text: str, limit: int) -> str:
    budget = limit - len(_SUMMARY_INSTRUCTION)
    if budget <= 0:
        raise PromptTooLarge()
    if len(text) <= budget:
        return text
    if len(_OMITTED_NOTE) >= budget:
        return text[-budget:]
    return _OMITTED_NOTE + text[-(budget - len(_OMITTED_NOTE)) :]

def _render_messages(messages: list[Any]) -> str:
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            parts.append(str(message))
            continue
        role = str(message.get("role") or "user")
        name = message.get("name")
        label = f"{role} {name}" if isinstance(name, str) and name else role
        extra = ""
        if message.get("tool_calls"):
            extra = "\n" + json.dumps(message["tool_calls"], ensure_ascii=False)
        parts.append(f"[{label}]: {_plain_content(message.get('content'))}{extra}")
    return "\n\n".join(parts)

def _message_chars(message: Any) -> int:
    if not isinstance(message, dict):
        return len(str(message))
    total = _content_chars(message.get("content"))
    if message.get("tool_calls"):
        total += len(json.dumps(message["tool_calls"], ensure_ascii=False))
    name = message.get("name")
    if isinstance(name, str):
        total += len(name)
    return total

def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_part_chars(part) for part in content)
    return len(str(content))

def _part_chars(part: Any) -> int:
    if isinstance(part, str):
        return len(part)
    if not isinstance(part, dict):
        return len(str(part))
    if part.get("type") in ("input_image", "image_url", "image") or "image_url" in part:
        return len(_IMAGE_PLACEHOLDER)
    text = part.get("text")
    if isinstance(text, str):
        return len(text)
    return len(_IMAGE_PLACEHOLDER)

def _plain_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("input_image", "image_url", "image") or "image_url" in part:
                texts.append(_IMAGE_PLACEHOLDER)
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
        return "\n".join(texts)
    return str(content)

def fetch_summary(text: str, complete: Callable[[str], str], limit: int) -> str:
    """Ask the summary model for a summary. `complete` returns the reply text.

    The same dropped text reuses the summary kept in this process.
    """
    messages = [
        {"role": "system", "content": _SUMMARY_INSTRUCTION},
        {"role": "user", "content": text},
    ]
    if estimate_prompt_chars(messages, None) > limit:
        raise PromptTooLarge()
    cached = _cached_summary(text)
    if cached is not None:
        return cached
    try:
        summary = complete(text)
    except SummaryFailed:
        raise
    except Exception as exc:
        status = getattr(exc, "status", 502)
        message = getattr(exc, "message", None) or "Summary request failed."
        raise SummaryFailed(status if isinstance(status, int) else 502, message) from exc
    if not isinstance(summary, str) or not summary.strip():
        raise SummaryFailed(502, "Summary request returned no text.")
    _store_summary(text, summary.strip())
    return summary.strip()


def clear_summary_cache() -> None:
    """Drop remembered summaries. The cache also dies when the process exits."""
    with _summary_cache_lock:
        _summary_cache.clear()

def _summary_cache_key(text: str) -> str:
    raw = f"{SUMMARY_MODEL}\0{_SUMMARY_INSTRUCTION}\0{text}"
    return hashlib.sha256(raw.encode()).hexdigest()

def _cached_summary(text: str) -> str | None:
    key = _summary_cache_key(text)
    with _summary_cache_lock:
        summary = _summary_cache.get(key)
        if summary is None:
            return None
        _summary_cache.move_to_end(key)
        return summary

def _store_summary(text: str, summary: str) -> None:
    key = _summary_cache_key(text)
    with _summary_cache_lock:
        _summary_cache[key] = summary
        _summary_cache.move_to_end(key)
        while len(_summary_cache) > _SUMMARY_CACHE_SIZE:
            _summary_cache.popitem(last=False)
