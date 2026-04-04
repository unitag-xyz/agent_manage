import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent_manage.cli import main as agent_manage_main
from openclaw_remote_Deprecated.cli import main as openclaw_remote_main
from openclaw_remote_Deprecated.response import (
    TYPE_CODE_INVALID_ARGUMENT,
    TYPE_CODE_NOT_FOUND,
    TYPE_CODE_OPERATION_ROLLED_BACK,
    TYPE_CODE_SUCCESS,
)


class CliResponseTest(unittest.TestCase):
    def test_agent_manage_success_response_uses_result_envelope(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.create_instance.return_value = {
                "ok": True,
                "agent_name": "base",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["create-instance", "--template-name", "base"]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["typeCode"], TYPE_CODE_SUCCESS)
        self.assertEqual(payload["message"], "OK")
        self.assertEqual(payload["result"]["agent_name"], "base")
        self.assertIsNone(payload["error"])
        self.assertIn("serverTimeStamp", payload)

    def test_agent_manage_not_found_error_is_structured(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.create_instance.side_effect = FileNotFoundError(
                "Template archive not found: /tmp/base.zip"
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["create-instance", "--template-name", "base"]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["typeCode"], TYPE_CODE_NOT_FOUND)
        self.assertEqual(payload["error"]["code"], "TEMPLATE_ARCHIVE_NOT_FOUND")
        self.assertIsNone(payload["result"])

    def test_agent_manage_tg_bot_status_uses_result_envelope(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.get_tg_bot_status.return_value = {
                "ok": True,
                "tg_bot_count": 3,
                "bound_tg_bot_count": 2,
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["tg-bot-status"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["typeCode"], TYPE_CODE_SUCCESS)
        self.assertEqual(payload["result"]["bound_tg_bot_count"], 2)

    def test_agent_manage_delete_tg_bot_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.delete_tg_bot.return_value = {
                "ok": True,
                "deleted_bot_name": "publicbot",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["delete-tg-bot", "--bot-name", "publicbot"]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["deleted_bot_name"], "publicbot")

    def test_openclaw_remote_runtime_error_preserves_steps_and_rollback(self):
        with patch("openclaw_remote_Deprecated.cli.OpenClawManager") as manager_cls:
            manager_cls.return_value.create.side_effect = RuntimeError(
                json.dumps(
                    {
                        "error": "populate failed",
                        "details": {},
                        "steps": [{"step": "agents.add"}],
                        "rollback": [
                            {
                                "step": "rollback.workspace.purge",
                                "result": {"deleted": True},
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = openclaw_remote_main(["create", "--tg-id", "123456789"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["typeCode"], TYPE_CODE_OPERATION_ROLLED_BACK)
        self.assertEqual(payload["error"]["code"], "OPERATION_FAILED_WITH_ROLLBACK")
        self.assertEqual(payload["error"]["steps"], [{"step": "agents.add"}])
        self.assertEqual(
            payload["error"]["rollback"],
            [{"step": "rollback.workspace.purge", "result": {"deleted": True}}],
        )

    def test_openclaw_remote_invalid_args_return_json_error(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = openclaw_remote_main(["create"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["typeCode"], TYPE_CODE_INVALID_ARGUMENT)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(payload["error"]["details"]["kind"], "argument")


if __name__ == "__main__":
    unittest.main()
