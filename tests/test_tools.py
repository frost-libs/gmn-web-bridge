# SPDX-License-Identifier: MIT
import base64
import unittest

from gmn_web_bridge.tools import parse_tool_calls, render_conversation, withhold_tool_fence


class ToolStreamTest(unittest.TestCase):
    def test_prose_is_released_and_a_fence_prefix_is_held(self):
        emit, held = withhold_tool_fence("Looking ```")

        self.assertEqual(emit, "Looking ")
        self.assertEqual(held, "```")

    def test_a_tool_call_and_the_text_before_it_are_split(self):
        emit, held = withhold_tool_fence('Answer\n```tool_call\n{"name":"ls"}\n```')

        self.assertEqual(emit, "Answer\n")
        self.assertTrue(held.startswith("```tool_call"))

    def test_plain_text_is_released(self):
        self.assertEqual(withhold_tool_fence("Just text"), ("Just text", ""))

    def test_a_fence_becomes_an_openai_tool_call(self):
        prose, calls = parse_tool_calls('Answer\n```tool_call\n{"name":"ls","arguments":{"path":"a"}}\n```')

        self.assertEqual(prose, "Answer")
        self.assertEqual(calls[0]["function"]["name"], "ls")
        self.assertIn("path", calls[0]["function"]["arguments"])

    def test_images_are_lifted_out_of_the_prompt(self):
        png = base64.b64encode(b"png").decode()
        prompt, images = render_conversation(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "see"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
                    ],
                }
            ],
            None,
        )

        self.assertIn("[image]", prompt)
        self.assertNotIn(png, prompt)
        self.assertEqual(images[0][0], b"png")
        self.assertEqual(images[0][1], "image/png")

    def test_an_https_url_and_a_base64_part_stay_out_of_the_prompt(self):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\npng").decode()
        prompt, images = render_conversation(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "mime_type": "image/png"}},
                        {"type": "input_image", "image_url": "https://example.com/b.jpg"},
                        {"type": "input_image", "data": png, "mime_type": "image/png"},
                    ],
                }
            ],
            None,
        )

        self.assertNotIn("example.com", prompt)
        self.assertEqual(images[0][0], "https://example.com/a.png")
        self.assertEqual(images[1][0], "https://example.com/b.jpg")
        self.assertTrue(images[2][0].startswith(b"\x89PNG"))
