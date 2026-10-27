# SPDX-License-Identifier: MIT
"""Run the bridge: `python -m gmn_web_bridge`."""

from __future__ import annotations

from .config import Settings
from .server import serve


def main() -> None:
    settings = Settings.from_env()
    if not settings.api_key:
        raise SystemExit("GMN_API_KEY is required.")
    if not settings.cookie:
        raise SystemExit("GMN_COOKIE is required.")
    serve(settings)


if __name__ == "__main__":
    main()
