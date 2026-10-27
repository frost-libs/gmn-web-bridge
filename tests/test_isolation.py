# SPDX-License-Identifier: MIT
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gmn-secrets.sh"


class IsolationTest(unittest.TestCase):
    def test_the_new_instance_does_not_name_the_old_container(self):
        text = SCRIPT.read_text()

        self.assertIn('CONTAINER="gmn-web-bridge"', text)
        self.assertIn('IMAGE="gmn-web-bridge:dev"', text)
        self.assertIn('PORT="${GMN_PORT:-8091}"', text)
        self.assertIn('podman rm -f "$CONTAINER"', text)
        self.assertNotIn("profile" + "-gemini", text)
        self.assertNotIn("8081", text)
