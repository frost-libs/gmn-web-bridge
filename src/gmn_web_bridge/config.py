# SPDX-License-Identifier: MIT
"""Process settings. Every value comes from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _text(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _int(name: str, default: int, *, allow_zero: bool = False) -> int:
    raw = _text(name)
    if raw == "":
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


def _bool(name: str, default: bool) -> bool:
    raw = _text(name)
    if raw == "":
        return default
    return raw.lower() not in {"0", "false", "no"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    api_key: str
    cookie: str
    xsrf_token: str
    auth_user: str
    temporary_chats: bool
    request_timeout_sec: int
    prompt_char_limit: int
    aliases_path: str
    build_label: str
    retry_attempts: int
    retry_delay_sec: float
    default_model: str
    summary_model: str
    send_model_header: bool

    @classmethod
    def from_env(cls) -> Settings:
        delay = _text("GMN_RETRY_DELAY_SEC", "2")
        try:
            retry_delay = float(delay)
        except ValueError:
            retry_delay = 2.0
        if retry_delay < 0:
            retry_delay = 2.0
        return cls(
            host=_text("GMN_HOST", "0.0.0.0"),
            port=_int("GMN_PORT", 8091),
            api_key=_text("GMN_API_KEY"),
            cookie=_text("GMN_COOKIE"),
            xsrf_token=_text("GMN_XSRF_TOKEN"),
            auth_user=_text("GMN_AUTH_USER", "0") or "0",
            temporary_chats=_bool("GMN_TEMPORARY_CHATS", True),
            request_timeout_sec=_int("GMN_REQUEST_TIMEOUT_SEC", 180),
            prompt_char_limit=_int("GMN_PROMPT_CHAR_LIMIT", 32000, allow_zero=True),
            aliases_path=_text("GMN_MODEL_ALIASES"),
            build_label=_text("GMN_BL"),
            retry_attempts=_int("GMN_RETRY_ATTEMPTS", 3),
            retry_delay_sec=retry_delay,
            default_model=_text("GMN_DEFAULT_MODEL", "gemini-3.8-flash"),
            summary_model=_text("GMN_SUMMARY_MODEL", "gemini-3.5-flash-lite"),
            send_model_header=_bool("GMN_SEND_MODEL_HEADER", False),
        )
