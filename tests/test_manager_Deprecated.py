import json
import tempfile
import unittest
from pathlib import Path

from openclaw_remote_Deprecated.local import LocalRunner
from openclaw_remote_Deprecated.models import (
    CreateAgentRequest,
    DeleteAgentRequest,
    TelegramAccountConfig,
)
from openclaw_remote_Deprecated.orchestrator import OpenClawManager


class FakeRunner:
    def __init__(self, responses=None, failures=None, dry_run=False):
        self.responses = responses or {}
        self.failures = failures or {}
        self.calls = []
        self.dry_run = dry_run
        self.openclaw_bin = "openclaw"

    def run_json(self, args):
        key = tuple(args)
        self.calls.append(list(args))
        if key in self.failures:
            raise RuntimeError(self.failures[key])
        return self.responses.get(key, {"ok": True})

    def log(self, message):
        return None


class OpenClawManagerTest(unittest.TestCase):
    def test_create_sets_agent_telegram_and_peer_binding(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []},
                (
                    "openclaw",
                    "agents",
                    "add",
                    "123456789",
                    "--workspace",
                    "/data/openclaw/123456789",
                    "--non-interactive",
                    "--json",
                    "--model",
                    "openai/gpt-5",
                ): {"ok": True},
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [],
                        "channels": {"telegram": {"accounts": {}}},
                        "agents": {"list": []},
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenClawManager(runner, config_path=str(config_path))

            result = manager.create(
                CreateAgentRequest(
                    tg_id="123456789",
                    model="openai/gpt-5",
                    telegram=TelegramAccountConfig(
                        account_id="unipaytgbot",
                        bot_token="123:abc",
                    ),
                )
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(runner.calls[0][:4], ["openclaw", "agents", "list", "--bindings"])
            self.assertEqual(runner.calls[1][:4], ["openclaw", "agents", "add", "123456789"])
            account = saved["channels"]["telegram"]["accounts"]["unipaytgbot"]
            self.assertEqual(account["botToken"], "123:abc")
            self.assertEqual(account["dmPolicy"], "allowlist")
            self.assertEqual(account["allowFrom"], ["123456789"])
            binding = saved["bindings"][0]
            self.assertEqual(binding["agentId"], "123456789")
            self.assertEqual(binding["match"]["accountId"], "unipaytgbot")
            self.assertEqual(binding["match"]["peer"]["id"], "123456789")

    def test_delete_keeps_workspace_by_default(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {
                    "agents": [{"id": "123456789", "workspace": "/srv/ws/support"}]
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [
                            {
                                "agentId": "123456789",
                                "match": {
                                    "channel": "telegram",
                                    "accountId": "unipaytgbot",
                                    "peer": {"kind": "dm", "id": "123456789"},
                                },
                            }
                        ],
                        "channels": {
                            "telegram": {
                                "accounts": {
                                    "unipaytgbot": {
                                        "botToken": "123:abc",
                                        "dmPolicy": "allowlist",
                                        "allowFrom": ["123456789", "999"],
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenClawManager(runner, config_path=str(config_path))

            result = manager.delete(DeleteAgentRequest(tg_id="123456789"))

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(result["workspace_purged"])
            self.assertEqual(result["workspace"], "/srv/ws/support")
            self.assertEqual(saved["bindings"], [])
            self.assertEqual(
                saved["channels"]["telegram"]["accounts"]["unipaytgbot"]["allowFrom"],
                ["999"],
            )

    def test_create_reuses_existing_agent_and_existing_single_bot(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {
                    "agents": [{"id": "123456789", "workspace": "/data/openclaw/123456789"}]
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {"list": [{"id": "123456789", "model": "openai/gpt-4.1"}]},
                        "channels": {
                            "telegram": {
                                "accounts": {
                                    "unipaytgbot": {
                                        "botToken": "123:abc",
                                        "dmPolicy": "allowlist",
                                        "allowFrom": ["111"],
                                    }
                                }
                            }
                        },
                        "bindings": [],
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenClawManager(runner, config_path=str(config_path))

            result = manager.create(CreateAgentRequest(tg_id="123456789", model="openai/gpt-5"))

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(saved["agents"]["list"][0]["model"], "openai/gpt-5")
            self.assertEqual(
                saved["channels"]["telegram"]["accounts"]["unipaytgbot"]["allowFrom"],
                ["111", "123456789"],
            )

    def test_purge_workspace_removes_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "support"
            workspace.mkdir()
            (workspace / "memo.txt").write_text("x", encoding="utf-8")

            runner = FakeRunner(
                responses={
                    ("openclaw", "agents", "list", "--bindings", "--json"): {
                        "agents": [{"id": "123456789", "workspace": str(workspace)}]
                    }
                }
            )
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(json.dumps({"bindings": []}), encoding="utf-8")
            manager = OpenClawManager(runner, config_path=str(config_path))
            result = manager.delete(DeleteAgentRequest(tg_id="123456789", purge_workspace=True))

            self.assertTrue(result["workspace_purged"])
            self.assertFalse(workspace.exists())

    def test_agent_name_overrides_default_agent_id_and_workspace(self):
        runner = FakeRunner(
            responses={
                ("openclaw", "agents", "list", "--bindings", "--json"): {"agents": []},
                (
                    "openclaw",
                    "agents",
                    "add",
                    "alice",
                    "--workspace",
                    "/data/openclaw/alice",
                    "--non-interactive",
                    "--json",
                    "--model",
                    "openai/gpt-5",
                ): {"ok": True},
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bindings": [],
                        "channels": {"telegram": {"accounts": {}}},
                        "agents": {"list": []},
                    }
                ),
                encoding="utf-8",
            )
            manager = OpenClawManager(runner, config_path=str(config_path))
            result = manager.create(
                CreateAgentRequest(
                    tg_id="123456789",
                    agent_name="alice",
                    model="openai/gpt-5",
                    telegram=TelegramAccountConfig(
                        account_id="unipaytgbot",
                        bot_token="123:abc",
                    ),
                )
            )
            self.assertEqual(result["agent_id"], "alice")
            self.assertEqual(result["workspace"], "/data/openclaw/alice")
            self.assertEqual(runner.calls[1][3], "alice")


class LocalRunnerTest(unittest.TestCase):
    def test_dry_run_returns_rendered_command(self):
        runner = LocalRunner(openclaw_bin="openclaw", dry_run=True)
        result = runner.run_json(["openclaw", "agents", "list", "--json"])
        self.assertTrue(result["skipped"])
        self.assertIn("openclaw agents list --json", result["command"])

    def test_extract_json_ignores_leading_and_trailing_noise(self):
        runner = LocalRunner(openclaw_bin="openclaw", dry_run=True)
        payload = runner._extract_json(
            '[plugins] example\n{"ok": true, "items": [1, 2]}\nextra trailing line'
        )
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["items"], [1, 2])


if __name__ == "__main__":
    unittest.main()
