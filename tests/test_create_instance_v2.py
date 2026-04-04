import tempfile
import unittest
import zipfile
import json
from pathlib import Path

from agent_manage_v2.models import (
    AddTelegramBotRequest,
    CreateInstanceRequest,
    DeleteTelegramBotRequest,
)
from agent_manage_v2.orchestrator import InstanceManagerV2


class FakeRunner:
    def __init__(self, responses=None, dry_run=False):
        self.responses = responses or {}
        self.calls = []
        self.dry_run = dry_run
        self.openclaw_bin = "openclaw"

    def run_json(self, args, timeout=None):
        key = tuple(args)
        self.calls.append(list(args))
        return self.responses.get(key, {"ok": True})

    def log(self, message):
        return None


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
    def test_create_instance_populates_workspace_and_overlays_template(self):
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
            archive_path.parent.mkdir(parents=True, exist_ok=True)

            self._write_archive(
                archive_path,
                {
                    "base/app/main.py": "print('hello')\n",
                    "base/SOUL.md": "old soul\n",
                    "base/skills/weather/SKILL.md": "old weather\n",
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

            manager = InstanceManagerV2(runner, template_root=str(tmp_path / "template"))
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="base",
                    model="openai/gpt-5",
                    workspace_root=str(workspace_root),
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(runner.calls[0][:4], ["openclaw", "agents", "list", "--bindings"])
            self.assertEqual(runner.calls[1][:4], ["openclaw", "agents", "add", "base"])
            self.assertEqual((workspace / "app" / "main.py").read_text(encoding="utf-8"), "print('hello')\n")
            self.assertEqual((workspace / "SOUL.md").read_text(encoding="utf-8"), "old soul\n")
            self.assertEqual((template_dir / "SOUL.md").read_text(encoding="utf-8"), "old soul\n")
            self.assertEqual(
                (workspace / "skills" / "weather" / "SKILL.md").read_text(encoding="utf-8"),
                "old weather\n",
            )
            self.assertEqual(result["template_dir"], str(template_dir.resolve()))

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
            archive_path.parent.mkdir(parents=True)
            self._write_archive(
                archive_path,
                {
                    "app/main.py": "print('hello')\n",
                    "SOUL.md": "zip soul\n",
                    "skills/weather/SKILL.md": "zip weather\n",
                },
            )

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

            manager = InstanceManagerV2(runner, template_root=str(tmp_path / "template"))
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="unipay-claw-base",
                    workspace_root=str(workspace_root),
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["agent_name"], "unipay-claw-base")
            self.assertEqual(result["template_dir"], str((tmp_path / "template" / "unipay-claw-base").resolve()))
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

    def test_create_instance_fails_when_agent_exists(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {
                    "agents": [{"id": "base"}]
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            archive_path = tmp_path / "template" / "base.zip"
            template_dir = tmp_path / "template" / "base"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})

            manager = InstanceManagerV2(runner, template_root=str(tmp_path / "template"))
            with self.assertRaises(FileExistsError):
                manager.create_instance(
                    CreateInstanceRequest(
                        template_name="base",
                        workspace_root=str(tmp_path / "data"),
                    )
                )

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
            workspace.mkdir(parents=True)
            archive_path = tmp_path / "template" / "base.zip"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})

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

            manager = InstanceManagerV2(runner, template_root=str(tmp_path / "template"))
            result = manager.create_instance(
                CreateInstanceRequest(
                    template_name="base",
                    workspace_root=str(workspace_root),
                )
            )

            self.assertTrue(result["ok"])
            self.assertEqual((workspace / "main.py").read_text(encoding="utf-8"), "print('x')\n")

    def test_create_instance_rejects_non_empty_workspace_directory(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            workspace_root = tmp_path / "data"
            workspace = workspace_root / "base"
            workspace.mkdir(parents=True)
            (workspace / "old.txt").write_text("keep\n", encoding="utf-8")
            archive_path = tmp_path / "template" / "base.zip"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})

            manager = InstanceManagerV2(runner, template_root=str(tmp_path / "template"))
            with self.assertRaises(FileExistsError):
                manager.create_instance(
                    CreateInstanceRequest(
                        template_name="base",
                        workspace_root=str(workspace_root),
                    )
                )

    def test_prepare_failure_rolls_back_partial_template_dir(self):
        runner = FakeRunner(responses={("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []}})

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_root = tmp_path / "template"
            archive_path = template_root / "base.zip"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})

            manager = FailingPrepareManager(runner, template_root=str(template_root))
            with self.assertRaises(RuntimeError):
                manager.create_instance(
                    CreateInstanceRequest(
                        template_name="base",
                        workspace_root=str(tmp_path / "data"),
                    )
                )

            self.assertFalse((template_root / "base").exists())

    def test_populate_failure_rolls_back_workspace_and_agent(self):
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
                ("openclaw", "agents", "delete", "base", "--force", "--json"): {"ok": True},
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            template_root = tmp_path / "template"
            workspace_root = tmp_path / "data"
            workspace = workspace_root / "base"
            archive_path = template_root / "base.zip"
            archive_path.parent.mkdir(parents=True)
            self._write_archive(archive_path, {"main.py": "print('x')\n"})

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

            manager = FailingPopulateManager(runner, template_root=str(template_root))
            with self.assertRaises(RuntimeError):
                manager.create_instance(
                    CreateInstanceRequest(
                        template_name="base",
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
                    workspace_root=str(tmp_path / "data"),
                )
            )

            self.assertTrue(result["ok"])
            self.assertFalse((tmp_path / "data" / "base").exists())
            self.assertFalse((tmp_path / "template" / "base").exists())
            self.assertEqual(runner.calls[0][:4], ["openclaw", "agents", "add", "base"])

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

    def test_add_tg_bot_replaces_existing_binding_for_same_bot_name(self):
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

    def test_add_tg_bot_requires_existing_agent(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []}
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(json.dumps({"bindings": []}), encoding="utf-8")

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            with self.assertRaises(FileNotFoundError):
                manager.add_tg_bot(
                    AddTelegramBotRequest(
                        agent_name="missing",
                        bot_token="123:abc",
                    )
                )

    def test_check_server_status_uses_lightweight_openclaw_check(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {
                    "agents": [{"id": "base"}, {"id": "demo"}]
                }
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(json.dumps({"bindings": []}), encoding="utf-8")

            manager = InstanceManagerV2(runner, config_path=str(config_path))
            result = manager.check_server_status()

            self.assertTrue(result["ok"])
            self.assertEqual(result["openclaw_status"], "running")
            self.assertEqual(result["agent_count"], 2)
            self.assertEqual(result["timeout_seconds"], 10)
            self.assertEqual(
                runner.calls[0],
                ["openclaw", "agents", "list", "--bindings", "--json"],
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

    def _write_archive(self, archive_path: Path, files):
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)


if __name__ == "__main__":
    unittest.main()
