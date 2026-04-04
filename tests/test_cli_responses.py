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
                    ["create-instance", "--template-name", "base", "--model-key", "test-key"]
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
                    ["create-instance", "--template-name", "base", "--model-key", "test-key"]
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

    def test_agent_manage_weixin_bot_status_uses_result_envelope(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.get_weixin_bot_status.return_value = {
                "ok": True,
                "weixin_bot_count": 2,
                "bound_weixin_bot_count": 1,
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["weixin-bot-status"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["weixin_bot_count"], 2)

    def test_agent_manage_add_weixin_bot_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_weixin_bot.return_value = {
                "ok": True,
                "account_id": "b0f5860fdecb-im-bot",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "add-weixin-bot",
                        "--agent",
                        "unipay-claw-base",
                        "--ilink-bot-id",
                        "B0F5860FDECB@im.bot",
                        "--bot-token",
                        "wx-token",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["account_id"], "b0f5860fdecb-im-bot")

    def test_agent_manage_delete_weixin_bot_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.delete_weixin_bot.return_value = {
                "ok": True,
                "deleted_account_id": "caf8d0cd98a9-im-bot",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "delete-weixin-bot",
                        "--ilink-bot-id",
                        "caf8d0cd98a9@im.bot",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["deleted_account_id"], "caf8d0cd98a9-im-bot")

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

    def test_agent_manage_set_model_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.set_model.return_value = {
                "ok": True,
                "model_name": "gpt-5.4",
                "model_ref": "unipay-fun/gpt-5.4",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["set-model", "--model", "gpt-5.4"]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["model_ref"], "unipay-fun/gpt-5.4")

    def test_agent_manage_set_model_rejects_unknown_choice(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = agent_manage_main(["set-model", "--model", "gpt-4o"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["typeCode"], TYPE_CODE_INVALID_ARGUMENT)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_agent_manage_agents_list_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.list_agents.return_value = {
                "ok": True,
                "agent_count": 1,
                "agents": [{"id": "unipay-claw-base"}],
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["agents-list"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["agent_count"], 1)
        self.assertEqual(payload["result"]["agents"][0]["id"], "unipay-claw-base")

    def test_agent_manage_current_model_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.get_current_model.return_value = {
                "ok": True,
                "current_model": "unipay-fun/gpt-4.1-mini",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["current-model"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["current_model"], "unipay-fun/gpt-4.1-mini")

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
