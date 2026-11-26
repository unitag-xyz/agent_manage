import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from agent_manage.cli import main
from agent_manage.adapters.openclaw.orchestrator import InstanceManagerV2


class WorkflowContractsTest(unittest.TestCase):
    def test_simple_commands_keep_the_existing_response_and_restart_marker(self):
        commands = {"agents-list": "list_agents", "models": "get_supported_models",
                    "current-model": "get_current_model", "current-gateway-token": "get_current_gateway_token",
                    "update-model": "update_model_catalog", "check-server-status": "check_server_status",
                    "tg-bot-status": "get_tg_bot_status", "feishu-bot-status": "get_feishu_bot_status",
                    "weixin-bot-status": "get_weixin_bot_status"}
        for command, method in commands.items():
            with self.subTest(command=command), patch("agent_manage.adapters.openclaw.execution.InstanceManagerV2") as cls:
                manager = cls.return_value
                manager.container_runtime = True
                manager.restart_required = True
                getattr(manager, method).return_value = {"ok": True, "command": command}
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = main([command])
                result = json.loads(stdout.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(result["result"], {"ok": True, "command": command})
                self.assertTrue(result["restartRequired"])
                self.assertIsNone(result["error"])
                getattr(manager, method).assert_called_once_with()

    def test_archive_and_prepared_template_keep_the_same_agent_steps_and_workspace(self):
        results = []
        for archived in (True, False):
            with self.subTest(archived=archived), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "openclaw.json"
                config.write_text('{"agents":{"list":[]}}')
                template = root / "template"
                template.mkdir()
                (template / "SOUL.md").write_text("template soul")
                archive = root / "template.zip"
                with zipfile.ZipFile(archive, "w") as zip_file:
                    zip_file.write(template / "SOUL.md", "SOUL.md")
                manager = InstanceManagerV2(Mock(dry_run=False, openclaw_bin="openclaw"), config_path=str(config))
                manager._add_agent = Mock(return_value={"agent_name": "example"})
                manager._safe_delete_agent = Mock(return_value={"deleted": True})
                steps = []
                kwargs = dict(steps=steps, template_name="example", agent_name="example",
                              template_dir=template, workspace=root / "workspace", model=None,
                              rollback_on_fail=True, step_scope="example")
                if archived:
                    result = manager._provision_agent_from_template(archive_path=archive, **kwargs)
                else:
                    result = manager._provision_agent_from_prepared_template(**kwargs)
                self.assertTrue(result["created_agent"])
                self.assertTrue(result["created_workspace"])
                self.assertEqual((root / "workspace" / "SOUL.md").read_text(), "template soul")
                manager._add_agent.assert_called_once_with(agent_name="example", workspace=root / "workspace", model=None)
                results.append([step["step"] for step in steps if "template.archive" not in step["step"]
                                and not step["step"].startswith("template.prepare")])
        self.assertEqual(results[0], results[1])
