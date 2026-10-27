#!/usr/bin/env bash
# Store the Gemini web session and the local bearer key, then start the bridge.
# Never prints secret values.
#
# Usage:
#   gmn-secrets.sh setup [--copy-from PREFIX] [--env-file PATH]
#   gmn-secrets.sh status [--env-file PATH]
#   gmn-secrets.sh up [--env-file PATH]
#   gmn-secrets.sh down
#   gmn-secrets.sh print [--env-file PATH]
#   gmn-secrets.sh check [--env-file PATH]
#
# Keychain item names are "<prefix>-cookie", "<prefix>-xsrf", "<prefix>-auth-user",
# and "<prefix>-api-key". The prefix defaults to gmn-web-bridge (GMN_KEYCHAIN_PREFIX).
# --copy-from reads another prefix and copies the values. It does not delete them.
# --env-file stores the same values in a mode 600 file when the macOS Keychain
# is not available.

set -euo pipefail

PREFIX="${GMN_KEYCHAIN_PREFIX:-gmn-web-bridge}"
ACCOUNT="gmn-web-bridge"
IMAGE="gmn-web-bridge:dev"
CONTAINER="gmn-web-bridge"
PORT="${GMN_PORT:-8091}"

if [ -n "${BASH_SOURCE[0]:-}" ]; then
  _self="${BASH_SOURCE[0]}"
else
  _self="$0"
fi
_dir="$(cd "$(dirname "$_self")" && pwd)"
_root="$(cd "${_dir}/.." && pwd)"

_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
  case "${ZSH_EVAL_CONTEXT:-}" in
    *:file*) _sourced=1 ;;
  esac
elif [ -n "${BASH_VERSION:-}" ]; then
  if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    _sourced=1
  fi
fi
if [ "$_sourced" -eq 1 ]; then
  echo "Do not source this script. Run: ${_root}/scripts/gmn-secrets.sh setup|status|up|down|print|check" >&2
  return 1 2>/dev/null || exit 1
fi

item() {
  printf '%s-%s' "$PREFIX" "$1"
}

have_security() {
  command -v security >/dev/null 2>&1
}

read_secret() {
  security find-generic-password -s "$1" -w 2>/dev/null || true
}

item_present() {
  security find-generic-password -s "$1" >/dev/null 2>&1
}

store_secret() {
  security delete-generic-password -s "$1" >/dev/null 2>&1 || true
  security add-generic-password -a "$ACCOUNT" -s "$1" -w "$2" -U >/dev/null
}

delete_secret() {
  security delete-generic-password -s "$1" >/dev/null 2>&1 || true
}

ENV_FILE=""
COPY_FROM=""

parse_flags() {
  ENV_FILE=""
  COPY_FROM=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --env-file)
        ENV_FILE="${2:-}"
        shift 2
        ;;
      --copy-from)
        COPY_FROM="${2:-}"
        shift 2
        ;;
      *)
        echo "Unknown option: $1" >&2
        return 1
        ;;
    esac
  done
  if [ -n "$ENV_FILE" ]; then
    return 0
  fi
  if [ -n "${GMN_ENV_FILE:-}" ]; then
    ENV_FILE="$GMN_ENV_FILE"
  fi
}

read_env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

