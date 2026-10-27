---
name: Gemini web bridge
alwaysApply: true
---

This chat uses a local Gemini web bridge at `http://127.0.0.1:8091/v1` through chat completions. Each call is a new Gemini conversation, so earlier turns have to be sent again. Calls are temporary and are not saved on the Google account unless the process was started with `GMN_TEMPORARY_CHATS=false`.

Model ids are the Gemini web names, such as `gemini-3.8-flash`, `gemini-3.1-pro`, `gemini-4-argon`, `gemini-3.5-flash-thinking`, `gemini-3.5-flash-thinking-lite`, and `gemini-3.5-flash-lite`. A `@think=0` suffix is the deepest pass and `@think=4` is the shallowest. If the operator loaded an alias file, those public ids are accepted too.

The input guard defaults to 32,000 characters. Older turns are replaced by one summary. If the latest turn is still too long, the start of it is dropped and the end is kept. An image from an older turn becomes the word `[image]` inside that summary.

Function tools work. Text arrives as it is written, and a tool call arrives at the end of the reply. A tool result that has no recoverable function name is rejected.

Do not put a cookie, an API key, or an account write in a prompt.
