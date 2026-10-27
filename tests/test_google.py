# SPDX-License-Identifier: MIT
import json
import unittest

from gmn_web_bridge.google_api import google_response, google_to_openai, model_from_path


class GoogleApiTest(unittest.TestCase):
    def test_generate_content_becomes_a_chat_body(self):
        body = google_to_openai(
            "gemini-3.8-flash",
            {
                "systemInstruction": {"parts": [{"text": "rules"}]},
                "contents": [
                    {"role": "user", "parts": [{"text": "Hello"}]},
                    {
                        "role": "model",
                        "parts": [{"functionCall": {"name": "ls", "args": {"path": "."}}}],
                    },
                    {
                        "role": "user",
                        "parts": [{"functionResponse": {"name": "ls", "response": {"ok": True}}}],
                    },
                ],
                "tools": [{"functionDeclarations": [{"name": "ls", "description": "list"}]}],
            },
            stream=False,
        )

        self.assertEqual(body["model"], "gemini-3.8-flash")
        self.assertEqual(body["messages"][0]["content"], "rules")
        self.assertEqual(body["messages"][1]["content"][0]["text"], "Hello")
        self.assertEqual(body["messages"][2]["tool_calls"][0]["function"]["name"], "ls")
        self.assertEqual(body["messages"][3]["role"], "tool")
        self.assertEqual(body["messages"][3]["name"], "ls")
        self.assertEqual(body["tools"][0]["function"]["name"], "ls")
        self.assertIs(body["stream"], False)

    def test_path_splits_model_and_action(self):
        self.assertEqual(
            model_from_path("/v1beta/models/gemini-3.8-flash:streamGenerateContent"),
            ("gemini-3.8-flash", "streamGenerateContent"),
        )

    def test_a_function_call_is_a_google_part(self):
        payload = google_response(
            "Hi",
            [{"function": {"name": "ls", "arguments": json.dumps({"path": "."})}}],
        )

        parts = payload["candidates"][0]["content"]["parts"]
        self.assertEqual(parts[0]["text"], "Hi")
        self.assertEqual(parts[1]["functionCall"]["name"], "ls")
        self.assertEqual(parts[1]["functionCall"]["args"], {"path": "."})
