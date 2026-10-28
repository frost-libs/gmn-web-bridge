# SPDX-License-Identifier: MIT
import os
import stat
import subprocess
import tempfile
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
        self.assertIn('PREVIOUS="gmn-web-bridge:previous"', text)
        self.assertIn("rollback", text)

    def test_a_failed_build_does_not_remove_the_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "podman.log"
            env_file = Path(tmp) / "env"
            env_file.write_text("GMN_COOKIE=SID=a; SAPISID=b\nGMN_API_KEY=sk-test\nGMN_XSRF_TOKEN=\nGMN_AUTH_USER=0\n")
            env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            self._stub_podman(Path(tmp) / "bin", log, fail_build=True)
            result = self._run(tmp, env_file, {})
            commands = log.read_text().splitlines()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("left in place", result.stderr)
        self.assertNotIn("rm", commands)
        self.assertIn("build", commands)

    def test_anonymous_mode_starts_without_a_cookie_and_local_mode_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "podman.log"
            env_file = Path(tmp) / "env"
            env_file.write_text("GMN_COOKIE=\nGMN_API_KEY=sk-test\nGMN_XSRF_TOKEN=\nGMN_AUTH_USER=0\n")
            env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            self._stub_podman(Path(tmp) / "bin", log, fail_build=False)
            local = self._run(tmp, env_file, {})
            local_commands = log.read_text().splitlines() if log.exists() else []
            log.write_text("")
            anonymous = self._run(tmp, env_file, {"GMN_MODE": "anonymous", "GMN_PORT": "18771"})
            anonymous_commands = log.read_text().splitlines()

        self.assertNotEqual(local.returncode, 0)
        self.assertIn("Secrets are missing", local.stderr)
        self.assertEqual(local_commands, [])
        self.assertEqual(anonymous.returncode, 0, anonymous.stderr)
        self.assertIn("run", anonymous_commands)

    def _run(self, tmp: str, env_file: Path, extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = str(Path(tmp) / "bin") + os.pathsep + env.get("PATH", "")
        env.update(extra)
        return subprocess.run(
            [str(SCRIPT), "up", "--env-file", str(env_file)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _stub_podman(self, bin_dir: Path, log: Path, fail_build: bool) -> None:
        bin_dir.mkdir()
        stub = bin_dir / "podman"
        fail = "exit 1" if fail_build else "exit 0"
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "$1" >> "{log}"\n'
            'if [ "$1" = "build" ]; then\n'
            f"  {fail}\n"
            "fi\n"
            "exit 0\n"
        )
        stub.chmod(0o755)
