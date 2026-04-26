import tempfile
import unittest
import zipfile
import json
from pathlib import Path
from unittest.mock import patch

from agent_manage.local import CommandError, CommandResult
from agent_manage.models import (
    AddAgentsRequest,
    AddAgentRequest,
    AddTelegramBotRequest,
    AddWeixinBotRequest,
    CreateInstanceRequest,
    DeleteTelegramBotRequest,
    DeleteWeixinBotRequest,
    SetModelRequest,
)
from agent_manage.orchestrator import InstanceManagerV2


class FakeRunner:
    def __init__(self, responses=None, dry_run=False):
        self.responses = responses or {}
        self.calls = []
        self.logs = []
        self.dry_run = dry_run
        self.openclaw_bin = "openclaw"

    def run_json(self, args, timeout=None):
        key = tuple(args)
        self.calls.append(list(args))
        return self.responses.get(key, {"ok": True})

    def log(self, message):
        self.logs.append(message)
        return None

    def run(self, args, timeout=None, stream_output=False):
        argv = list(args)
        self.calls.append(argv)
        key = tuple(argv)
        response = self.responses.get(key)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, CommandResult):
            if response.returncode != 0:
                raise CommandError(f"Command failed with exit code {response.returncode}", response)
            return response
        if argv[:2] == ["pgrep", "-f"]:
            result = CommandResult(
                argv=argv,
                command_text=" ".join(argv),
                returncode=1,
                stdout="",
                stderr="",
                skipped=self.dry_run,
            )
            raise CommandError("Command failed with exit code 1", result)
        stdout = ""
        if argv == ["ss", "-ltn"]:
            stdout = "LISTEN 0 511 0.0.0.0:18889 0.0.0.0:*\n"
        return CommandResult(
            argv=argv,
            command_text=" ".join(argv),
            returncode=0,
            stdout=stdout,
            stderr="",
            skipped=self.dry_run,
        )


class WorkspaceCreatingRunner(FakeRunner):
    def run_json(self, args, timeout=None):
        if list(args[:3]) == ["openclaw", "agents", "add"]:
            workspace = Path(args[args.index("--workspace") + 1])
            workspace.mkdir(parents=True, exist_ok=True)
        return super().run_json(args, timeout=timeout)


class FailingPrepareManager(InstanceManagerV2):
    def _prepare_template_dir(self, archive_path: Path, template_dir: Path):
        template_dir.mkdir(parents=True, exist_ok=True)
        (template_dir / "partial.txt").write_text("partial\n", encoding="utf-8")
        raise RuntimeError("prepare failed")


class FailingPopulateManager(InstanceManagerV2):
    def _populate_workspace(self, template_dir: Path, workspace: Path):
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "partial.txt").write_text("partial\n", encoding="utf-8")
        raise RuntimeError("populate failed")