write_env_file() {
  local cookie="$1" xsrf="$2" auth_user="$3" api_key="$4"
  mkdir -p "$(dirname "$ENV_FILE")"
  local tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  chmod 600 "$tmp"
  {
    printf 'GMN_COOKIE=%s\n' "$cookie"
    printf 'GMN_XSRF_TOKEN=%s\n' "$xsrf"
    printf 'GMN_AUTH_USER=%s\n' "$auth_user"
    printf 'GMN_API_KEY=%s\n' "$api_key"
  } >"$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

load_runtime_secrets() {
  if [ -n "$ENV_FILE" ]; then
    GMN_COOKIE="$(read_env_value GMN_COOKIE)"
    GMN_API_KEY="$(read_env_value GMN_API_KEY)"
    GMN_XSRF_TOKEN="$(read_env_value GMN_XSRF_TOKEN)"
    GMN_AUTH_USER="$(read_env_value GMN_AUTH_USER)"
    return 0
  fi
  if ! have_security; then
    echo "macOS Keychain (security) is required, or pass --env-file." >&2
    return 1
  fi
  GMN_COOKIE="$(read_secret "$(item cookie)")"
  GMN_API_KEY="$(read_secret "$(item api-key)")"
  GMN_XSRF_TOKEN="$(read_secret "$(item xsrf)")"
  GMN_AUTH_USER="$(read_secret "$(item auth-user)")"
}

cmd_setup() {
  parse_flags "$@"
  local cookie="" xsrf="" auth_user="" api_key="" existing_key=""
  if [ -n "$COPY_FROM" ]; then
    if ! have_security; then
      echo "--copy-from reads the macOS Keychain." >&2
      return 1
    fi
    cookie="$(read_secret "${COPY_FROM}-cookie")"
    xsrf="$(read_secret "${COPY_FROM}-xsrf")"
    auth_user="$(read_secret "${COPY_FROM}-auth-user")"
    api_key="$(read_secret "${COPY_FROM}-api-key")"
    if [ -z "$cookie" ] || [ -z "$api_key" ]; then
      echo "Keychain prefix '${COPY_FROM}' is missing a cookie or API key." >&2
      return 1
    fi
    auth_user="${auth_user:-0}"
  else
    printf "Gemini cookie (one line, input hidden): "
    read -rs cookie
    printf '\n'
    cookie="${cookie#"${cookie%%[![:space:]]*}"}"
    cookie="${cookie%"${cookie##*[![:space:]]}"}"
    if [ -z "$cookie" ]; then
      echo "Cookie is required" >&2
      return 1
    fi
    case "$cookie" in
      *SID=*|*sid=*) ;;
      *) echo "Cookie line is missing SID." >&2; return 1 ;;
    esac
    case "$cookie" in
      *SAPISID=*|*sapisid=*) ;;
      *) echo "Cookie line is missing SAPISID." >&2; return 1 ;;
    esac
    printf "XSRF token (optional, input hidden; empty clears it): "
    read -rs xsrf
    printf '\n'
    xsrf="${xsrf#"${xsrf%%[![:space:]]*}"}"
    xsrf="${xsrf%"${xsrf##*[![:space:]]}"}"
    printf "Gemini auth user index [0]: "
    read -r auth_user
    auth_user="${auth_user:-0}"
    case "$auth_user" in
      ''|*[!0-9]*) echo "auth user must be a number such as 0 or 1" >&2; return 1 ;;
    esac
    if [ -n "$ENV_FILE" ]; then
      existing_key="$(read_env_value GMN_API_KEY)"
    elif have_security; then
      existing_key="$(read_secret "$(item api-key)")"
    fi
    if [ -n "$existing_key" ]; then
      printf "Local API key (input hidden; empty keeps the existing key): "
    else
      printf "Local API key (input hidden; empty generates one): "
    fi
    read -rs api_key
    printf '\n'
    api_key="${api_key#"${api_key%%[![:space:]]*}"}"
    api_key="${api_key%"${api_key##*[![:space:]]}"}"
    if [ -z "$api_key" ]; then
      if [ -n "$existing_key" ]; then
        api_key="$existing_key"
        echo "Kept the existing local API key."
      else
        command -v openssl >/dev/null || { echo "openssl is required to generate a local API key" >&2; return 1; }
        api_key="sk-$(openssl rand -hex 24)"
        echo "Generated a new local API key. Read it with: ${_root}/scripts/gmn-secrets.sh print"
      fi
    fi
  fi

  if [ -n "$ENV_FILE" ]; then
    write_env_file "$cookie" "$xsrf" "$auth_user" "$api_key"
    echo "Stored secrets in ${ENV_FILE} (mode 600)."
  else
    have_security || { echo "macOS Keychain (security) is required, or pass --env-file." >&2; return 1; }
    store_secret "$(item cookie)" "$cookie"
    if [ -n "$xsrf" ]; then
      store_secret "$(item xsrf)" "$xsrf"
    else
      delete_secret "$(item xsrf)"
    fi
    store_secret "$(item auth-user)" "$auth_user"
    store_secret "$(item api-key)" "$api_key"
    echo "Stored Keychain items for prefix '${PREFIX}'."
  fi
  echo "Next: ${_root}/scripts/gmn-secrets.sh up"
}

cmd_status() {
  parse_flags "$@"
  if [ -n "$ENV_FILE" ]; then
    if [ -f "$ENV_FILE" ]; then
      echo "ok: env file present"
      return 0
    fi
    echo "missing: env file ${ENV_FILE}"
    return 1
  fi
  have_security || { echo "macOS Keychain (security) is required, or pass --env-file." >&2; return 1; }
  local missing=0 name
  for name in cookie api-key auth-user; do
    if item_present "$(item "$name")"; then
      echo "ok: Keychain item '$(item "$name")' present"
    else
      echo "missing: Keychain item '$(item "$name")'"
      missing=1
    fi
  done
  if item_present "$(item xsrf)"; then
    echo "ok: Keychain item '$(item xsrf)' present"
  else
    echo "note: Keychain item '$(item xsrf)' absent (optional)"
  fi
  return "$missing"
}

