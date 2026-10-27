# SPDX-License-Identifier: MIT
import json
import unittest
from pathlib import Path

from gmn_web_bridge.protocol import build_freq, build_inner, model_header, native_thinking, sapisid_hash
from gmn_web_bridge.stream import ProtocolError, accept_snapshot, texts_from_line

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stream_generate_redacted.txt"


class PayloadTest(unittest.TestCase):
    def test_mode_think_and_temporary_slots(self):
        inner = build_inner("Hello", mode=8, think=0, temporary=True, file_refs=[("/contrib_service/ttl_1d/x", "image.png")], request_id="req-1")

        self.assertEqual(len(inner), 80)
        self.assertEqual(inner[79], 8)
        self.assertEqual(inner[17], [[0]])
        self.assertIs(inner[45], True)
        self.assertEqual(inner[59], "req-1")
        self.assertEqual(inner[0][0], "Hello")
        self.assertEqual(inner[0][3], [[["/contrib_service/ttl_1d/x", 1], "image.png"]])
        outer = json.loads(build_freq(inner))
        self.assertIsNone(outer[0])
        self.assertEqual(json.loads(outer[1])[79], 8)

    def test_temporary_off_leaves_the_slot_empty(self):
        inner = build_inner("Hello", mode=1, think=4, temporary=False, file_refs=None, request_id="req-2")

        self.assertIsNone(inner[45])
        self.assertIsNone(inner[0][3])

    def test_model_header_carries_hash_mode_and_temporary(self):
        header = json.loads(model_header("56fdd199312815e2", 1, 0, True, "req-1"))

        self.assertEqual(header[4], "56fdd199312815e2")
        self.assertIs(header[7], True)
        self.assertEqual(header[14], 1)
        self.assertEqual(header[15], native_thinking(0))
        self.assertEqual(header[16], "req-1")
        self.assertEqual(native_thinking(4), 1)
        self.assertEqual(native_thinking(2), 2)

    def test_sapisid_hash_uses_timestamp_secret_and_origin(self):
        self.assertTrue(sapisid_hash("secret", 1700000000).startswith("SAPISIDHASH 1700000000_"))


class FixtureTest(unittest.TestCase):
    def test_redacted_stream_is_cumulative(self):
        raw = ""
        sent = False
        deltas = []
        for line in FIXTURE.read_text().splitlines():
            texts = texts_from_line(line)
            if not texts:
                continue
            raw, suffix = accept_snapshot(raw, sent, texts)
            if suffix:
                sent = True
                deltas.append(suffix)

        self.assertEqual(deltas, ["Hello", " there"])

    def test_a_size_event_is_not_an_error_and_a_short_code_is(self):
        self.assertEqual(texts_from_line(json.dumps([["e", 10, None, None, 22098]])), [])
        with self.assertRaises(ProtocolError):
            texts_from_line(json.dumps([["e", 4, None, None, 131]]))
        self.assertNotIn("SAPISID", FIXTURE.read_text())
        self.assertNotIn("SNlM0e", FIXTURE.read_text())
