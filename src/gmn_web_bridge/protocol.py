# SPDX-License-Identifier: MIT
"""StreamGenerate request body and model side channel. See docs/protocol.md."""

from __future__ import annotations

import hashlib
import json
import uuid

ORIGIN = "https://gemini.google.com"
STREAM_PATH = (
    "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
)
UPLOAD_URL = "https://push.clients6.google.com/upload/"
INNER_LENGTH = 80
MODEL_HEADER = "x-goog-ext-525001261-jspb"
REQUEST_HEADER = "x-goog-ext-525005358-jspb"


def native_thinking(level: int) -> int:
    """Map 0 (deepest) through 4 (shallowest) onto the web thinking enum."""
    if level <= 1:
        return 3
    if level <= 3:
        return 2
    return 1


def build_inner(
    prompt: str,
    mode: int,
    think: int,
    temporary: bool,
    file_refs: list[tuple[str, str]] | None,
    request_id: str,
) -> list:
    """80-element inner array. Unset slots stay null."""
    inner: list = [None] * INNER_LENGTH
    refs = [[[ref, 1], name] for ref, name in file_refs] if file_refs else None
    inner[0] = [prompt, 0, None, refs, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [1]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    if temporary:
        inner[45] = True
    inner[53] = 0
    inner[59] = request_id
    inner[61] = []
    inner[68] = 1
    inner[79] = mode
    return inner


def build_freq(inner: list) -> str:
    """Double-encoded `f.req` value."""
    encoded = json.dumps(inner, separators=(",", ":"))
    return json.dumps([None, encoded], separators=(",", ":"))


def model_header(token: str | None, mode: int, think: int, temporary: bool, request_id: str) -> str:
    """`x-goog-ext-525001261-jspb` value."""
    return json.dumps(
        [
            1,
            None,
            None,
            None,
            token,
            None,
            None,
            True if temporary else None,
            [4, 5, 6, 8, 4, 5, 6, 8],
            None,
            None,
            mode,
            None,
            None,
            mode,
            native_thinking(think),
            request_id,
        ],
        separators=(",", ":"),
    )


def new_request_id() -> str:
    return str(uuid.uuid4())


def sapisid_hash(sapisid: str, timestamp: int, origin: str = ORIGIN) -> str:
    digest = hashlib.sha1(f"{timestamp} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {timestamp}_{digest}"
