# Gemini web protocol

This note is the contract `gmn-web-bridge` implements. It is not a Google API.
The web app changes without notice. Values below were taken from the public
`https://gemini.google.com/app` page on 2026-10-27 and from the public reverse
contract published by Gemini Nexus on 2026-10-14. No session cookie was sent
while writing this note. Signed-out HTML does not contain `SNlM0e`.

## Page tokens

| HTML key | Use |
| --- | --- |
| `SNlM0e` or `thykhd` | POST field `at`. Present only on a signed-in page. |
| `cfb2h` | Query parameter `bl`. The public page on 2026-10-27 exposed `boq_gemini-web-uiserver_20261026.16_p0`. |
| `FdrFJe` | Query parameter `f.sid`. |
| `qKIAYe` | Upload header `Push-ID`. |
| `Ylro7b` | Upload header `X-Client-Pctx`. |

The signed-out page also exposes `https://push.clients6.google.com/upload/` and the RPC prefix `/_/BardChatUi`.

`GMN_BL` overrides `cfb2h`. When it is unset, the process reads `cfb2h` from the Gemini page on the first request.

## Anonymous session

`GMN_MODE=anonymous` with an empty cookie fetches `https://gemini.google.com/app` and sends no `Cookie` header. That page has `cfb2h` and `FdrFJe`. It does not have `SNlM0e`, so the POST omits `at`. The StreamGenerate URL has no `/u/<index>` prefix, and the request has no `Authorization` and no `X-Goog-AuthUser`. `GMN_AUTH_USER` and `GMN_XSRF_TOKEN` are ignored until a cookie is set.

A cookie in `anonymous` mode uses the signed-in request above.

## StreamGenerate

```text
POST https://gemini.google.com/u/<auth-user>/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate
    ?bl=<cfb2h>&hl=en&_reqid=<n>&rt=c&f.sid=<FdrFJe>
```

Headers:

- `Content-Type: application/x-www-form-urlencoded;charset=utf-8`
- `Origin: https://gemini.google.com`
- `Referer: https://gemini.google.com/u/<auth-user>/app`
- `X-Same-Domain: 1`
- `X-Goog-AuthUser: <auth-user>` when the index is not `0`
- `Cookie` from the signed-in browser
- `Authorization: SAPISIDHASH <unix>_<sha1>` when `SAPISID` is in the cookie. The hash input is `"<unix> <SAPISID> https://gemini.google.com"`.
- `Connection: close`
- `x-goog-ext-525001261-jspb` and `x-goog-ext-525005358-jspb` only when `GMN_SEND_MODEL_HEADER=true`. On 2026-10-27 the live session answered those headers with protocol code 131, so the default is off. Model selection then uses inner index 79.

Body:

```text
f.req=<json>&at=<SNlM0e>
```

`f.req` is a two-element JSON array. Element 0 is null. Element 1 is a JSON string of an 80-element inner array.

| Inner index | Value |
| --- | --- |
| 0 | `[prompt, 0, null, file refs or null, null, null, 0]`. Each file ref is `[["<upload id>", 1], "<filename>"]`. |
| 1 | `["en"]` |
| 2 | `["", "", "", null, null, null, null, null, null, ""]` for a new conversation. History is folded into the prompt. |
| 17 | `[[think]]`. `0` is the deepest pass and `4` is the shallowest. |
| 45 | `true` when temporary chats are on |
| 59 | request UUID |
| 79 | mode category integer |

Mode categories used by this bridge:

| Integer | Menu |
| --- | --- |
| 1 | Fast (Flash) |
| 2 | Thinking |
| 3 | Pro |
| 4 | Auto |
| 5 | Dynamic thinking |
| 6 | Flash-Lite |
| 8 | 4 Argon |

The side-channel header `x-goog-ext-525001261-jspb` is a JSON array:

| Index | Value |
| --- | --- |
| 4 | model hash, or null when this bridge has no hash for that mode |
| 7 | `true` for a temporary chat |
| 8 | `[4, 5, 6, 8, 4, 5, 6, 8]` |
| 11 | mode category |
| 14 | mode category |
| 15 | native thinking level: `3` when our level is 0 or 1, `2` when it is 2 or 3, `1` when it is 4 |
| 16 | request UUID |

Known hashes, from the 2026-10-14 public contract and earlier public captures:

| Hash | Model |
| --- | --- |
| `56fdd199312815e2` | 3.8 Flash |
| `e6fa609c3fa255c0` | 3.1 Pro |
| `cf41b0e0dd7d53e5` | 3.5 Flash-Lite |
| `95a4c0abcba24364` | 4 Argon |

## Response

The body starts with the XSSI guard `)]}'`. Later lines are either a decimal byte length or a JSON array. Length lines are ignored. A JSON line is a list of events. An event `["wrb.fr", ..., "<inner json>"]` carries the candidate. The answer text is a cumulative snapshot at inner `[4][0][1][0]`. A later snapshot that extends the previous one contributes only the new suffix. A snapshot that replaces text already sent ends the stream. A snapshot that arrives before anything was sent replaces the draft.

`BardErrorInfo [<n>]` in the body is a terminal upstream error. An event shaped `["e", ..., <code>]` is the same.

A retry is allowed only before the first text byte is yielded. After that, a dropped connection ends the stream.

HTTP 405 from StreamGenerate, before any text, means the build label is stale. Unless `GMN_BL` is set, the bridge drops the cached `cfb2h`, reads the page again (signed-in or anonymous), and retries that request once.

## Uploads

```text
POST https://push.clients6.google.com/upload/
Push-ID: <qKIAYe>
X-Tenant-Id: bard-storage
X-Client-Pctx: <Ylro7b>
X-Goog-Upload-Protocol: resumable
X-Goog-Upload-Command: start
```

The response header `X-Goog-Upload-URL` receives the bytes:

```text
POST <X-Goog-Upload-URL>
X-Goog-Upload-Command: upload, finalize
X-Goog-Upload-Offset: 0
```

The response body is the file id placed in the prompt's file-ref slot. Only images are uploaded.

## Fixture

`tests/fixtures/stream_generate_redacted.txt` is a synthetic body in this shape. It contains no cookie, no `at` token, and no account id.
