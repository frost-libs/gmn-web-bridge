# gmn-web-bridge

An OpenAI-compatible bridge to a [Gemini](https://gemini.google.com) web session. One Python process listens on `127.0.0.1:8091` and calls Gemini directly. The default mode uses your signed-in browser session. `GMN_MODE=anonymous` calls Gemini without a cookie. The local bearer key is required either way.

This project is not affiliated with Google. It uses your own browser session. You are responsible for complying with Google's terms.

The license is MIT. Copyright (c) 2026 Frost Libs

## What is stored

| Value | Where it comes from | Keychain item |
| --- | --- | --- |
| Gemini session cookie | Chrome, while signed in to `gemini.google.com` | `gmn-web-bridge-cookie` |
| Optional XSRF token (`SNlM0e` in the page source) | The same signed-in page | `gmn-web-bridge-xsrf` |
| Account index (`/u/0/`, `/u/1/`, …) | The Gemini URL | `gmn-web-bridge-auth-user` |
| Local bearer key | Generated or chosen at setup | `gmn-web-bridge-api-key` |

On macOS the four items live in the login Keychain. The item prefix is `GMN_KEYCHAIN_PREFIX` (`gmn-web-bridge` by default). On Linux, pass `--env-file` and the same values are written to a mode 600 file. The container receives them only as environment variables.

Do not commit the cookie, the XSRF token, or the local bearer key. Do not paste them into chat.

`scripts/gmn-secrets.sh setup --copy-from <prefix>` reads `<prefix>-cookie`, `<prefix>-xsrf`, `<prefix>-auth-user`, and `<prefix>-api-key` and copies them under the new names. It does not change or delete the source items.

## Run

Podman is required.

```bash
scripts/gmn-secrets.sh setup
scripts/gmn-secrets.sh up
```

`up` builds the image `gmn-web-bridge:dev`, starts the container `gmn-web-bridge`, and publishes `127.0.0.1:8091`. The default port is `GMN_PORT` (`8091`). If that port is already in use, `up` refuses to start. `down` stops only `gmn-web-bridge`.

```bash
eval "$(scripts/gmn-secrets.sh print)"
curl -sS http://127.0.0.1:8091/v1/models \
  -H "Authorization: Bearer $GMN_API_KEY"
curl -sS http://127.0.0.1:8091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GMN_API_KEY" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Hello"}]}'
```

Do not `echo` `$GMN_API_KEY`.

| Field | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:8091/v1` |
| API key | Keychain item `gmn-web-bridge-api-key`, or `GMN_API_KEY` from the env file |
| Model | `gemini-3.8-flash`, `gemini-3.1-pro`, `gemini-4-argon`, `gemini-3.5-flash-thinking`, `gemini-3.5-flash-thinking-lite`, `gemini-3.5-flash-lite` |

An unknown model name falls back to `GMN_DEFAULT_MODEL` (`gemini-3.8-flash`). Append `@think=0` for the deepest pass and `@think=4` for the shallowest.

Optional public ids are loaded only when `GMN_MODEL_ALIASES` points at a JSON file. [`examples/aliases.example.json`](examples/aliases.example.json) is one such file. Without that file, only the Gemini web model names above are special.

A Google-style request is also accepted:

```text
POST /v1beta/models/gemini-3.8-flash:generateContent
POST /v1beta/models/gemini-3.8-flash:streamGenerateContent
```

## Cookies

1. Open Chrome and sign in at [https://gemini.google.com](https://gemini.google.com).
2. DevTools → Application → Cookies → `https://gemini.google.com`.
3. Copy `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, and `__Secure-1PSID` onto one line, separated by `; `.
4. If the address bar contains `/u/1/`, the auth user index is `1`. The default is `0`.
5. Optional, needed when authenticated calls return an `xsrf` error: in the page source, find `SNlM0e` and copy that value.

`scripts/gmn-secrets.sh check` sends one tiny completion and prints `ok`, `xsrf`, `auth`, or `unreachable`. It does not print the bearer key, the cookie, or the reply.

## Behaviour

Each request is its own Gemini conversation. The client sends the history again. Text is forwarded as Gemini writes it. A tool call is sent at the end of that reply. Function tools work. A tool result with no recoverable function name returns HTTP 400.

`GMN_TEMPORARY_CHATS` defaults to on, so calls are not saved on the Google account. `false`, `0`, and `no` turn persistence on.

The bridge guards each prompt at 32,000 characters. Set `GMN_PROMPT_CHAR_LIMIT` to change it, or `0` to turn the guard off. When a thread is over the guard, older turns are summarized with `GMN_SUMMARY_MODEL` (`gemini-3.5-flash-lite`) and replaced by one system message.

`GMN_REQUEST_TIMEOUT_SEC` defaults to 180.

Images in a chat message are uploaded to Gemini's push endpoint and attached to that turn. A part may be a data URL, an `http` or `https` URL, or raw base64 in `data` or `base64`. A downloaded image is capped at 20 MB, and its type is taken from the file bytes when they match a known image. `GMN_FETCH_IMAGE_URLS=false` rejects URL images. An anonymous session that cannot upload an image returns HTTP 502 and asks for `GMN_COOKIE`.

The local key is accepted from `Authorization: Bearer`, `x-api-key`, `x-goog-api-key`, or `?key=`. `GET /health` does not need the key. It returns `{"status": "ok"}`.

Chat completions include `usage`, estimated as characters divided by 4. `stream_options.include_usage` adds that object on a final stream chunk. A reply that is only tool calls has `content` set to `null`. Google responses include `usageMetadata`, `modelVersion`, and `candidates[].index`.

`POST /v1/responses` accepts the OpenAI Responses shape used by Codex: `instructions`, a string or item list in `input`, and flat function tools. Text is streamed as Gemini writes it. A tool call is sent at the end of that reply.

The wire format is described in [`docs/protocol.md`](docs/protocol.md). The build label is read from the Gemini page (`cfb2h`) unless `GMN_BL` is set. If StreamGenerate answers HTTP 405 before any text, the bridge reads the page again and retries once. A pinned `GMN_BL` is not replaced.

## Anonymous mode

`GMN_MODE=anonymous` starts without a cookie. If a cookie is set, the signed-in session is used as in `local` mode. Without a cookie the bridge sends no cookie, no XSRF token, and no account path. `gemini-3.1-pro` and `gemini-4-argon` may be answered as Flash. Google may rate-limit the address. `scripts/gmn-secrets.sh setup` accepts an empty cookie in this mode. The local API key is still created and required.

Any other `GMN_MODE` value stops the process at startup. An unset value means `local`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `GMN_MODE` | `local` | `local` requires a cookie. `anonymous` does not. |
| `GMN_PORT` | `8091` | Published on `127.0.0.1`. |
| `GMN_HOST` | `0.0.0.0` | Listen address inside the container. |
| `GMN_TEMPORARY_CHATS` | `true` | `false`, `0`, or `no` saves chats on the account. |
| `GMN_REQUEST_TIMEOUT_SEC` | `180` | Upstream timeout. |
| `GMN_PROMPT_CHAR_LIMIT` | `32000` | `0` turns the guard off. |
| `GMN_DEFAULT_MODEL` | `gemini-3.8-flash` | Used when the requested name is unknown. |
| `GMN_SUMMARY_MODEL` | `gemini-3.5-flash-lite` | Summarizes turns dropped by the guard. |
| `GMN_BL` | page `cfb2h` | Pins the build label and disables the 405 refresh. |
| `GMN_MODEL_ALIASES` | empty | Optional public-id JSON file. |
| `GMN_AUTH_USER` | `0` | Account index. Ignored without a cookie. |
| `GMN_UPSTREAM_PROXY` | empty | HTTP proxy for Gemini, uploads, and image downloads. `HTTPS_PROXY` is used when this is empty. |
| `GMN_FETCH_IMAGE_URLS` | `true` | Download `http` and `https` images. |
| `GMN_LOG_REQUESTS` | `false` | One stderr line per request: time, method, path, status, model, duration. No body, cookie, or key. |
| `GMN_CORS` | `false` | Answers `OPTIONS` and sends `Access-Control-Allow-Origin: *`. |
| `GMN_RETRY_ATTEMPTS` | `3` | Retries before the first text byte. |
| `GMN_RETRY_DELAY_SEC` | `2` | Delay between those retries. |
| `GMN_SEND_MODEL_HEADER` | `false` | Sends the model side-channel header. Leave it off. |

`scripts/gmn-secrets.sh up` passes `GMN_MODE` and, when set in the environment, `GMN_UPSTREAM_PROXY`, `GMN_LOG_REQUESTS`, `GMN_CORS`, `GMN_FETCH_IMAGE_URLS`, `GMN_REQUEST_TIMEOUT_SEC`, `GMN_PROMPT_CHAR_LIMIT`, and `GMN_BL`.

## Codex and Gemini CLI

Codex uses the same base URL and bearer key, with the Responses route above.

Gemini CLI sends the key in `x-goog-api-key`:

```bash
eval "$(scripts/gmn-secrets.sh print)"
export GEMINI_API_KEY="$GMN_API_KEY"
export GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8091"
```

Do not `echo` `$GMN_API_KEY` or `$GEMINI_API_KEY`.

## Upgrade and rollback

`scripts/gmn-secrets.sh up` builds the image before it removes the running container. The image that was `gmn-web-bridge:dev` is tagged `gmn-web-bridge:previous` first. A failed build leaves the running container in place.

```bash
scripts/gmn-secrets.sh rollback
```

`rollback` starts the container again from `gmn-web-bridge:previous` with the same secrets and the same `GMN_MODE`.

## Develop

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

[`examples/agent-rules.example.md`](examples/agent-rules.example.md) is a client rule file for a coding agent pointed at this bridge.
