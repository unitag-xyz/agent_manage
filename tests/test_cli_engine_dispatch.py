import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_manage.cli import main
from agent_manage.core.errors import APIError


class RecordingExecution:
    restart_required = False

    def __init__(self):
        self.calls = []
        self.arguments = []

    def operation(self, name):
        if name not in {"get_current_model", "create_instance", "add_agents"}:
            raise APIError("UNSUPPORTED", "Unsupported fixture operation")

        def run(*args):
            self.calls.append(name)
            self.arguments.extend(args)
            return {"engine": "fixture", "operation": name}
        return run

    def register_secret(self, value):
        pass

    def default_workspace(self, *, local=False):
        return "/fixture/workspace"


def run_cli(argv, stdin=""):
    out = io.StringIO()
    with redirect_stdout(out), patch("sys.stdin", io.StringIO(stdin)):
        code = main(argv)
    return code, json.loads(out.getvalue())


class CliEngineRoutingTests(unittest.TestCase):
    def test_omitted_and_explicit_openclaw_preserve_cli_response(self):
        from agent_manage.adapters.registry import ENGINES, EngineDriver
        execution = RecordingExecution()
        with patch.dict(ENGINES, openclaw=EngineDriver(lambda _: execution, None)):
            with patch.dict(os.environ, clear=True):
                implicit = run_cli(["current-model"])
                os.environ["UNITAG_AGENT_MANAGER_ENGINE"] = "openclaw"
                explicit = run_cli(["current-model"])
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit[0], 0)
        self.assertEqual(execution.calls, ["get_current_model", "get_current_model"])

    def test_bootstrap_and_reads_use_only_selected_engine(self):
        from agent_manage.adapters.registry import ENGINES, EngineDriver
        execution = RecordingExecution()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_ENGINE": "fixture"}):
            path = Path(tmp) / "never-created.json"
            driver = EngineDriver(open_local=lambda options: execution, create_runtime=None)
            with patch.dict(ENGINES, fixture=driver):
                code, result = run_cli(["--config-path", str(path), "create-instance", "--model-key-stdin"], "fixture-key\n")
                self.assertEqual((code, result["result"]["engine"]), (0, "fixture"))
                code, result = run_cli(["current-model"])
                self.assertEqual((code, result["result"]["engine"]), (0, "fixture"))
                code, result = run_cli(["current-gateway-token"])
                self.assertEqual((code, result["error"]["code"]), (1, "UNSUPPORTED"))
            self.assertFalse(path.exists())
            self.assertEqual(execution.calls, ["create_instance", "get_current_model"])

    def test_every_parser_command_has_an_openclaw_operation(self):
        import argparse
        from agent_manage.cli import CLI_OPERATIONS, build_parser
        from agent_manage.adapters.openclaw.execution import OpenClawExecution
        commands = next(a.choices for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(set(commands), set(CLI_OPERATIONS))
        self.assertEqual(set(CLI_OPERATIONS.values()), OpenClawExecution.operations)

    def test_native_prerequisites_and_workspace_belong_to_selected_driver(self):
        from agent_manage.adapters.registry import ENGINES, EngineDriver
        execution = RecordingExecution()
        with patch.dict(ENGINES, fixture=EngineDriver(lambda _: execution, None)), patch.dict(os.environ, clear=True):
            code, result = run_cli(["create-instance"])
            self.assertEqual(code, 1)
            self.assertIn("--model-key", result["message"])
            os.environ["UNITAG_AGENT_MANAGER_ENGINE"] = "fixture"
            self.assertEqual(run_cli(["create-instance"])[0], 0)
            self.assertEqual(run_cli(["add-agents", "--agents", '["first"]'])[0], 0)
        self.assertEqual(execution.arguments[0].model_key, "")
        self.assertEqual(execution.arguments[1].workspace_root, "/fixture/workspace")

    def test_unknown_engine_denied_but_version_does_not_need_one(self):
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_ENGINE": "missing"}):
            code, result = run_cli(["current-model"])
            self.assertEqual(code, 1)
            self.assertIn("engine", result["message"].lower())
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
                main(["--version"])
            self.assertEqual(exited.exception.code, 0)
