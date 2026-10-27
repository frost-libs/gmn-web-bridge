# SPDX-License-Identifier: MIT
import unittest
from pathlib import Path

from gmn_web_bridge.compaction import (
    _END_OMITTED_NOTE,
    _OMITTED_NOTE,
    _SUMMARY_INSTRUCTION,
    _SUMMARY_PREFIX,
    clear_summary_cache,
    compact_chat_body,
    estimate_prompt_chars,
    fetch_summary,
)
from gmn_web_bridge.openai_api import (
    UnknownPublicModel,
    UnnamedToolResult,
    load_aliases,
    rewrite_chat_body,
    rewrite_tool_sse,
    translate_upstream_error,
)

ALIASES = load_aliases(str(Path(__file__).resolve().parent / "fixtures" / "aliases.json"))


class RewriteChatBodyTest(unittest.TestCase):
    def test_messages_body_is_unchanged(self):
        body = {
            "model": "gemini-3.6-flash",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream_options": {"include_usage": True},
        }

        self.assertEqual(rewrite_chat_body(body, ALIASES), body)

    def test_tool_results_take_the_name_from_the_call(self):
        tool_result = {"role": "tool", "tool_call_id": "call_1", "content": "clear"}
        body = {
            "model": "alias-pro",
            "messages": [
                {"role": "user", "content": "Weather?"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                    ],
                },
                tool_result,
            ],
        }

        rewritten = rewrite_chat_body(body, ALIASES)

        self.assertEqual(rewritten["messages"][2]["name"], "get_weather")
        self.assertNotIn("name", tool_result)
        self.assertEqual(rewritten["model"], "gemini-3.1-pro")

    def test_unnamed_tool_result_without_a_call_is_rejected(self):
        body = {
            "model": "gemini-3.6-flash",
            "messages": [{"role": "tool", "tool_call_id": "call_missing", "content": "nope"}],
        }

        with self.assertRaises(UnnamedToolResult):
            rewrite_chat_body(body, ALIASES)

    def test_aliases_map_and_keep_think(self):
        rewritten = rewrite_chat_body(
            {"model": "alias-pro@think=0", "messages": [{"role": "user", "content": "Hello"}]},
            ALIASES,
        )

        self.assertEqual(rewritten["model"], "gemini-3.1-pro@think=0")

    def test_unknown_alias_prefix_is_rejected(self):
        with self.assertRaises(UnknownPublicModel):
            rewrite_chat_body({"model": "alias-unknown", "messages": []}, ALIASES)

    def test_without_an_alias_file_a_prefixed_name_is_left_alone(self):
        body = {"model": "alias-pro", "messages": []}

        self.assertEqual(rewrite_chat_body(body), body)

    def test_usage_chunk_passes_through(self):
        raw = b'data: {"choices":[],"usage":{"total_tokens":1}}\n\ndata: [DONE]\n\n'

        self.assertEqual(rewrite_tool_sse(raw), raw)

    def test_content_and_tool_calls_become_two_chunks(self):
        raw = (
            b'data: {"choices":[{"index":0,"delta":{"content":"Hi","tool_calls":[{"id":"call_1","function":{"name":"ls","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n'
        )

        pieces = rewrite_tool_sse(raw).split(b"\n\n")
        first = pieces[0]
        second = pieces[1]

        self.assertIn(b"Hi", first)
        self.assertNotIn(b"tool_calls", first)
        self.assertIn(b"tool_calls", second)
        self.assertNotIn(b"Hi", second)

    def test_xsrf_and_context_and_html_errors_are_rewritten(self):
        status, body = translate_upstream_error(502, b'{"error":{"message":"xsrf token rejected"}}')
        self.assertEqual(status, 401)
        self.assertIn(b"XSRF", body)

        status, body = translate_upstream_error(400, b'{"error":{"message":"prompt exceeds the context window"}}')
        self.assertEqual(status, 400)
        self.assertIn(b"context window", body)

        status, body = translate_upstream_error(200, b"<!DOCTYPE html><html>cookie=secret</html>")
        self.assertEqual(status, 502)
        self.assertNotIn(b"cookie=secret", body)


