import io
import os
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from agent_manage.adapters.openclaw.local import CommandError, LocalRunner


class LocalRunnerEnvironmentTest(unittest.TestCase):
    def test_command_path_contains_npm_and_system_sbin_directories(self):
        runner = LocalRunner()

        with patch.dict(
            os.environ,
            {
                "HOME": "/tmp/agent-manage-home",
                "PATH": "/usr/bin:/bin",
            },
            clear=True,
        ):
            env = runner._command_env()

        path_parts = env["PATH"].split(os.pathsep)
        self.assertEqual(path_parts[0], "/tmp/agent-manage-home/.npm-global/bin")
        self.assertIn("/usr/local/sbin", path_parts)
        self.assertIn("/usr/sbin", path_parts)
        self.assertIn("/sbin", path_parts)
        self.assertEqual(len(path_parts), len(set(path_parts)))

    def test_failed_command_logs_only_utf8_safe_tails_and_redacts_secrets(self):
        secret = "sensitive-token"
        stdout = "前缀" * 10_000 + " stdout-tail " + secret
        stderr = "错误" * 10_000 + " stderr-tail " + secret
        runner = LocalRunner()
        runner.add_redaction_value(secret)
        captured_stderr = io.StringIO()
        completed = subprocess.CompletedProcess(
            ["openclaw", "broken"], 1, stdout=stdout, stderr=stderr
        )

        with patch("agent_manage.adapters.openclaw.local.os.getuid", return_value=1000, create=True), patch(
            "agent_manage.adapters.openclaw.local.subprocess.run", return_value=completed
        ), redirect_stderr(captured_stderr):
            with self.assertRaises(CommandError):
                runner.run(["openclaw", "broken"])

        logs = captured_stderr.getvalue()
        self.assertIn("stdout: utf8_bytes=", logs)
        self.assertIn("stderr: utf8_bytes=", logs)
        self.assertIn("truncated=true", logs)
        self.assertIn("stdout-tail [REDACTED]", logs)
        self.assertIn("stderr-tail [REDACTED]", logs)
        self.assertNotIn(secret, logs)
        self.assertLess(len(logs.encode("utf-8")), 40_000)


if __name__ == "__main__":
    unittest.main()
