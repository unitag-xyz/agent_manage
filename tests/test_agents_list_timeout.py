import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from agent_manage.cli import main
from agent_manage.adapters.openclaw.local import LocalRunner
from agent_manage.adapters.openclaw.orchestrator import InstanceManagerV2


VARIABLE = "UNITAG_AGENT_MANAGER_AGENTS_LIST_TIMEOUT_SECONDS"
COMMAND = ["openclaw", "agents", "list", "--bindings", "--json"]


class AgentsListTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runner = Mock(spec=LocalRunner)
        self.runner.openclaw_bin = "openclaw"
        self.runner.dry_run = False
        self.agent = {"id": "team", "workspace": "/workspace/team", "agentDir": "/agent/team",
                      "identityName": "Example", "bindings": 1,
                      "bindingDetails": ["channel=example"], "routes": ["example default"],
                      "providers": ["example default: configured"]}
        self.runner.run_json.return_value = {"agents": [{"id": "main"}, self.agent]}

    def manager(self):
        return InstanceManagerV2(self.runner, config_path=str(Path(self.directory.name) / "openclaw.json"))

    def list_with(self, runtime, value):
        environment = {"UNITAG_AGENT_MANAGER_RUNTIME": runtime}
        if value is not None:
            environment[VARIABLE] = value
        with patch.dict(os.environ, environment, clear=True):
            return self.manager().list_agents()

    def test_container_default_is_ten_and_output_fields_are_unchanged(self):
        result = self.list_with("container", None)
        self.runner.run_json.assert_called_once_with(COMMAND, timeout=10)
        self.assertEqual(result, {"ok": True, "check": "openclaw agents list --bindings --json",
                                 "agent_count": 1, "agents": [self.agent]})

    def test_container_accepts_supported_budgets_without_changing_command_or_result(self):
        for value in ("10", "20", "30", "60"):
            with self.subTest(value=value):
                self.runner.run_json.reset_mock()
                result = self.list_with("container", value)
                self.runner.run_json.assert_called_once_with(COMMAND, timeout=int(value))
                self.assertEqual(result["agents"], [self.agent])

    def test_invalid_container_budget_fails_before_starting_subprocess(self):
        for value in ("", "9", "61", "0", "-20", "+20", "20.0", " 20", "20 ", "20\n",
                      "020", "NaN", "1e1", "\u0662\u0660", "\uff12\uff10", "1" * 1000):
            with self.subTest(value=value):
                self.runner.run_json.reset_mock()
                with self.assertRaisesRegex(ValueError, VARIABLE + " must be an ASCII integer between 10 and 60"):
                    self.list_with("container", value)
                self.runner.run_json.assert_not_called()

    def test_non_container_runtime_ignores_variable(self):
        for runtime in ("", "vps"):
            for value in ("20", "invalid-secret-value", "61"):
                with self.subTest(runtime=runtime, value=value):
                    self.runner.run_json.reset_mock()
                    self.list_with(runtime, value)
                    self.runner.run_json.assert_called_once_with(COMMAND, timeout=10)

    def test_budget_does_not_change_other_status_queries(self):
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container", VARIABLE: "20"}, clear=True):
            manager = self.manager()
            manager.list_agents()
            self.runner.run_json.assert_called_once_with(COMMAND, timeout=20)
            self.runner.run_json.reset_mock()
            for method in ("get_tg_bot_status", "get_feishu_bot_status", "get_weixin_bot_status", "get_current_model"):
                setattr(manager, method, Mock(return_value={}))
            self.runner.run_json.return_value = {"ok": True}
            result = manager.check_server_status()
            self.runner.run_json.assert_called_once_with(
                ["openclaw", "gateway", "status", "--require-rpc", "--json"], timeout=10)
            self.assertEqual(result["timeout_seconds"], 10)

    def test_invalid_override_is_not_consumed_by_other_commands(self):
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container", VARIABLE: "bad"}, clear=True):
            manager = self.manager()
            for method in ("get_tg_bot_status", "get_feishu_bot_status", "get_weixin_bot_status", "get_current_model"):
                setattr(manager, method, Mock(return_value={}))
            self.runner.run_json.return_value = {"ok": True}
            manager.check_server_status()
            self.runner.run_json.assert_called_once_with(
                ["openclaw", "gateway", "status", "--require-rpc", "--json"], timeout=10)

    def test_cli_invalid_override_returns_validation_error_without_echoing_value(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container", VARIABLE: "private-invalid-value"}):
            with patch.object(LocalRunner, "run_json") as run:
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    code = main(["agents-list"])
                run.assert_not_called()
        value = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(value["error"]["code"], "VALIDATION_ERROR")
        self.assertIn(VARIABLE, value["message"])
        self.assertNotIn("private-invalid-value", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