class CompactHistoryTest(unittest.TestCase):
    def test_older_turn_is_summarized_and_the_latest_stays(self):
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "O" * 400},
            {"role": "assistant", "content": "looked"},
            {"role": "user", "content": "latest question"},
        ]
        kept = [messages[0], messages[-1]]
        limit = max(
            estimate_prompt_chars(kept, None) + len(_SUMMARY_PREFIX) + 80,
            len(_SUMMARY_INSTRUCTION) + 80,
        )
        seen = {}

        def summarize(text):
            seen["text"] = text
            return "summary mentions src/old.py"

        compacted = compact_chat_body({"model": "gemini-3.1-pro", "messages": messages}, limit, summarize)

        self.assertEqual(compacted["messages"][0]["content"], "rules")
        self.assertTrue(compacted["messages"][1]["content"].startswith(_SUMMARY_PREFIX))
        self.assertIn("src/old.py", compacted["messages"][1]["content"])
        self.assertEqual(compacted["messages"][-1]["content"], "latest question")
        self.assertNotIn("latest question", seen["text"])
        self.assertLessEqual(estimate_prompt_chars(compacted["messages"], None), limit)

    def test_dropped_text_is_clipped_from_the_oldest_end(self):
        messages = [
            {"role": "user", "content": "HEAD" + ("x" * 5000)},
            {"role": "user", "content": "TAIL"},
        ]
        limit = max(
            estimate_prompt_chars([messages[1]], None) + len(_SUMMARY_PREFIX) + 40,
            len(_SUMMARY_INSTRUCTION) + len(_OMITTED_NOTE) + 20,
        )
        seen = {}

        def summarize(text):
            seen["text"] = text
            return "short"

        compact_chat_body({"model": "m", "messages": messages}, limit, summarize)

        self.assertTrue(seen["text"].startswith(_OMITTED_NOTE))
        self.assertNotIn("HEAD", seen["text"])

    def test_latest_turn_over_the_guard_keeps_its_end(self):
        compacted = compact_chat_body(
            {"model": "m", "messages": [{"role": "user", "content": "HEAD" + ("x" * 5000) + "QUESTION"}]},
            200,
            lambda text: text,
        )

        content = compacted["messages"][-1]["content"]
        self.assertTrue(content.startswith(_OMITTED_NOTE))
        self.assertTrue(content.endswith("QUESTION"))
        self.assertNotIn("HEAD", content)

    def test_system_text_keeps_its_start_when_the_turn_is_trimmed(self):
        compacted = compact_chat_body(
            {
                "model": "m",
                "messages": [
                    {"role": "system", "content": "RULES" + ("s" * 5000)},
                    {"role": "user", "content": "QUESTION"},
                ],
            },
            120,
            lambda text: text,
        )

        system = compacted["messages"][0]["content"]
        self.assertTrue(system.startswith("RULES"))
        self.assertTrue(system.endswith(_END_OMITTED_NOTE))
        self.assertEqual(compacted["messages"][1]["content"], "QUESTION")

    def test_a_short_question_keeps_its_text_when_the_tool_list_is_large(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "d" * 5000,
                    "parameters": {"type": "object", "properties": {"path": {"description": "p" * 5000}}},
                },
            }
        ]
        messages = [{"role": "user", "content": "QUESTION"}]
        limit = len("QUESTION") + 200
        compacted = compact_chat_body({"model": "m", "messages": messages, "tools": tools}, limit, lambda text: text)

        self.assertEqual(compacted["messages"][0]["content"], "QUESTION")
        self.assertLessEqual(estimate_prompt_chars(compacted["messages"], compacted["tools"]), limit)
        self.assertEqual(compacted["tools"][0]["function"]["name"], "read_file")
        self.assertNotIn("description", compacted["tools"][0]["function"])

    def test_summary_cache_skips_the_second_call(self):
        clear_summary_cache()
        calls = []

        def complete(text):
            calls.append(text)
            return "remembered"

        limit = len(_SUMMARY_INSTRUCTION) + 100
        self.assertEqual(fetch_summary("same", complete, limit), "remembered")
        self.assertEqual(fetch_summary("same", complete, limit), "remembered")
        self.assertEqual(len(calls), 1)
        self.assertEqual(fetch_summary("other", complete, limit), "remembered")
        self.assertEqual(len(calls), 2)
