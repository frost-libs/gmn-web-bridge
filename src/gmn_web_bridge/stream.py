# SPDX-License-Identifier: MIT
"""Turn Gemini's cumulative text snapshots into stream deltas."""

from __future__ import annotations

import json
import re

_SCAFFOLD = re.compile(
    r"```(?:python|text)\?code_(?:reference|stdout)[^\n]*\n.*?```",
    re.DOTALL,
)
_BARD_ERROR = re.compile(r"BardErrorInfo\s*\[(\d+)\]")


class StreamRewritten(Exception):
    """A new snapshot replaced text that was already sent to the client."""


class ProtocolError(Exception):
    """Gemini closed the stream with a protocol or Bard error code."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Gemini upstream rejected request: {code}")
        self.code = code


def accept_snapshot(previous: str, sent: bool, texts: list[str]) -> tuple[str, str]:
    """Return ``(raw_text, raw_suffix)`` for the next snapshot.

    ``raw_suffix`` is empty when the line adds no new answer text. A snapshot
    that extends ``previous`` wins over an unrelated string in the same line.
    A replacement is adopted only when nothing has been sent yet. After text
    has been sent, a replacement raises ``StreamRewritten``.
    """
    candidates = [text for text in texts if text]
    if not candidates:
        return previous, ""

    if not previous:
        snapshot = max(candidates, key=len)
        return snapshot, snapshot

    extensions = [text for text in candidates if text.startswith(previous) and text != previous]
    if extensions:
        snapshot = max(extensions, key=len)
        return snapshot, snapshot[len(previous) :]

    replacements = [text for text in candidates if text != previous and not previous.startswith(text)]
    if not replacements:
        return previous, ""
    if sent:
        raise StreamRewritten()
    snapshot = max(replacements, key=len)
    return snapshot, snapshot


def clean_text(text: str) -> str:
    """Drop Gemini's hidden code-execution scaffolding. Real code fences stay."""
    return _SCAFFOLD.sub("", text)


def bard_error(buf: str) -> str | None:
    match = _BARD_ERROR.search(buf)
    if match is None:
        return None
    return match.group(1)


def texts_from_line(line: str) -> list[str]:
    """Answer snapshots carried by one response line. Length prefixes yield nothing."""
    stripped = line.strip()
    if not stripped.startswith("["):
        return []
    try:
        outer = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    if not isinstance(outer, list):
        return []
    found: list[str] = []
    for event in outer:
        if not isinstance(event, list) or not event:
            continue
        if event[0] == "e":
            if _is_protocol_error(event):
                raise ProtocolError(str(event[-1]))
            continue
        if len(event) < 3 or not isinstance(event[2], str) or not event[2].startswith("["):
            continue
        try:
            inner = json.loads(event[2])
        except json.JSONDecodeError:
            continue
        text = _nested(inner, (4, 0, 1, 0))
        if isinstance(text, str) and text:
            found.append(text)
    return found


def _is_protocol_error(event: list) -> bool:
    """True for the short error event. A trailing size on a normal stream is not an error."""
    if len(event) < 5 or not isinstance(event[-1], int):
        return False
    return event[1] in (4, 5) and event[-1] < 1000


def _nested(value: object, path: tuple[int, ...]) -> object:
    current = value
    for index in path:
        if not isinstance(current, list) or index >= len(current):
            return None
        current = current[index]
    return current
