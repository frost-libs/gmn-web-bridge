# SPDX-License-Identifier: MIT
"""Signed-in Gemini web client. One StreamGenerate call is one conversation."""

from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import urlencode

import httpx

from .auth import app_path, parse_page_tokens, sapisid
from .config import Settings
from .models import resolve_model
from .protocol import (
    MODEL_HEADER,
    ORIGIN,
    REQUEST_HEADER,
    STREAM_PATH,
    build_freq,
    build_inner,
    model_header,
    new_request_id,
    sapisid_hash,
)
from .stream import (
    ProtocolError,
    StreamRewritten,
    accept_snapshot,
    bard_error,
    clean_text,
    texts_from_line,
)
from .uploads import ImageFetchError, UploadError, fetch_image, upload_image


class GeminiError(Exception):
    """A session, protocol, or transport failure with an HTTP status for the caller."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class BuildLabelStale(Exception):
    """StreamGenerate rejected the build label. The page can be read again."""


class GeminiClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        if http is None:
            kwargs: dict[str, object] = {
                "timeout": settings.request_timeout_sec,
                "headers": {"Connection": "close"},
                "verify": True,
                "follow_redirects": True,
            }
            if settings.upstream_proxy:
                kwargs["proxy"] = settings.upstream_proxy
            http = httpx.Client(**kwargs)
        self._http = http
        self._at_token = (settings.xsrf_token or None) if settings.cookie else None
        self._build_label = settings.build_label or None
        self._session_id: str | None = None
        self._push_id: str | None = None
        self._client_pctx: str | None = None
        self._ready = False

    def close(self) -> None:
        self._http.close()

    def generate(
        self,
        prompt: str,
        model_name: str,
        files: list[tuple[bytes | str, str, str]] | None = None,
    ) -> Iterator[str]:
        """Yield reply text. Retries only happen before the first yield."""
        _name, spec, think = resolve_model(model_name, self.settings.default_model)
        self._ensure_session()
        file_refs = self._upload_all(files or [])
        last_error: Exception | None = None
        attempts = self.settings.retry_attempts
        attempt = 0
        refreshed = False
        while attempt < attempts:
            raw = ""
            sent = ""
            yielded = False
            try:
                for texts in self._snapshots(prompt, spec.mode, think, spec.token, file_refs):
                    try:
                        raw, _suffix = accept_snapshot(raw, yielded, texts)
                    except StreamRewritten:
                        return
                    cleaned = clean_text(raw)
                    if not cleaned.startswith(sent):
                        if yielded:
                            return
                        sent = ""
                    delta = cleaned[len(sent) :]
                    if not delta:
                        continue
                    sent = cleaned
                    yielded = True
                    yield delta
                return
            except BuildLabelStale:
                if yielded or refreshed or self.settings.build_label:
                    raise GeminiError("Gemini rejected the build label.", 502)
                self._build_label = None
                self._ready = False
                self._ensure_session()
                refreshed = True
                continue
            except GeminiError:
                raise
            except Exception as exc:
                if yielded:
                    return
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(self.settings.retry_delay_sec)
            attempt += 1
        raise GeminiError(str(last_error) if last_error else "Gemini request failed")

    def _signed_in(self) -> bool:
        return bool(self.settings.cookie)

    def _session_rejected(self) -> GeminiError:
        if self._signed_in():
            return GeminiError(
                "Gemini session rejected the request. Refresh the cookie and the XSRF token, then run setup again.",
                401,
            )
        return GeminiError(
            "Gemini rejected the anonymous request. Google may be rate-limiting this address.",
            401,
        )

    def _ensure_session(self) -> None:
        if self._ready and self._build_label and (self._at_token or not self._signed_in()):
            return
        page = app_path(self.settings.auth_user) if self._signed_in() else "/app"
        response = self._http.get(ORIGIN + page, headers=self._browser_headers())
        if response.status_code in (401, 403) or (
            self._signed_in() and "accounts.google.com" in str(response.url)
        ):
            raise self._session_rejected()
        tokens = parse_page_tokens(response.text)
        if self._signed_in():
            self._at_token = self._at_token or tokens.at_token
        self._build_label = self._build_label or tokens.build_label
        self._session_id = self._session_id or tokens.session_id
        self._push_id = self._push_id or tokens.push_id
        self._client_pctx = self._client_pctx or tokens.client_pctx
        if self._signed_in() and not self._at_token:
            raise self._session_rejected()
        if not self._build_label:
            raise GeminiError("The Gemini page did not include a build label.", 502)
        self._ready = True

    def _upload_all(self, files: list[tuple[bytes | str, str, str]]) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        for data, mime, name in files:
            if isinstance(data, str):
                if not self.settings.fetch_image_urls:
                    raise GeminiError("Image URLs are disabled.", 400)
                try:
                    data, mime = fetch_image(self._http, data, mime)
                except ImageFetchError as exc:
                    raise GeminiError(exc.message, exc.status) from exc
            try:
                file_id = upload_image(
                    self._http,
                    data,
                    mime,
                    push_id=self._push_id,
                    client_pctx=self._client_pctx,
                    cookie=self.settings.cookie,
                    authorization=self._authorization(),
                )
                refs.append((file_id, name))
            except UploadError as exc:
                if not self._signed_in():
                    raise GeminiError(
                        "Image upload needs a signed-in session. Set GMN_COOKIE.",
                        502,
                    ) from exc
                raise GeminiError(str(exc)) from exc
        return refs

    def _snapshots(self, prompt: str, mode: int, think: int, token: str | None, file_refs: list[tuple[str, str]]):
        request_id = new_request_id()
        inner = build_inner(
            prompt,
            mode,
            think,
            self.settings.temporary_chats,
            file_refs,
            request_id,
        )
        form = {"f.req": build_freq(inner)}
        if self._signed_in():
            form["at"] = self._at_token or ""
        body = urlencode(form).encode()
        params = {
            "bl": self._build_label,
            "hl": "en",
            "_reqid": str(int(time.time() * 1000) % 1_000_000),
            "rt": "c",
        }
        if self._session_id:
            params["f.sid"] = self._session_id
        headers = self._browser_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"
        if self.settings.send_model_header:
            headers[MODEL_HEADER] = model_header(
                token, mode, think, self.settings.temporary_chats, request_id
            )
            headers[REQUEST_HEADER] = request_id
        url = ORIGIN + self._account_prefix() + STREAM_PATH
        with self._http.stream("POST", url, params=params, content=body, headers=headers) as response:
            if response.status_code == 405:
                raise BuildLabelStale()
            if response.status_code in (401, 403):
                raise self._session_rejected()
            if response.status_code >= 400:
                raise GeminiError(f"Gemini returned HTTP {response.status_code}.")
            buf = ""
            for chunk in response.iter_text():
                buf += chunk
                code = bard_error(buf)
                if code:
                    raise GeminiError(f"Gemini upstream rejected request: BardErrorInfo [{code}]")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.strip().lower().startswith("<!doctype html") or line.strip().lower().startswith("<html"):
                        raise GeminiError("Gemini returned a web page instead of a completion.")
                    try:
                        texts = texts_from_line(line)
                    except ProtocolError as exc:
                        raise GeminiError(str(exc)) from exc
                    if texts:
                        yield texts
            if buf.strip():
                try:
                    texts = texts_from_line(buf)
                except ProtocolError as exc:
                    raise GeminiError(str(exc)) from exc
                if texts:
                    yield texts

    def _account_prefix(self) -> str:
        if not self._signed_in():
            return ""
        user = self.settings.auth_user.strip() or "0"
        if user == "0":
            return ""
        return f"/u/{user}"

    def _authorization(self) -> str | None:
        secret = sapisid(self.settings.cookie)
        if not secret:
            return None
        return sapisid_hash(secret, int(time.time()))

    def _browser_headers(self) -> dict[str, str]:
        referer = app_path(self.settings.auth_user) if self._signed_in() else "/app"
        headers = {
            "Origin": ORIGIN,
            "Referer": ORIGIN + referer,
            "X-Same-Domain": "1",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if not self._signed_in():
            return headers
        headers["Cookie"] = self.settings.cookie
        if self.settings.auth_user not in ("", "0"):
            headers["X-Goog-AuthUser"] = self.settings.auth_user
        authorization = self._authorization()
        if authorization:
            headers["Authorization"] = authorization
        return headers