class CreateInstanceV2Test(unittest.TestCase):
    gateway_service_restart_calls = [
        ["systemctl", "--user", "stop", "openclaw-gateway.service"],
        ["pgrep", "-f", "openclaw-gateway"],
        ["systemctl", "--user", "start", "openclaw-gateway.service"],
        ["ss", "-ltn"],
    ]

    def setUp(self):
        self.sample_supported_models = [
            {
                "id": "deepseek-v4-flash",
                "model_ref": "unipay-fun/deepseek-v4-flash",
                "definition": {
                    "id": "deepseek-v4-flash",
                    "name": "DeepSeek V4 Flash",
                    "contextWindow": 1000000,
                    "maxTokens": 128000,
                    "input": ["text"],
                    "cost": {"input": 0.14, "output": 0.28, "cacheRead": 0.028, "cacheWrite": 0},
                    "reasoning": False,
                },
            },
            {
                "id": "deepseek-v4-pro",
                "model_ref": "unipay-fun/deepseek-v4-pro",
                "definition": {
                    "id": "deepseek-v4-pro",
                    "name": "DeepSeek V4 Pro",
                    "contextWindow": 1000000,
                    "maxTokens": 128000,
                    "input": ["text"],
                    "cost": {"input": 1.74, "output": 3.48, "cacheRead": 0.145, "cacheWrite": 0},
                    "reasoning": False,
                },
            },
            {
                "id": "gpt-5.4",
                "model_ref": "unipay-fun/gpt-5.4",
                "definition": {
                    "id": "gpt-5.4",
                    "name": "GPT-5.4",
                    "contextWindow": 1050000,
                    "maxTokens": 128000,
                    "input": ["text"],
                    "cost": {"input": 2.5, "output": 15.0, "cacheRead": 0.25, "cacheWrite": 0},
                    "reasoning": True,
                },
            },
            {
                "id": "gpt-5.3-codex",
                "model_ref": "unipay-fun/gpt-5.3-codex",
                "definition": {
                    "id": "gpt-5.3-codex",
                    "name": "GPT-5.3 Codex",
                    "contextWindow": 400000,
                    "maxTokens": 128000,
                    "input": ["text"],
                    "cost": {"input": 1.75, "output": 14.0, "cacheRead": 0.175, "cacheWrite": 0},
                    "reasoning": True,
                },
            },
            {
                "id": "gpt-5.4-nano",
                "model_ref": "unipay-fun/gpt-5.4-nano",
                "definition": {
                    "id": "gpt-5.4-nano",
                    "name": "GPT-5.4 nano",
                    "contextWindow": 400000,
                    "maxTokens": 128000,
                    "input": ["text"],
                    "cost": {"input": 0.2, "output": 1.25, "cacheRead": 0.02, "cacheWrite": 0},
                    "reasoning": True,
                },
            },
            {
                "id": "claude-sonnet-4-6",
                "model_ref": "unipay-fun/claude-sonnet-4-6",
                "definition": {
                    "id": "claude-sonnet-4-6",
                    "name": "Claude Sonnet 4.6",
                    "contextWindow": 1000000,
                    "maxTokens": 128000,
                    "input": ["text"],
                    "cost": {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 0},
                    "reasoning": False,
                },
            },
        ]
        self.fetch_models_patcher = patch.object(
            InstanceManagerV2,
            "_fetch_supported_gateway_models",
            return_value={
                "source_url": InstanceManagerV2.MODEL_CATALOG_URL,
                "model_count": len(self.sample_supported_models),
                "models": self.sample_supported_models,
                "primary_model": "unipay-fun/deepseek-v4-flash",
            },
        )
        self.fetch_models_patcher.start()
        self.addCleanup(self.fetch_models_patcher.stop)

    def _write_host_config(self, config_path: Path, payload=None):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(payload or {}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_normalize_catalog_model_maps_api_fields(self):
        manager = InstanceManagerV2(FakeRunner())

        result = manager._normalize_catalog_model(
            {
                "identifier": "deepseek-v4-flash",
                "displayName": "DeepSeek V4 Flash",
                "contextWindowTokens": 1000000,
                "inputTokenPrice": 0.14,
                "outputTokenPrice": 0.28,
                "cachedInputTokenPrice": 0.028,
                "reasoningTokenPrice": None,
                "currency": "USD",
                "tokenPricingUnit": "PerMillionTokens",
            }
        )

        self.assertEqual(result["model_ref"], "unipay-fun/deepseek-v4-flash")
        self.assertEqual(
            result["definition"],
            {
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "contextWindow": 1000000,
                "maxTokens": InstanceManagerV2.DEFAULT_MODEL_MAX_TOKENS,
                "input": ["text"],
                "cost": {"input": 0.14, "output": 0.28, "cacheRead": 0.028, "cacheWrite": 0},
                "reasoning": False,
            },
        )
        self.assertEqual(result["upstream"]["currency"], "USD")
        self.assertEqual(result["upstream"]["token_pricing_unit"], "PerMillionTokens")

    def test_create_instance_populates_workspace_and_overlays_template(self):
        runner = FakeRunner(
            responses={
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                    "--model",
                    "openai/gpt-5",
                ): {"ok": True},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            workspace_root = tmp_path / "data"
            template_dir = tmp_path / "template" / "base"
            archive_path = tmp_path / "template" / "base.zip"
            workspace = workspace_root / "base"
            config_path = tmp_path / ".openclaw" / "openclaw.json"
            archive_path.parent.mkdir(parents=True, exist_ok=True)

            self._write_archive(
                archive_path,
                {
                    "base/app/main.py": "print('hello')\n",
                    "base/SOUL.md": "old soul\n",
                    "base/skills/weather/SKILL.md": "old weather\n",
                },
            )
            self._write_host_config(
                config_path,
                {
                    "agents": {
                        "list": [],
                        "defaults": {
                            "workspace": "/home/ubuntu/.openclaw/workspace",
                            "models": {"vllm/gpt-4.1-mini": {}},
                            "model": {"primary": "vllm/gpt-4.1-mini"},
                        }
                    },
                    "tools": {
                        "exec": {
                            "timeout": 30,
                        },
                        "web": {
                            "search": {
                                "region": "us",
                            }
                        },
                    },
                    "models": {
                        "providers": {
                            "legacy": {
                                "apiKey": "old-key",
                            }
                        }
                    },
                },
            )

            runner.responses[
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    str(workspace),
                    "--non-interactive",
                    "--json",
                    "--model",
                    "openai/gpt-5",
                )
            ] = runner.responses.pop(
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                    "--model",
                    "openai/gpt-5",
                )
            )

            manager = InstanceManagerV2(
                runner,
                template_root=str(tmp_path / "template"),
                config_path=str(config_path),
            )
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="base",
                    model_key="test-key",
                    model="openai/gpt-5",
                    workspace_root=str(workspace_root),
                )
            )

            self.assertTrue(result["ok"])
            self.assertIsInstance(result["gateway_token"], str)
            self.assertGreaterEqual(len(result["gateway_token"]), 40)
            self.assertEqual(runner.calls[0][:4], ["openclaw", "agents", "add", "base"])
            self.assertEqual((workspace / "app" / "main.py").read_text(encoding="utf-8"), "print('hello')\n")
            self.assertEqual((workspace / "SOUL.md").read_text(encoding="utf-8"), "old soul\n")
            self.assertEqual((template_dir / "SOUL.md").read_text(encoding="utf-8"), "old soul\n")
            self.assertEqual(
                (workspace / "skills" / "weather" / "SKILL.md").read_text(encoding="utf-8"),
                "old weather\n",
            )
            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved_config["agents"]["defaults"]["model"]["primary"],
                "unipay-fun/deepseek-v4-flash",
            )
            self.assertEqual(
                saved_config["agents"]["defaults"]["workspace"],
                "/home/ubuntu/.openclaw/workspace",
            )
            self.assertEqual(
                list(saved_config["agents"]["defaults"]["models"].keys()),
                [
                    "unipay-fun/deepseek-v4-flash",
                    "unipay-fun/deepseek-v4-pro",
                    "unipay-fun/gpt-5.4",
                    "unipay-fun/gpt-5.3-codex",
                    "unipay-fun/gpt-5.4-nano",
                    "unipay-fun/claude-sonnet-4-6",
                ],
            )
            self.assertNotIn("vllm/gpt-4.1-mini", saved_config["agents"]["defaults"]["models"])
            self.assertEqual(
                saved_config["models"]["providers"]["unipay-fun"]["apiKey"],
                "test-key",
            )
            self.assertEqual(
                saved_config["models"]["providers"]["unipay-fun"]["models"],
                [item["definition"] for item in self.sample_supported_models],
            )
            self.assertEqual(saved_config["gateway"]["auth"]["mode"], "token")
            self.assertEqual(saved_config["gateway"]["auth"]["token"], result["gateway_token"])
            self.assertEqual(saved_config["tools"]["profile"], "coding")
            self.assertEqual(saved_config["tools"]["exec"]["security"], "full")
            self.assertEqual(saved_config["tools"]["exec"]["timeout"], 30)
            self.assertEqual(saved_config["tools"]["web"]["search"], {"enabled": False})
            self.assertEqual(saved_config["tools"]["web"]["fetch"], {"enabled": True})
            self.assertNotIn("legacy", saved_config["models"]["providers"])
            self.assertEqual(result["template_dir"], str(template_dir.resolve()))
            self.assertIn("elapsed_ms", result["steps"][0])
            self.assertEqual(
                result["steps"][1]["result"]["command"],
                f"openclaw agents add base --workspace {workspace.resolve()} --non-interactive --json --model openai/gpt-5",
            )
            self.assertEqual(result["steps"][3]["step"], "models.fetch_catalog")
            self.assertEqual(result["steps"][4]["step"], "config.configure_models")
            self.assertEqual(result["steps"][-1]["step"], "config.configure_tools")
            self.assertEqual(result["steps"][-2]["step"], "config.configure_gateway_auth")

    def test_create_instance_unzips_to_same_named_template_dir_then_copies_to_workspace(self):
        runner = WorkspaceCreatingRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []},
                (
                    "openclaw",
                    "agents",
                    "add",
                    "unipay-claw-base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                ): {"ok": True},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            workspace_root = tmp_path / "data"
            workspace = workspace_root / "unipay-claw-base"
            archive_path = tmp_path / "template" / "unipay-claw-base.zip"
            config_path = tmp_path / ".openclaw" / "openclaw.json"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(
                archive_path,
                {
                    "app/main.py": "print('hello')\n",
                    "SOUL.md": "zip soul\n",
                    "skills/weather/SKILL.md": "zip weather\n",
                },
            )
            self._write_host_config(config_path)

            runner.responses[
                (
                    "openclaw",
                    "agents",
                    "add",
                    "unipay-claw-base",
                    "--workspace",
                    str(workspace),
                    "--non-interactive",
                    "--json",
                )
            ] = runner.responses.pop(
                (
                    "openclaw",
                    "agents",
                    "add",
                    "unipay-claw-base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                )
            )

            manager = InstanceManagerV2(
                runner,
                template_root=str(tmp_path / "template"),
                config_path=str(config_path),
            )
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="unipay-claw-base",
                    model_key="test-key",
                    workspace_root=str(workspace_root),
                )
            )

            self.assertTrue(result["ok"])
            self.assertIsInstance(result["gateway_token"], str)
            self.assertEqual(result["agent_name"], "unipay-claw-base")
            self.assertEqual(result["template_dir"], str((tmp_path / "template" / "unipay-claw-base").resolve()))
            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_config["gateway"]["auth"]["token"], result["gateway_token"])
            self.assertEqual(
                ((tmp_path / "template" / "unipay-claw-base" / "SOUL.md").read_text(encoding="utf-8")),
                "zip soul\n",
            )
            self.assertEqual((workspace / "SOUL.md").read_text(encoding="utf-8"), "zip soul\n")
            self.assertEqual(
                (workspace / "skills" / "weather" / "SKILL.md").read_text(encoding="utf-8"),
                "zip weather\n",
            )
            self.assertIn("template.prepare", result["steps"][0]["step"])
            self.assertEqual(result["steps"][-1]["step"], "config.configure_tools")

    def test_create_instance_skips_add_when_agent_exists(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            archive_path = tmp_path / "template" / "base.zip"
            template_dir = tmp_path / "template" / "base"
            config_path = tmp_path / ".openclaw" / "openclaw.json"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})
            self._write_host_config(config_path, {"agents": {"list": [{"id": "base"}]}})

            manager = InstanceManagerV2(
                runner,
                template_root=str(tmp_path / "template"),
                config_path=str(config_path),
            )
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="base",
                    model_key="test-key",
                    workspace_root=str(tmp_path / "data"),
                )
            )

            self.assertTrue(result["ok"])
            self.assertIsInstance(result["gateway_token"], str)
            self.assertEqual(result["steps"][1]["step"], "agents.add")
            self.assertEqual(
                result["steps"][1]["result"],
                {
                    "skipped": True,
                    "reason": "agent_exists",
                    "agent_name": "base",
                },
            )
            self.assertEqual(runner.calls, [])
            self.assertIn("agent exists, skip add: base", runner.logs)

    def test_create_instance_reuses_precreated_empty_workspace_directory(self):
        runner = WorkspaceCreatingRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []},
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                ): {"ok": True},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            workspace_root = tmp_path / "data"
            workspace = workspace_root / "base"
            config_path = tmp_path / ".openclaw" / "openclaw.json"
            workspace.mkdir(parents=True)
            archive_path = tmp_path / "template" / "base.zip"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})
            self._write_host_config(config_path)

            runner.responses[
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    str(workspace),
                    "--non-interactive",
                    "--json",
                )
            ] = runner.responses.pop(
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                )
            )

            manager = InstanceManagerV2(
                runner,
                template_root=str(tmp_path / "template"),
                config_path=str(config_path),
            )
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="base",
                    model_key="test-key",
                    workspace_root=str(workspace_root),
                )
            )

            self.assertTrue(result["ok"])
            self.assertIsInstance(result["gateway_token"], str)
            self.assertEqual((workspace / "main.py").read_text(encoding="utf-8"), "print('x')\n")
            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_config["gateway"]["auth"]["token"], result["gateway_token"])

    def test_create_instance_skips_populate_when_workspace_is_non_empty(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []},
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                ): {"ok": True},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            workspace_root = tmp_path / "data"
            workspace = workspace_root / "base"
            config_path = tmp_path / ".openclaw" / "openclaw.json"
            workspace.mkdir(parents=True)
            (workspace / "old.txt").write_text("keep\n", encoding="utf-8")
            archive_path = tmp_path / "template" / "base.zip"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})
            self._write_host_config(config_path)

            runner.responses[
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    str(workspace),
                    "--non-interactive",
                    "--json",
                )
            ] = runner.responses.pop(
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                )
            )

            manager = InstanceManagerV2(
                runner,
                template_root=str(tmp_path / "template"),
                config_path=str(config_path),
            )
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="base",
                    model_key="test-key",
                    workspace_root=str(workspace_root),
                )
            )

            self.assertTrue(result["ok"])
            self.assertIsInstance(result["gateway_token"], str)
            self.assertEqual(result["steps"][2]["step"], "workspace.populate")
            self.assertEqual(
                result["steps"][2]["result"],
                {
                    "skipped": True,
                    "reason": "workspace_not_empty",
                    "workspace": str(workspace.resolve()),
                },
            )
            self.assertEqual((workspace / "old.txt").read_text(encoding="utf-8"), "keep\n")
            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_config["agents"]["defaults"]["model"]["primary"], "unipay-fun/deepseek-v4-flash")
            self.assertEqual(saved_config["gateway"]["auth"]["token"], result["gateway_token"])
            self.assertIn(f"workspace not empty, skip populate: {workspace.resolve()}", runner.logs)

    def test_prepare_failure_rolls_back_partial_template_dir(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_root = tmp_path / "template"
            archive_path = template_root / "base.zip"
            config_path = tmp_path / ".openclaw" / "openclaw.json"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})
            self._write_host_config(config_path, {"agents": {"list": []}})

            manager = FailingPrepareManager(
                runner,
                template_root=str(template_root),
                config_path=str(config_path),
            )
            with self.assertRaises(RuntimeError):
                manager.create_instance(
                    CreateInstanceRequest(
                        template_name="base",
                        model_key="test-key",
                        workspace_root=str(tmp_path / "data"),
                    )
                )

            self.assertFalse((template_root / "base").exists())

    def test_populate_failure_rolls_back_workspace_and_agent(self):
        runner = WorkspaceCreatingRunner(
            responses={
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                ): {"ok": True},
                ("openclaw", "agents", "delete", "base", "--force", "--json"): {"ok": True},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_root = tmp_path / "template"
            workspace_root = tmp_path / "data"
            workspace = workspace_root / "base"
            archive_path = template_root / "base.zip"
            config_path = tmp_path / ".openclaw" / "openclaw.json"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})
            self._write_host_config(config_path, {"agents": {"list": []}})

            runner.responses[
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    str(workspace),
                    "--non-interactive",
                    "--json",
                )
            ] = runner.responses.pop(
                (
                    "openclaw",
                    "agents",
                    "add",
                    "base",
                    "--workspace",
                    "__WORKSPACE__",
                    "--non-interactive",
                    "--json",
                )
            )

            manager = FailingPopulateManager(
                runner,
                template_root=str(template_root),
                config_path=str(config_path),
            )
            with self.assertRaises(RuntimeError):
                manager.create_instance(
                    CreateInstanceRequest(
                        template_name="base",
                        model_key="test-key",
                        workspace_root=str(workspace_root),
                    )
                )

            self.assertFalse(workspace.exists())

    def test_create_instance_dry_run_keeps_workspace_unmodified(self):
        runner = FakeRunner(dry_run=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            archive_path = tmp_path / "template" / "base.zip"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})

            manager = InstanceManagerV2(runner, template_root=str(tmp_path / "template"))
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="base",
                    model_key="test-key",
                    workspace_root=str(tmp_path / "data"),
                )
            )

            self.assertTrue(result["ok"])
            self.assertIsInstance(result["gateway_token"], str)
            self.assertFalse((tmp_path / "data" / "base").exists())
            self.assertFalse((tmp_path / "template" / "base").exists())
            self.assertEqual(runner.calls[0][:4], ["openclaw", "agents", "add", "base"])
            self.assertEqual(result["steps"][-2]["step"], "config.configure_gateway_auth")

    def test_add_tg_bot_generates_name_and_writes_public_binding(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {
                    "agents": [{"id": "base"}]
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {"list": [{"id": "base"}]},
                        "bindings": [],
                        "channels": {"telegram": {"accounts": {}}},
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.add_tg_bot(
                AddTelegramBotRequest(
                    agent_name="base",
                    bot_token="123:abc",
                )
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertTrue(result["bot_name"].startswith("tgbot-"))
            self.assertEqual(runner.calls, self.gateway_service_restart_calls)
            self.assertEqual(
                saved["channels"]["telegram"]["accounts"][result["bot_name"]],
                {
                    "botToken": "123:abc",
                    "dmPolicy": "open",
                    "allowFrom": ["*"],
                },
            )
            self.assertEqual(
                saved["bindings"],
                [
                    {
                        "agentId": "base",
                        "match": {
                            "channel": "telegram",
                            "accountId": result["bot_name"],
                        },
                    }
                ],
            )

    def test_add_agents_runs_non_interactive_add_for_each_requested_agent(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_root = tmp_path / "template"
            template_root.mkdir(parents=True)
            self._write_archive(template_root / "base.zip", {"base/SOUL.md": "base soul\n"})
            self._write_archive(
                template_root / "demo-template.zip",
                {"demo-template/app/main.py": "print('demo')\n"},
            )
            config_path = tmp_path / "openclaw.json"
            config_path.write_text(json.dumps({"agents": {"list": []}}), encoding="utf-8")

            manager = InstanceManagerV2(
                runner,
                template_root=str(template_root),
                config_path=str(config_path),
            )
            result = manager.add_agents(
                AddAgentsRequest(
                    agents=[
                        AddAgentRequest(agent_name="base"),
                        AddAgentRequest(
                            agent_name="demo",
                            template_name="demo-template",
                            workspace=str(tmp_path / "custom-demo"),
                            model="openai/gpt-5",
                        ),
                    ],
                    workspace_root=str(tmp_path / "data"),
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["added_count"], 2)
            self.assertEqual(result["skipped_count"], 0)
            self.assertFalse(result["restart_required"])
            self.assertEqual(result["post_batch_actions"], [])
            self.assertEqual(
                (tmp_path / "data" / "base" / "SOUL.md").read_text(encoding="utf-8"),
                "base soul\n",
            )
            self.assertEqual(
                (tmp_path / "custom-demo" / "app" / "main.py").read_text(encoding="utf-8"),
                "print('demo')\n",
            )
            self.assertEqual(
                runner.calls,
                [
                    [
                        "openclaw",
                        "agents",
                        "add",
                        "base",
                        "--workspace",
                        str((tmp_path / "data" / "base").resolve()),
                        "--non-interactive",
                        "--json",
                    ],
                    [
                        "openclaw",
                        "agents",
                        "add",
                        "demo",
                        "--workspace",
                        str((tmp_path / "custom-demo").resolve()),
                        "--non-interactive",
                        "--json",
                        "--model",
                        "openai/gpt-5",
                    ],
                ],
            )
            self.assertEqual(result["steps"][0]["step"], "template.prepare[base]")
            self.assertEqual(result["steps"][1]["step"], "agents.add[base]")
            self.assertEqual(result["steps"][2]["step"], "workspace.populate[base]")
            self.assertEqual(result["steps"][3]["step"], "template.prepare[demo]")
            self.assertEqual(result["steps"][4]["step"], "agents.add[demo]")
            self.assertEqual(result["steps"][5]["step"], "workspace.populate[demo]")
            self.assertEqual(result["agents"][1]["template_name"], "demo-template")
            self.assertEqual(
                result["agents"][0]["result"]["template_prepare"]["archive_path"],
                str((template_root / "base.zip").resolve()),
            )

    def test_add_agents_skips_existing_agent_and_keeps_running(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_root = tmp_path / "template"
            template_root.mkdir(parents=True)
            self._write_archive(template_root / "base.zip", {"SOUL.md": "base soul\n"})
            self._write_archive(template_root / "demo.zip", {"SOUL.md": "demo soul\n"})
            config_path = tmp_path / "openclaw.json"
            config_path.write_text(
                json.dumps({"agents": {"list": [{"id": "base"}]}}),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(
                runner,
                template_root=str(template_root),
                config_path=str(config_path),
            )
            result = manager.add_agents(
                AddAgentsRequest(
                    agents=[
                        AddAgentRequest(agent_name="base"),
                        AddAgentRequest(agent_name="demo"),
                    ],
                    workspace_root=str(tmp_path / "data"),
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["added_count"], 1)
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(
                (tmp_path / "data" / "base" / "SOUL.md").read_text(encoding="utf-8"),
                "base soul\n",
            )
            self.assertEqual(
                result["agents"][0]["result"]["agents_add"],
                {
                    "skipped": True,
                    "reason": "agent_exists",
                    "agent_name": "base",
                },
            )
            self.assertEqual(
                runner.calls,
                [
                    [
                        "openclaw",
                        "agents",
                        "add",
                        "demo",
                        "--workspace",
                        str((tmp_path / "data" / "demo").resolve()),
                        "--non-interactive",
                        "--json",
                    ]
                ],
            )
            self.assertEqual(result["steps"][0]["step"], "template.prepare[base]")
            self.assertEqual(result["steps"][1]["step"], "agents.add[base]")
            self.assertEqual(result["steps"][2]["step"], "workspace.populate[base]")
            self.assertIn("agent exists, skip add: base", runner.logs)

    def test_add_tg_bot_replaces_existing_binding_for_same_bot_name(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {"list": [{"id": "base"}]},
                        "bindings": [
                            {
                                "agentId": "old-agent",
                                "match": {
                                    "channel": "telegram",
                                    "accountId": "publicbot",
                                },
                            },
                            {
                                "agentId": "other",
                                "match": {
                                    "channel": "telegram",
                                    "accountId": "otherbot",
                                },
                            },
                        ],
                        "channels": {
                            "telegram": {
                                "accounts": {
                                    "publicbot": {
                                        "botToken": "old",
                                        "dmPolicy": "allowlist",
                                        "allowFrom": ["1"],
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.add_tg_bot(
                AddTelegramBotRequest(
                    agent_name="base",
                    bot_token="123:new",
                    bot_name="publicbot",
                )
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(result["bot_name"], "publicbot")
            self.assertEqual(
                saved["channels"]["telegram"]["accounts"]["publicbot"],
                {
                    "botToken": "123:new",
                    "dmPolicy": "open",
                    "allowFrom": ["*"],
                },
            )
            self.assertEqual(
                saved["bindings"],
                [
                    {
                        "agentId": "other",
                        "match": {
                            "channel": "telegram",
                            "accountId": "otherbot",
                        },
                    },
                    {
                        "agentId": "base",
                        "match": {
                            "channel": "telegram",
                            "accountId": "publicbot",
                        },
                    },
                ],
            )
            self.assertEqual(runner.calls, self.gateway_service_restart_calls)

    def test_add_tg_bot_requires_existing_agent(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(json.dumps({"bindings": [], "agents": {"list": []}}), encoding="utf-8")

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            with self.assertRaises(FileNotFoundError):
                manager.add_tg_bot(
                    AddTelegramBotRequest(
                        agent_name="missing",
                        bot_token="123:abc",
                    )
                )

    def test_check_server_status_uses_gateway_status_and_embeds_tg_status(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "gateway", "status", "--require-rpc", "--json"): {
                    "ok": True,
                    "service": {"status": "running"},
                    "runtime": {"status": "running"},
                    "rpc": {"ok": True},
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [],
                        "agents": {
                            "defaults": {
                                "model": {
                                    "primary": "unipay-fun/gpt-5.4-nano",
                                }
                            }
                        },
                        "channels": {
                            "telegram": {
                                "enabled": True,
                                "accounts": {
                                    "publicbot": {"dmPolicy": "open"},
                                }
                            },
                            "openclaw-weixin": {
                                "accounts": {
                                    "bot-a-im-bot": {
                                        "enabled": True,
                                        "name": "客服A",
                                    }
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            accounts_dir = Path(tmpdir) / "openclaw-weixin" / "accounts"
            accounts_dir.mkdir(parents=True)
            (accounts_dir / "bot-a-im-bot.json").write_text(
                json.dumps(
                    {
                        "baseUrl": "https://ilinkai.weixin.qq.com",
                        "userId": "wx-user-1",
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.check_server_status()

            self.assertTrue(result["ok"])
            self.assertEqual(result["gateway_status"]["rpc"]["ok"], True)
            self.assertEqual(result["tg_bot_status"]["tg_bot_count"], 1)
            self.assertEqual(result["weixin_bot_status"]["weixin_bot_count"], 1)
            self.assertEqual(
                result["current_model_status"]["current_model"],
                "unipay-fun/gpt-5.4-nano",
            )
            self.assertEqual(result["timeout_seconds"], 10)
            self.assertEqual(
                runner.calls[0],
                ["openclaw", "gateway", "status", "--require-rpc", "--json"],
            )

    def test_get_tg_bot_status_returns_bound_bot_count(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [
                            {
                                "agentId": "base",
                                "match": {"channel": "telegram", "accountId": "publicbot"},
                            },
                            {
                                "agentId": "demo",
                                "match": {"channel": "telegram", "accountId": "publicbot"},
                            },
                            {
                                "agentId": "other",
                                "match": {"channel": "telegram", "accountId": "otherbot"},
                            },
                        ],
                        "channels": {
                            "telegram": {
                                "enabled": True,
                                "accounts": {
                                    "publicbot": {"dmPolicy": "open"},
                                    "otherbot": {"dmPolicy": "open"},
                                    "idlebot": {"dmPolicy": "open"},
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.get_tg_bot_status()

            self.assertTrue(result["ok"])
            self.assertEqual(result["tg_bot_count"], 3)
            self.assertEqual(result["bound_tg_bot_count"], 2)
            self.assertEqual(result["total_binding_count"], 3)
            self.assertEqual(
                [item["bot_name"] for item in result["bots"]],
                ["idlebot", "otherbot", "publicbot"],
            )

    def test_add_weixin_bot_writes_state_config_and_binding(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            config_path = state_dir / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {"list": [{"id": "unipay-claw-base"}]},
                        "plugins": {"entries": {"openclaw-weixin": {"enabled": True}}},
                        "bindings": [],
                        "channels": {},
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.add_weixin_bot(
                AddWeixinBotRequest(
                    agent_name="unipay-claw-base",
                    ilink_bot_id="B0F5860FDECB@im.bot",
                    bot_token="wx-token",
                    baseurl="https://ilinkai.weixin.qq.com",
                    ilink_user_id="wx-user-1",
                    bot_name="客服微信",
                    route_tag="route-a",
                )
            )

            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            account_id = "b0f5860fdecb-im-bot"
            account_path = state_dir / "openclaw-weixin" / "accounts" / f"{account_id}.json"
            index_path = state_dir / "openclaw-weixin" / "accounts.json"

            self.assertTrue(result["ok"])
            self.assertEqual(result["account_id"], account_id)
            self.assertTrue(result["plugin_prepare"]["install_check_skipped"])
            self.assertTrue(result["plugin_prepare"]["restart_required"])
            self.assertEqual(runner.calls, self.gateway_service_restart_calls)
            self.assertTrue(account_path.exists())
            self.assertTrue(index_path.exists())
            self.assertEqual(
                json.loads(account_path.read_text(encoding="utf-8"))["token"],
                "wx-token",
            )
            self.assertEqual(
                saved_config["channels"]["openclaw-weixin"]["accounts"][account_id]["name"],
                "客服微信",
            )
            self.assertEqual(
                saved_config["channels"]["openclaw-weixin"]["accounts"][account_id]["routeTag"],
                "route-a",
            )
            self.assertEqual(
                saved_config["bindings"],
                [
                    {
                        "agentId": "unipay-claw-base",
                        "match": {
                            "channel": "openclaw-weixin",
                            "accountId": account_id,
                        },
                    }
                ],
            )

    def test_add_weixin_bot_enables_plugin_and_always_restarts_gateway(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {"list": [{"id": "unipay-claw-base"}]},
                        "plugins": {"entries": {}},
                        "bindings": [],
                        "channels": {},
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.add_weixin_bot(
                AddWeixinBotRequest(
                    agent_name="unipay-claw-base",
                    ilink_bot_id="wx@im.bot",
                    bot_token="wx-token",
                )
            )

            self.assertTrue(result["ok"])
            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(saved_config["plugins"]["entries"]["openclaw-weixin"]["enabled"])
            self.assertEqual(runner.calls, self.gateway_service_restart_calls)

    def test_add_weixin_bot_ignores_sync_sidecar_files_when_clearing_stale_accounts(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            config_path = state_dir / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {"list": [{"id": "unipay-claw-base"}]},
                        "plugins": {"entries": {"openclaw-weixin": {"enabled": True}}},
                        "bindings": [],
                        "channels": {},
                    }
                ),
                encoding="utf-8",
            )
            state_root = state_dir / "openclaw-weixin"
            accounts_dir = state_root / "accounts"
            accounts_dir.mkdir(parents=True)
            (accounts_dir / "old-bot-im-bot.json").write_text(
                json.dumps({"userId": "wx-user-1", "token": "old-token"}),
                encoding="utf-8",
            )
            (accounts_dir / "old-bot-im-bot.sync.json").write_text(
                json.dumps({"synced": True}),
                encoding="utf-8",
            )
            (state_root / "accounts.json").write_text(
                json.dumps(["old-bot-im-bot"]) + "\n",
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.add_weixin_bot(
                AddWeixinBotRequest(
                    agent_name="unipay-claw-base",
                    ilink_bot_id="new-bot@im.bot",
                    bot_token="wx-token",
                    ilink_user_id="wx-user-1",
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["stale_accounts_cleared"], ["old-bot-im-bot"])
            self.assertFalse((accounts_dir / "old-bot-im-bot.json").exists())
            self.assertFalse((accounts_dir / "old-bot-im-bot.sync.json").exists())
            self.assertTrue((accounts_dir / "new-bot-im-bot.json").exists())

    def test_get_weixin_bot_status_returns_bound_bot_count(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            config_path = state_dir / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [
                            {
                                "agentId": "unipay-claw-base",
                                "match": {
                                    "channel": "openclaw-weixin",
                                    "accountId": "bot-a-im-bot",
                                },
                            },
                            {
                                "agentId": "demo",
                                "match": {
                                    "channel": "openclaw-weixin",
                                    "accountId": "bot-a-im-bot",
                                },
                            },
                        ],
                        "channels": {
                            "openclaw-weixin": {
                                "accounts": {
                                    "bot-a-im-bot": {
                                        "enabled": True,
                                        "name": "客服A",
                                        "routeTag": "route-a",
                                    },
                                    "bot-b-im-bot": {
                                        "enabled": True,
                                    },
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            accounts_dir = state_dir / "openclaw-weixin" / "accounts"
            accounts_dir.mkdir(parents=True)
            (accounts_dir / "bot-a-im-bot.json").write_text(
                json.dumps(
                    {
                        "baseUrl": "https://ilinkai.weixin.qq.com",
                        "userId": "wx-user-1",
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.get_weixin_bot_status()

            self.assertTrue(result["ok"])
            self.assertEqual(result["weixin_bot_count"], 2)
            self.assertEqual(result["bound_weixin_bot_count"], 1)
            self.assertEqual(result["total_binding_count"], 2)
            self.assertEqual(
                [item["account_id"] for item in result["bots"]],
                ["bot-a-im-bot", "bot-b-im-bot"],
            )
            self.assertEqual(result["bots"][0]["bot_name"], "客服A")
            self.assertEqual(result["bots"][0]["has_state_file"], True)
            self.assertEqual(result["bots"][1]["has_state_file"], False)

    def test_delete_weixin_bot_removes_account_binding_and_state(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            config_path = state_dir / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [
                            {
                                "agentId": "unipay-claw-base",
                                "match": {
                                    "channel": "openclaw-weixin",
                                    "accountId": "bot-a-im-bot",
                                },
                            },
                            {
                                "agentId": "demo",
                                "match": {
                                    "channel": "telegram",
                                    "accountId": "tg-a",
                                },
                            },
                        ],
                        "channels": {
                            "openclaw-weixin": {
                                "accounts": {
                                    "bot-a-im-bot": {
                                        "enabled": True,
                                        "name": "客服A",
                                    },
                                    "bot-b-im-bot": {
                                        "enabled": True,
                                    },
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            state_root = state_dir / "openclaw-weixin"
            accounts_dir = state_root / "accounts"
            accounts_dir.mkdir(parents=True)
            (accounts_dir / "bot-a-im-bot.json").write_text("{}", encoding="utf-8")
            (accounts_dir / "bot-a-im-bot.sync.json").write_text("{}", encoding="utf-8")
            (state_root / "accounts.json").write_text(
                json.dumps(["bot-a-im-bot", "bot-b-im-bot"]) + "\n",
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.delete_weixin_bot(
                DeleteWeixinBotRequest(ilink_bot_id="bot-a@im.bot")
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["deleted_account_id"], "bot-a-im-bot")
            self.assertEqual(result["removed_bindings"], 1)
            self.assertEqual(result["remaining_weixin_bot_count"], 1)
            self.assertNotIn(
                "bot-a-im-bot",
                saved["channels"]["openclaw-weixin"]["accounts"],
            )
            self.assertFalse((accounts_dir / "bot-a-im-bot.json").exists())
            self.assertFalse((accounts_dir / "bot-a-im-bot.sync.json").exists())
            self.assertEqual(
                json.loads((state_root / "accounts.json").read_text(encoding="utf-8")),
                ["bot-b-im-bot"],
            )

    def test_delete_weixin_bot_requires_existing_account(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps({"channels": {"openclaw-weixin": {"accounts": {}}}}),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            with self.assertRaises(FileNotFoundError):
                manager.delete_weixin_bot(
                    DeleteWeixinBotRequest(ilink_bot_id="missing@im.bot")
                )

    def test_delete_tg_bot_removes_account_and_bindings(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [
                            {
                                "agentId": "base",
                                "match": {"channel": "telegram", "accountId": "publicbot"},
                            },
                            {
                                "agentId": "other",
                                "match": {"channel": "telegram", "accountId": "otherbot"},
                            },
                        ],
                        "channels": {
                            "telegram": {
                                "enabled": True,
                                "accounts": {
                                    "publicbot": {"botToken": "123:abc"},
                                    "otherbot": {"botToken": "456:def"},
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.delete_tg_bot(DeleteTelegramBotRequest(bot_name="publicbot"))

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["removed_bindings"], 1)
            self.assertEqual(result["remaining_tg_bot_count"], 1)
            self.assertNotIn("publicbot", saved["channels"]["telegram"]["accounts"])
            self.assertEqual(
                saved["bindings"],
                [
                    {
                        "agentId": "other",
                        "match": {
                            "channel": "telegram",
                            "accountId": "otherbot",
                        },
                    }
                ],
            )

    def test_delete_tg_bot_requires_existing_bot(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps({"channels": {"telegram": {"accounts": {}}}}),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            with self.assertRaises(FileNotFoundError):
                manager.delete_tg_bot(DeleteTelegramBotRequest(bot_name="missingbot"))

    def test_set_model_runs_models_set_only(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "providers": {
                                "unipay-fun": {
                                    "models": [item["definition"] for item in self.sample_supported_models]
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = InstanceManagerV2(runner, config_path=str(config_path))

            result = manager.set_model(SetModelRequest(model_ref="unipay-fun/gpt-5.4"))

            self.assertTrue(result["ok"])
            self.assertEqual(result["model_ref"], "unipay-fun/gpt-5.4")
            self.assertEqual(
                runner.calls,
                [
                    ["openclaw", "models", "set", "unipay-fun/gpt-5.4"],
                ],
            )

    def test_set_model_rejects_unsupported_model(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "providers": {
                                "unipay-fun": {
                                    "models": [item["definition"] for item in self.sample_supported_models]
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = InstanceManagerV2(runner, config_path=str(config_path))

            with self.assertRaises(ValueError):
                manager.set_model(SetModelRequest(model_ref="gpt-4o"))

    def test_get_supported_models_reads_config_only(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "model": {
                                    "primary": "unipay-fun/gpt-5.4-nano",
                                }
                            }
                        },
                        "models": {
                            "providers": {
                                "unipay-fun": {
                                    "models": [item["definition"] for item in self.sample_supported_models]
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.get_supported_models()

        self.assertTrue(result["ok"])
        self.assertEqual(result["current_model"], "unipay-fun/gpt-5.4-nano")
        self.assertEqual(
            result["supported_model_refs"],
            [
                "unipay-fun/deepseek-v4-flash",
                "unipay-fun/deepseek-v4-pro",
                "unipay-fun/gpt-5.4",
                "unipay-fun/gpt-5.3-codex",
                "unipay-fun/gpt-5.4-nano",
                "unipay-fun/claude-sonnet-4-6",
            ],
        )
        self.assertEqual(runner.calls, [])

    def test_update_model_catalog_preserves_current_model_when_still_supported(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "model": {
                                    "primary": "unipay-fun/claude-sonnet-4-6",
                                }
                            }
                        },
                        "models": {
                            "providers": {
                                "unipay-fun": {
                                    "apiKey": "test-key",
                                    "models": [],
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.update_model_catalog()

            saved_config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["current_model_before"], "unipay-fun/claude-sonnet-4-6")
        self.assertEqual(result["current_model_after"], "unipay-fun/claude-sonnet-4-6")
        self.assertEqual(
            saved_config["agents"]["defaults"]["model"]["primary"],
            "unipay-fun/claude-sonnet-4-6",
        )
        self.assertEqual(
            saved_config["models"]["providers"]["unipay-fun"]["models"],
            [item["definition"] for item in self.sample_supported_models],
        )
        self.assertEqual(result["steps"][0]["step"], "models.fetch_catalog")
        self.assertEqual(result["steps"][1]["step"], "config.configure_models")

    def test_update_model_catalog_falls_back_when_current_model_no_longer_supported(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "model": {
                                    "primary": "unipay-fun/removed-model",
                                }
                            }
                        },
                        "models": {
                            "providers": {
                                "unipay-fun": {
                                    "apiKey": "test-key",
                                    "models": [],
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.update_model_catalog()

        self.assertTrue(result["ok"])
        self.assertEqual(result["current_model_before"], "unipay-fun/removed-model")
        self.assertEqual(result["current_model_after"], "unipay-fun/deepseek-v4-flash")

    def test_list_agents_excludes_main(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {
                    "agents": [
                        {"id": "main", "name": "main"},
                        {"id": "unipay-claw-base", "name": "unipay-claw-base"},
                    ]
                }
            }
        )
        manager = InstanceManagerV2(runner)

        result = manager.list_agents()

        self.assertTrue(result["ok"])
        self.assertEqual(result["agent_count"], 1)
        self.assertEqual(
            result["agents"],
            [{"id": "unipay-claw-base", "name": "unipay-claw-base"}],
        )
        self.assertEqual(runner.calls, [["openclaw", "agents", "list", "--bindings", "--json"]])

    def test_get_current_model_reads_config_only(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "model": {
                                    "primary": "unipay-fun/gpt-5.4-nano",
                                }
                            },
                            "list": [
                                {"id": "main"},
                                {
                                    "id": "unipay-claw-base",
                                    "model": {
                                        "primary": "unipay-fun/gpt-5.4-mini",
                                    },
                                },
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.get_current_model()

        self.assertTrue(result["ok"])
        self.assertEqual(result["current_model"], "unipay-fun/gpt-5.4-nano")
        self.assertEqual(
            result["agent_overrides"],
            [{"agent_id": "unipay-claw-base", "model": "unipay-fun/gpt-5.4-mini"}],
        )
        self.assertEqual(runner.calls, [])

    def test_get_current_gateway_token_reads_config_only(self):
        runner = FakeRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gateway": {
                            "auth": {
                                "mode": "token",
                                "token": "gateway-token-123",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.get_current_gateway_token()

        self.assertTrue(result["ok"])
        self.assertEqual(result["gateway_auth_mode"], "token")
        self.assertEqual(result["gateway_token"], "gateway-token-123")
        self.assertEqual(result["config_path"], str(config_path.resolve()))
        self.assertEqual(result["config_exists"], True)
        self.assertEqual(runner.calls, [])

    def _write_archive(self, archive_path: Path, files):
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)


if __name__ == "__main__":
    unittest.main()
