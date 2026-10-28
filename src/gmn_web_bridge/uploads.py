# SPDX-License-Identifier: MIT
"""Upload an image to the Gemini push endpoint and return its file id."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .protocol import ORIGIN, UPLOAD_URL

MAX_IMAGE_BYTES = 20 * 1024 * 1024


class UploadError(Exception):
    """The push endpoint did not accept the image."""


class ImageFetchError(Exception):
    """An image URL could not be downloaded."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def sniff_mime(data: bytes) -> str | None:
    """Return an image type recognized from the first bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch_image(
    http: httpx.Client,
    url: str,
    mime_hint: str,
    *,
    limit: int = MAX_IMAGE_BYTES,
) -> tuple[bytes, str]:
    """Download an http(s) image. The MIME type comes from the bytes when they match."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ImageFetchError("Only http and https image URLs are accepted.")
    with http.stream("GET", url) as response:
        if response.status_code >= 400:
            raise ImageFetchError(f"Image download failed: HTTP {response.status_code}.", 502)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise ImageFetchError("Image is larger than 20 MB.")
            chunks.append(chunk)
        header = response.headers.get("content-type", "").split(";", 1)[0].strip()
    data = b"".join(chunks)
    mime = sniff_mime(data) or mime_hint or header or "image/png"
    return data, mime


def upload_image(
    http: httpx.Client,
    data: bytes,
    mime: str,
    *,
    push_id: str | None,
    client_pctx: str | None,
    cookie: str,
    authorization: str | None,
) -> str:
    headers = {
        "Push-ID": push_id or "",
        "X-Tenant-Id": "bard-storage",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "X-Goog-Upload-Header-Content-Type": mime,
        "Content-Length": "0",
        "Origin": ORIGIN,
    }
    if cookie:
        headers["Cookie"] = cookie
    if client_pctx:
        headers["X-Client-Pctx"] = client_pctx
    if authorization:
        headers["Authorization"] = authorization
    started = http.post(UPLOAD_URL, headers=headers, content=b"")
    if started.status_code >= 400:
        raise UploadError(f"upload start failed: {started.status_code}")
    upload_url = started.headers.get("x-goog-upload-url")
    if not upload_url:
        raise UploadError("upload start returned no URL")
    finished = http.post(
        upload_url,
        content=data,
        headers={
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Type": mime,
            **({"Cookie": cookie} if cookie else {}),
        },
    )
    if finished.status_code >= 400:
        raise UploadError(f"upload finalize failed: {finished.status_code}")
    file_id = finished.text.strip().split()[-1] if finished.text.strip() else ""
    if not file_id:
        raise UploadError("upload finalize returned no file id")
    return file_id
