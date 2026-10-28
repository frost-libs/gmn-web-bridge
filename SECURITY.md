# Security

In `local` mode this process holds a signed-in Gemini browser session. Treat the cookie, the XSRF token, and the local bearer key as secrets. `GMN_MODE=anonymous` holds no account session when no cookie is set. The local bearer key still guards the port.

- `scripts/gmn-secrets.sh` stores them in the macOS login Keychain, or in a mode 600 env file when you pass `--env-file`.
- The container receives them as environment variables for that process. `podman inspect gmn-web-bridge` shows those values. Do not share that output.
- The server does not log request bodies, cookies, or the bearer key. `GMN_LOG_REQUESTS` writes the method and path only, with the query string removed so `?key=` is not recorded.
- With `GMN_FETCH_IMAGE_URLS` left on, a caller who has the local key can make this process download an `http` or `https` URL. Downloads stop at 20 MB. Other URL schemes are rejected.
- Do not commit a cookie, an XSRF token, a bearer key, or a filled-in env file.
- The published port binds to `127.0.0.1` unless you choose host networking.

Report a vulnerability through the GitHub security tab of this repository. Do not include a live cookie in the report.