require_podman() {
  command -v podman >/dev/null || { echo "podman is required" >&2; return 1; }
  podman info >/dev/null 2>&1 || {
    echo "Podman is not running." >&2
    if [ "$(uname -s)" = "Darwin" ]; then
      echo "On macOS, start the machine: podman machine start" >&2
    fi
    return 1
  }
}

port_free() {
  python3 - "$1" <<'PY'
import socket, sys
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

cmd_up() {
  parse_flags "$@"
  require_podman
  load_runtime_secrets
  if [ -z "${GMN_COOKIE:-}" ] || [ -z "${GMN_API_KEY:-}" ]; then
    echo "Secrets are missing. Run setup first." >&2
    return 1
  fi
  GMN_AUTH_USER="${GMN_AUTH_USER:-0}"
  export GMN_COOKIE GMN_API_KEY GMN_XSRF_TOKEN GMN_AUTH_USER
  export GMN_TEMPORARY_CHATS="${GMN_TEMPORARY_CHATS:-true}"
  export GMN_PORT="$PORT"
  podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if ! port_free "$PORT"; then
    echo "127.0.0.1:${PORT} is already in use. Refusing to start." >&2
    return 1
  fi

  podman build -t "$IMAGE" -f "${_root}/Dockerfile" "${_root}"

  local -a run_args
  run_args=(
    run -d
    --name "$CONTAINER"
    --restart unless-stopped
    -e GMN_COOKIE
    -e GMN_API_KEY
    -e GMN_XSRF_TOKEN
    -e GMN_AUTH_USER
    -e GMN_TEMPORARY_CHATS
    -e GMN_PORT
  )
  if [ -n "${GMN_REQUEST_TIMEOUT_SEC:-}" ]; then
    run_args+=(-e GMN_REQUEST_TIMEOUT_SEC)
  fi
  if [ -n "${GMN_PROMPT_CHAR_LIMIT:-}" ]; then
    run_args+=(-e GMN_PROMPT_CHAR_LIMIT)
  fi
  if [ -n "${GMN_BL:-}" ]; then
    run_args+=(-e GMN_BL)
  fi
  if [ -n "${GMN_MODEL_ALIASES:-}" ]; then
    run_args+=(-v "${GMN_MODEL_ALIASES}:/aliases.json:ro" -e GMN_MODEL_ALIASES=/aliases.json)
  fi
  if [ "${GMN_PODMAN_NETWORK:-}" = "host" ]; then
    run_args+=(--network host -e GMN_HOST=127.0.0.1)
  else
    run_args+=(-p "127.0.0.1:${PORT}:${PORT}")
  fi
  podman "${run_args[@]}" "$IMAGE"
  echo "Listening on http://127.0.0.1:${PORT}/v1"
  echo "podman inspect ${CONTAINER} shows these environment values. Do not share that output."
}

cmd_down() {
  require_podman
  podman rm -f "$CONTAINER"
}

cmd_check() {
  parse_flags "$@"
  command -v curl >/dev/null || { echo "unreachable"; return 1; }
  load_runtime_secrets || { echo "unreachable"; return 1; }
  if [ -z "${GMN_API_KEY:-}" ]; then
    echo "unreachable"
    return 1
  fi
  local response http_code reply lowered
  if ! response="$(curl -sS -m 40 -w $'\n%{http_code}' \
    "http://127.0.0.1:${PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${GMN_API_KEY}" \
    -d '{"model":"gemini-3.5-flash-lite","stream":false,"messages":[{"role":"user","content":"Reply with ok"}]}')"; then
    echo "unreachable"
    return 1
  fi
  http_code="${response##*$'\n'}"
  reply="${response%$'\n'*}"
  lowered="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    *xsrf*) echo "xsrf"; return 1 ;;
  esac
  case "$http_code" in
    200) echo "ok" ;;
    401) echo "auth"; return 1 ;;
    *) echo "unreachable"; return 1 ;;
  esac
}

cmd_print() {
  parse_flags "$@"
  load_runtime_secrets || return 1
  if [ -z "${GMN_API_KEY:-}" ]; then
    echo "WARN: local API key is missing. Run setup." >&2
    return 1
  fi
  local escaped="${GMN_API_KEY//\'/\'\\\'\'}"
  printf "export GMN_API_KEY='%s'\n" "$escaped"
}

command_name="${1:-}"
shift || true
case "$command_name" in
  setup) cmd_setup "$@" ;;
  status) cmd_status "$@" ;;
  up) cmd_up "$@" ;;
  down) cmd_down ;;
  print) cmd_print "$@" ;;
  check) cmd_check "$@" ;;
  *)
    echo "Usage: ${_root}/scripts/gmn-secrets.sh setup|status|up|down|print|check" >&2
    exit 1
    ;;
esac
