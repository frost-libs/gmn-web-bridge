# SPDX-License-Identifier: MIT
"""Upload an image to the Gemini push endpoint and return its file id."""

from __future__ import annotations

import httpx

from .protocol import ORIGIN, UPLOAD_URL


class UploadError(Exception):
    """The push endpoint did not accept the image."""


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
        "Cookie": cookie,
    }
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
            "Cookie": cookie,
        },
    )
    if finished.status_code >= 400:
        raise UploadError(f"upload finalize failed: {finished.status_code}")
    file_id = finished.text.strip().split()[-1] if finished.text.strip() else ""
    if not file_id:
        raise UploadError("upload finalize returned no file id")
    return file_id
