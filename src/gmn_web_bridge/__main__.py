# SPDX-License-Identifier: MIT
"""Run the bridge: `python -m gmn_web_bridge`."""

from __future__ import annotations

import sys

from .config import Settings
from .server import serve


def startup_error(settings: Settings) -> str | None:
    """Reject a process that cannot serve. None means it may listen."""
    if not settings.api_key:
        return "GMN_API_KEY is required."
    if settings.mode == "local" and not settings.cookie:
        return "GMN_COOKIE is required."
    return None


def main() -> None:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    error = startup_error(settings)
    if error:
        raise SystemExit(error)
    if settings.mode == "anonymous" and not settings.cookie:
        sys.stderr.write("anonymous Gemini session (no cookie)\n")
    serve(settings)


if __name__ == "__main__":
    main()
