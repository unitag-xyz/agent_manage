import json
import tempfile
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock, patch

from agent_manage.adapters.openclaw.local import CommandError, CommandResult
from agent_manage.models import (
    AddFeishuBotRequest, AddTelegramBotRequest,
    DeleteFeishuBotRequest, DeleteTelegramBotRequest, DeleteWeixinBotRequest,
)
from agent_manage.adapters.openclaw.orchestrator import InstanceManagerV2
from agent_manage.response import build_error_response
from agent_manage.adapters.openclaw.seed import _atomic_write_runtime_config


class RefactorContractsTest(unittest.TestCase):
    def test_wrapped_command_error_preserves_timeout_and_output(self):
        manager = object.__new__(InstanceManagerV2)
        for timed_out, returncode in ((True, 124), (False, 2)):
            with self.subTest(timed_out=timed_out):
                failure = CommandError("failed", CommandResult(
                    argv=["openclaw", "agents", "list"], command_text="openclaw agents list",
                    returncode=returncode, stdout="partial", stderr="failure", timed_out=timed_out))
                direct = build_error_response(failure)
                wrapped = manager._create_instance_failure(failure, [], [], perf_counter())
                response = build_error_response(wrapped)
                self.assertEqual(response["error"]["details"], direct["error"]["details"])
                self.assertEqual(response["typeCode"], direct["typeCode"])

    def test_seed_failed_flush_preserves_config_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openclaw.json"
            path.write_bytes(b'{"gateway":{"token":"original"}}')
            before = path.read_bytes()
            with patch("os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    _atomic_write_runtime_config(path, '{"new":true}')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([p.name for p in Path(directory).iterdir()], [path.name])

    def test_channel_rebinding_removes_duplicates_but_preserves_other_routes(self):
        cases = (("telegram", "add_tg_bot", AddTelegramBotRequest("new", "secret", "shared")),
                 ("feishu", "add_feishu_bot", AddFeishuBotRequest("new", "app", "secret", account_id="shared")))
        for channel, method, request in cases:
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                config = {"agents": {"list": [{"id": "new"}]},
                          "channels": {channel: {"accounts": {"shared": {}, "other": {}}}},
                          "bindings": self._bindings(channel)}
                manager, path = self._manager(directory, config)
                result = getattr(manager, method)(request)
                saved = json.loads(path.read_text())
                self.assertEqual(result["config_write"]["removed_existing_bindings"], 2)
                self.assertEqual(saved["bindings"], config["bindings"][2:] + [
                    {"agentId": "new", "match": {"channel": channel, "accountId": "shared"}}])
                self.assertTrue(manager.restart_required)

    def test_channel_deletion_preserves_other_bindings_and_policies(self):
        operations = (("telegram", "delete_tg_bot", DeleteTelegramBotRequest("shared")),
                      ("feishu", "delete_feishu_bot", DeleteFeishuBotRequest("shared")),
                      ("openclaw-weixin", "delete_weixin_bot", DeleteWeixinBotRequest("shared")))
        for channel, method, request in operations:
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                config = {"channels": {name: {"enabled": True, "custom": "keep",
                          "accounts": {"shared": {}, "other": {}}} for name, _, _ in operations},
                          "bindings": self._bindings(channel)}
                manager, path = self._manager(directory, config)
                manager._delete_weixin_account_state = Mock(return_value={"deleted": False})
                result = getattr(manager, method)(request)
                after = json.loads(path.read_text())
                self.assertEqual(result["removed_bindings"], 2)
                self.assertEqual(after["bindings"], config["bindings"][2:])
                self.assertEqual(after["channels"][channel]["custom"], "keep")
                self.assertNotIn("shared", after["channels"][channel]["accounts"])
                self.assertEqual(manager.restart_required, channel == "feishu")

    def test_channel_status_counts_bindings_without_changing_config(self):
        for channel, method in (("telegram", "get_tg_bot_status"),
                                ("feishu", "get_feishu_bot_status"),
                                ("openclaw-weixin", "get_weixin_bot_status")):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                config = {"channels": {channel: {"enabled": True, "accounts": {"shared": {}, "unbound": {}}}},
                          "bindings": self._bindings(channel)[:2] + [self._bindings(channel)[-1]]}
                manager, path = self._manager(directory, config)
                before = path.read_bytes()
                manager._load_weixin_account_state = Mock(return_value=None)
                result = getattr(manager, method)()
                self.assertEqual(result["total_binding_count"], 2)
                self.assertEqual([bot["binding_count"] for bot in result["bots"]], [2, 0])
                self.assertEqual(path.read_bytes(), before)

    @staticmethod
    def _bindings(channel):
        return [{"agentId": "old", "match": {"channel": channel, "accountId": "shared"}},
                {"agentId": "duplicate", "match": {"channel": channel, "accountId": "shared"}},
                {"agentId": "keep", "match": {"channel": channel, "accountId": "other"}},
                {"agentId": "keep", "match": {"channel": "custom", "accountId": "shared"}}]

    @staticmethod
    def _manager(directory, config):
        path = Path(directory) / "openclaw.json"
        path.write_text(json.dumps(config))
        manager = InstanceManagerV2(Mock(dry_run=False, openclaw_bin="openclaw"), config_path=str(path))
        manager.container_runtime = True
        return manager, path
