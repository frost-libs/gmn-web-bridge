#!/bin/sh
# Validate the session environment and run the bridge. Secrets stay in the
# process environment and are never printed.
set -eu

if [ -z "${GMN_COOKIE:-}" ]; then
  echo "GMN_COOKIE is required. Run scripts/gmn-secrets.sh setup, then scripts/gmn-secrets.sh up." >&2
  exit 1
fi

if [ -z "${GMN_API_KEY:-}" ]; then
  echo "GMN_API_KEY is required. Run scripts/gmn-secrets.sh setup, then scripts/gmn-secrets.sh up." >&2
  exit 1
fi

export GMN_AUTH_USER="${GMN_AUTH_USER:-0}"
export GMN_TEMPORARY_CHATS="${GMN_TEMPORARY_CHATS:-true}"
export GMN_PORT="${GMN_PORT:-8091}"
export GMN_HOST="${GMN_HOST:-0.0.0.0}"

exec python -m gmn_web_bridge
