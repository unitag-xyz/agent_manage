import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from agent_manage import __version__
from agent_manage.cli import main as agent_manage_main
from agent_manage.models import AddAgentsRequest
from agent_manage.response import (
    MAX_PROTOCOL_RESPONSE_BYTES,
    TYPE_CODE_INVALID_ARGUMENT,
    TYPE_CODE_NOT_FOUND,
    TYPE_CODE_OUTPUT_TOO_LARGE,
    TYPE_CODE_SUCCESS,
    build_success_response,
    build_error_response,
    print_json,
)


class CliResponseTest(unittest.TestCase):
    def test_agent_manage_add_agent_dispatches_template_registration(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_agent.return_value = {"ok": True}
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "add-agent",
                        "--template-name",
                        "team",
                        "--agent-name",
                        "primary",
                        "--workspace-root",
                        "/data",
                    ]
                )

        self.assertEqual(exit_code, 0)
        manager_cls.return_value.add_agent.assert_called_once_with(
            template_name="team",
            agent_name="primary",
            workspace_root="/data",
            model=None,
            base_url=None,
            template_zip_url=None,
            template_zip_sha256=None,
        )

    def test_agent_manage_add_agent_accepts_base_url(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_agent.return_value = {"ok": True}
            with redirect_stdout(io.StringIO()):
                exit_code = agent_manage_main(
                    [
                        "add-agent",
                        "--template-name",
                        "team",
                        "--base-url",
                        "https://server-001.web.dolaio.cn/",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            manager_cls.return_value.add_agent.call_args.kwargs["base_url"],
            "https://server-001.web.dolaio.cn/",
        )

    def test_agent_manage_add_agent_accepts_template_archive_source(self):
        digest = "a" * 64
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_agent.return_value = {"ok": True}
            with redirect_stdout(io.StringIO()):
                exit_code = agent_manage_main(
                    [
                        "add-agent",
                        "--template-name",
                        "team",
                        "--template-zip-url",
                        "https://assets.example.test/team.zip",
                        "--template-zip-sha256",
                        digest,
                    ]
                )

        self.assertEqual(exit_code, 0)
        kwargs = manager_cls.return_value.add_agent.call_args.kwargs
        self.assertEqual(kwargs["template_zip_url"], "https://assets.example.test/team.zip")
        self.assertEqual(kwargs["template_zip_sha256"], digest)

    def test_agent_manage_add_agent_surfaces_restart_required(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_agent.return_value = {
                "ok": True,
                "restart_required": True,
            }
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["add-agent", "--template-name", "team"]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["restartRequired"])

    def test_agent_manage_configure_instance_dispatches_without_create(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.configure_instance.return_value = {
                "ok": True,
                "restart_required": True,
            }
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "configure-instance",
                        "--model-key",
                        "runtime-secret",
                        "--base-url",
                        "https://server-001.web.dolaio.cn/",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["restartRequired"])
        manager_cls.return_value.configure_instance.assert_called_once()
        request = manager_cls.return_value.configure_instance.call_args.args[0]
        self.assertIsNone(request.template_name)
        self.assertEqual(request.base_url, "https://server-001.web.dolaio.cn/")
        manager_cls.return_value.create_instance.assert_not_called()

    def test_embedded_failure_response_preserves_total_elapsed_ms(self):
        response = build_error_response(
            RuntimeError(json.dumps({"error": "failed", "total_elapsed_ms": 12.3}))
        )

        self.assertEqual(response["error"]["total_elapsed_ms"], 12.3)

    def test_oversized_final_json_returns_small_redacted_error_envelope(self):
        secret = "never-leak-this-secret"
        stdout = io.StringIO()
        stderr = io.StringIO()
        oversized = build_success_response({"payload": secret + ("x" * MAX_PROTOCOL_RESPONSE_BYTES)})
        original_utf8_bytes = len((json.dumps(oversized, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

        with redirect_stdout(stdout), redirect_stderr(stderr):
            print_json(oversized)

        output = stdout.getvalue()
        diagnostics = stderr.getvalue()
        payload = json.loads(output)
        self.assertLess(len(output.encode("utf-8")), 8_192)
        self.assertEqual(payload["typeCode"], TYPE_CODE_OUTPUT_TOO_LARGE)
        self.assertEqual(payload["error"]["code"], "RESPONSE_OUTPUT_TOO_LARGE")
        self.assertEqual(payload["error"]["details"]["totalUtf8Bytes"], original_utf8_bytes)
        self.assertNotIn(secret, output)
        self.assertNotIn(secret, diagnostics)
        self.assertEqual(
            diagnostics,
            "[agentctl response] "
            f"original_utf8_bytes={original_utf8_bytes} "
            f"emitted_utf8_bytes={len(output.encode('utf-8'))} "
            "budget_applied=true\n",
        )

    def test_small_final_json_reports_exact_emitted_byte_count(self):
        response = build_success_response({"message": "猫"})
        expected_utf8_bytes = len((json.dumps(response, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            print_json(response)

        self.assertEqual(json.loads(stdout.getvalue()), response)
        self.assertEqual(
            stderr.getvalue(),
            "[agentctl response] "
            f"original_utf8_bytes={expected_utf8_bytes} "
            f"emitted_utf8_bytes={expected_utf8_bytes} "
            "budget_applied=false\n",
        )

    def test_wire_budget_includes_final_lf(self):
        for extra in (0, 1):
            with self.subTest(extra=extra):
                raw = io.BytesIO()
                with io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n") as stdout:
                    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                        # JSON adds two ASCII quotes, stdout adds one LF.
                        print_json("x" * (MAX_PROTOCOL_RESPONSE_BYTES - 3 + extra))
                    stdout.flush()
                    wire = raw.getvalue()
                if not extra:
                    self.assertEqual(len(wire), MAX_PROTOCOL_RESPONSE_BYTES)
                    self.assertTrue(wire.endswith(b'"\n'))
                else:
                    error = json.loads(wire)["error"]
                    self.assertEqual(error, {"code": "RESPONSE_OUTPUT_TOO_LARGE",
                                             "details": {"totalUtf8Bytes": MAX_PROTOCOL_RESPONSE_BYTES + 1}})

    def test_agent_manage_version_uses_package_version(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(stdout):
            agent_manage_main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"agent-manage {__version__}")

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
        request = manager_cls.return_value.create_instance.call_args.args[0]
        self.assertEqual(request.model_env, "global")
        self.assertEqual(request.ai_shop, "shop")

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

    def test_agent_manage_create_instance_accepts_cn_model_env(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.create_instance.return_value = {
                "ok": True,
                "agent_name": "base",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "create-instance",
                        "--template-name",
                        "base",
                        "--model-key",
                        "test-key",
                        "--model-env",
                        "cn",
                    ]
                )

        request = manager_cls.return_value.create_instance.call_args.args[0]
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(request.model_env, "cn")
        self.assertEqual(payload["result"]["agent_name"], "base")

    def test_agent_manage_create_instance_accepts_ai_shop(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.create_instance.return_value = {
                "ok": True,
                "agent_name": "base",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "create-instance",
                        "--template-name",
                        "base",
                        "--model-key",
                        "test-key",
                        "--ai-shop",
                        "shop",
                    ]
                )

        request = manager_cls.return_value.create_instance.call_args.args[0]
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(request.ai_shop, "shop")
        self.assertEqual(payload["result"]["agent_name"], "base")

    def test_agent_manage_create_instance_accepts_image_quality_default(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.create_instance.return_value = {
                "ok": True,
                "agent_name": "base",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "create-instance",
                        "--template-name",
                        "base",
                        "--model-key",
                        "test-key",
                        "--image-quality",
                        "low",
                    ]
                )

        request = manager_cls.return_value.create_instance.call_args.args[0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(request.image_quality, "low")

    def test_agent_manage_create_instance_accepts_base_url(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.create_instance.return_value = {
                "ok": True,
                "agent_name": "base",
                "restart_required": True,
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "create-instance",
                        "--template-name",
                        "base",
                        "--model-key",
                        "test-key",
                        "--base-url",
                        "https://server-001.web.dolaio.cn/",
                    ]
                )

        request = manager_cls.return_value.create_instance.call_args.args[0]
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(request.base_url, "https://server-001.web.dolaio.cn/")
        self.assertTrue(payload["restartRequired"])

    def test_agent_manage_create_instance_accepts_local_agent_zip(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.create_instance.return_value = {
                "ok": True,
                "agent_name": "base",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "create-instance",
                        "--local",
                        "--agent-zip",
                        "/tmp/base.zip",
                        "--model-key",
                        "test-key",
                        "--model",
                        "unipay-fun/gpt-5.4",
                    ]
                )

        request = manager_cls.return_value.create_instance.call_args.args[0]
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(request.local)
        self.assertEqual(request.agent_zip, "/tmp/base.zip")
        self.assertEqual(request.workspace_root, "~/.openclaw/data")
        self.assertEqual(request.model, "unipay-fun/gpt-5.4")
        self.assertEqual(payload["result"]["agent_name"], "base")

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

    def test_agent_manage_add_agents_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_agents.return_value = {
                "ok": True,
                "added_count": 2,
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "add-agents",
                        "--agents",
                        '[\"base\", {\"agent_name\": \"demo\", \"model\": \"openai/gpt-5\"}]',
                        "--base-url",
                        "https://server-001.web.dolaio.cn/",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        request = manager_cls.return_value.add_agents.call_args.args[0]
        self.assertEqual(exit_code, 0)
        self.assertIsInstance(request, AddAgentsRequest)
        self.assertEqual(request.agents[0].agent_name, "base")
        self.assertEqual(request.agents[1].agent_name, "demo")
        self.assertEqual(request.agents[1].model, "openai/gpt-5")
        self.assertEqual(request.base_url, "https://server-001.web.dolaio.cn/")
        self.assertEqual(payload["result"]["added_count"], 2)

    def test_agent_manage_add_agents_parses_optional_template_name(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_agents.return_value = {
                "ok": True,
                "added_count": 1,
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "add-agents",
                        "--agents",
                        '[{"agent_name":"demo","template_name":"demo-template",'
                        '"template_zip_url":"https://assets.example.test/demo.zip",'
                        '"template_zip_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]',
                    ]
                )

        request = manager_cls.return_value.add_agents.call_args.args[0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(request.agents[0].template_name, "demo-template")
        self.assertEqual(
            request.agents[0].template_zip_url,
            "https://assets.example.test/demo.zip",
        )
        self.assertEqual(request.agents[0].template_zip_sha256, "a" * 64)

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

    def test_agent_manage_feishu_bot_status_uses_result_envelope(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.get_feishu_bot_status.return_value = {
                "ok": True,
                "feishu_bot_count": 2,
                "bound_feishu_bot_count": 1,
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["feishu-bot-status"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["feishu_bot_count"], 2)

    def test_agent_manage_add_feishu_bot_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_feishu_bot.return_value = {
                "ok": True,
                "account_id": "main",
            }

            stdout = io.StringIO()
            with patch("sys.stdin", io.StringIO("secret\n")), redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "add-feishu-bot",
                        "--agent",
                        "unipay-claw-base",
                        "--domain",
                        "lark",
                        "--account-id",
                        "main",
                        "--app-id",
                        "cli_123",
                        "--app-secret-stdin",
                        "--bot-name",
                        "Lark Bot",
                        "--bind-lark-cli",
                    ]
                )

        request = manager_cls.return_value.add_feishu_bot.call_args.args[0]
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["account_id"], "main")
        self.assertEqual(request.domain, "lark")
        self.assertEqual(request.app_secret, "secret")
        self.assertTrue(request.bind_lark_cli)

    def test_agent_manage_delete_feishu_bot_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.delete_feishu_bot.return_value = {
                "ok": True,
                "deleted_account_id": "main",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    [
                        "delete-feishu-bot",
                        "--account-id",
                        "main",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["deleted_account_id"], "main")

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

    def test_container_mode_reads_model_key_from_stdin_and_strips_one_newline(self):
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container"}):
            with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
                manager_cls.return_value.create_instance.return_value = {"ok": True}
                manager_cls.return_value.restart_required = False
                stdout = io.StringIO()
                with patch("sys.stdin", io.StringIO("model-secret\r\n")), redirect_stdout(stdout):
                    exit_code = agent_manage_main(
                        ["create-instance", "--template-name", "base", "--model-key-stdin"]
                    )

        request = manager_cls.return_value.create_instance.call_args.args[0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(request.model_key, "model-secret")
        self.assertFalse(json.loads(stdout.getvalue())["restartRequired"])

    def test_container_mode_uses_persistent_workspace_root_for_create_instance(self):
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container"}):
            with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
                manager_cls.CONTAINER_WORKSPACE_ROOT = "/home/node/.openclaw/data"
                manager_cls.return_value.create_instance.return_value = {"ok": True}
                manager_cls.return_value.restart_required = False
                with redirect_stdout(io.StringIO()):
                    agent_manage_main(
                        ["create-instance", "--template-name", "base", "--model-key", "key"]
                    )

        request = manager_cls.return_value.create_instance.call_args.args[0]
        self.assertEqual(request.workspace_root, "/home/node/.openclaw/data")

    def test_container_mode_reads_tg_and_weixin_tokens_from_stdin(self):
        for command, arguments, method, attribute in (
            ("add-tg-bot", ["--agent", "main", "--tg-token-stdin"], "add_tg_bot", "bot_token"),
            (
                "add-weixin-bot",
                ["--agent", "main", "--ilink-bot-id", "bot@im.bot", "--bot-token-stdin"],
                "add_weixin_bot",
                "bot_token",
            ),
        ):
            with self.subTest(command=command), patch.dict(
                os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container"}
            ):
                with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
                    getattr(manager_cls.return_value, method).return_value = {"ok": True}
                    manager_cls.return_value.restart_required = False
                    with patch("sys.stdin", io.StringIO("token\n")), redirect_stdout(io.StringIO()):
                        exit_code = agent_manage_main([command, *arguments])

            self.assertEqual(exit_code, 0)
            request = getattr(manager_cls.return_value, method).call_args.args[0]
            self.assertEqual(getattr(request, attribute), "token")

    def test_container_mode_rejects_global_path_overrides(self):
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container"}):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["--config-path", "/tmp/unsafe.json", "current-model"]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["typeCode"], TYPE_CODE_INVALID_ARGUMENT)
        self.assertNotIn("unsafe.json", payload["message"])

    def test_secret_is_redacted_from_error_output(self):
        secret = "never-print-this-secret"
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.add_tg_bot.side_effect = ValueError(f"failed: {secret}")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["add-tg-bot", "--agent", "main", "--tg-token", secret]
                )

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, stdout.getvalue())
        self.assertIn("[REDACTED]", stdout.getvalue())

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
                "model_ref": "unipay-fun/gpt-5.4",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(
                    ["set-model", "--model", "unipay-fun/gpt-5.4"]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["model_ref"], "unipay-fun/gpt-5.4")

    def test_agent_manage_set_model_rejects_unknown_choice(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.set_model.side_effect = ValueError(
                "Unsupported model 'gpt-4o'. Allowed: unipay-fun/gpt-5.4"
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["set-model", "--model", "gpt-4o"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["typeCode"], TYPE_CODE_INVALID_ARGUMENT)
        self.assertEqual(payload["error"]["code"], "VALIDATION_ERROR")

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
                "current_model": "unipay-fun/gpt-5.4-nano",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["current-model"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["current_model"], "unipay-fun/gpt-5.4-nano")

    def test_agent_manage_models_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.get_supported_models.return_value = {
                "ok": True,
                "supported_model_refs": [
                    "unipay-fun/gpt-5.4-nano",
                    "unipay-fun/claude-sonnet-4-6",
                ],
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["models"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["result"]["supported_model_refs"],
            ["unipay-fun/gpt-5.4-nano", "unipay-fun/claude-sonnet-4-6"],
        )

    def test_agent_manage_update_model_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.update_model_catalog.return_value = {
                "ok": True,
                "current_model_after": "unipay-fun/gpt-5.4",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["update-model"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["current_model_after"], "unipay-fun/gpt-5.4")

    def test_agent_manage_current_gateway_token_dispatches_correctly(self):
        with patch("agent_manage.cli.InstanceManagerV2") as manager_cls:
            manager_cls.return_value.get_current_gateway_token.return_value = {
                "ok": True,
                "gateway_auth_mode": "token",
                "gateway_token": "test-gateway-token",
            }

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agent_manage_main(["current-gateway-token"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result"]["gateway_auth_mode"], "token")
        self.assertEqual(payload["result"]["gateway_token"], "test-gateway-token")

if __name__ == "__main__":
    unittest.main()
