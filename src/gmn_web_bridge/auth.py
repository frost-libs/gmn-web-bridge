# SPDX-License-Identifier: MIT
"""Cookie, account index, and the tokens embedded in the signed-in page."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PAGE_TOKEN = re.compile(
    r'"(SNlM0e|thykhd|cfb2h|FdrFJe|qKIAYe|Ylro7b)"\s*:\s*"([^"\\]*)"'
)


@dataclass
class PageTokens:
    at_token: str | None
    build_label: str | None
    session_id: str | None
    push_id: str | None
    client_pctx: str | None


def parse_cookie(cookie: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        pairs[name.strip()] = value.strip()
    return pairs


def sapisid(cookie: str) -> str | None:
    pairs = parse_cookie(cookie)
    return pairs.get("SAPISID") or pairs.get("sapisid") or None


def parse_page_tokens(html: str) -> PageTokens:
    found: dict[str, str] = {}
    for key, value in _PAGE_TOKEN.findall(html):
        found.setdefault(key, value)
    at_token = found.get("SNlM0e") or found.get("thykhd") or None
    return PageTokens(
        at_token=at_token or None,
        build_label=found.get("cfb2h") or None,
        session_id=found.get("FdrFJe") or None,
        push_id=found.get("qKIAYe") or None,
        client_pctx=found.get("Ylro7b") or None,
    )


def app_path(auth_user: str) -> str:
    user = auth_user.strip() or "0"
    return f"/u/{user}/app"
