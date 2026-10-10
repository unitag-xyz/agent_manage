import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_manage.cli import main
from agent_manage.local import LocalRunner
from agent_manage.models import CreateInstanceRequest
from agent_manage.orchestrator import InstanceManagerV2


FLY_ENV = {"UNITAG_AGENT_MANAGER_RUNTIME": "container",
           "UNITAG_AGENT_MANAGER_ACTIVATION_MODE": "fly"}


class ActivateInstanceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "data" / "team"
        self.workspace.mkdir(parents=True)
        (self.workspace / "AGENTS.md").write_text("user policy\n", encoding="utf-8")
        self.config_path = self.root / "openclaw.json"
        self.config_path.write_text(json.dumps({
            "agents": {"list": [{"id": "team", "workspace": str(self.workspace)}]},
            "gateway": {"auth": {"mode": "token", "token": "existing-gateway-token"}},
            "custom": {"keep": True},
        }) + "\n", encoding="utf-8")
        self.original = self.config_path.read_bytes()
        self.backup = self.root / "openclaw.json.bak"
        self.backup.write_bytes(b"previous backup")
        environment = patch.dict(os.environ, {**FLY_ENV, "OPENCLAW_GATEWAY_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.runner = LocalRunner()
        quiet_logs = patch.object(self.runner, "log")
        quiet_logs.start()
        self.addCleanup(quiet_logs.stop)
        no_commands = patch.object(self.runner, "run", side_effect=AssertionError("No runtime commands allowed"))
        no_commands.start()
        self.addCleanup(no_commands.stop)
        self.manager = InstanceManagerV2(self.runner, config_path=str(self.config_path))
        self.request = CreateInstanceRequest(model_key="model-secret")
        catalog = patch.object(InstanceManagerV2, "_fetch_supported_gateway_models", return_value={
            "models": [{"id": "gpt-5.4", "model_ref": "unipay-fun/gpt-5.4",
                        "definition": {"id": "gpt-5.4", "name": "GPT"}}],
            "model_count": 1,
            "primary_model": "unipay-fun/gpt-5.4",
        })
        self.catalog = catalog.start()
        self.addCleanup(catalog.stop)

    def assert_clean(self):
        self.assertEqual(list(self.root.glob(".activation-*")), [])
        lock = self.root / ".openclaw.json.activation.lock"
        if lock.exists():
            with self.manager._activation_lock(lock):
                pass
        self.assertEqual(self.manager.config_path, self.config_path)

    def test_atomic_single_publication_backup_hash_and_configure_data(self):
        real_replace = os.replace
        publications = []

        def observe(source, destination):
            if Path(destination) == self.config_path:
                self.assertEqual(self.config_path.read_bytes(), self.original)
                staged = json.loads(Path(source).read_text(encoding="utf-8"))
                self.assertIn("models", staged)
                self.assertEqual(staged["tools"]["profile"], "coding")
                self.assertIn("Runtime rules", (self.workspace / "AGENTS.md").read_text(encoding="utf-8"))
                publications.append(destination)
            return real_replace(source, destination)

        with patch("agent_manage.activation.os.replace", side_effect=observe):
            result = self.manager.activate_instance(self.request)
        self.assertEqual(len(publications), 1)
        self.assertTrue(result["activationRequired"])
        self.assertEqual(result["requestedConfigSha256"], hashlib.sha256(self.config_path.read_bytes()).hexdigest())
        self.assertEqual(result["mode"], "configured")
        self.assertEqual(result["agent_names"], ["team"])
        self.assertEqual(result["gateway_token"], "existing-gateway-token")
        self.assertEqual(result["config_path"], str(self.config_path))
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertTrue(json.loads(self.config_path.read_bytes())["custom"]["keep"])
        self.assertFalse(self.manager.restart_required)
        self.assertNotIn("model-secret", json.dumps(result))
        self.assertNotIn(".activation-", json.dumps(result))
        self.assertEqual(result["steps"][-1]["step"], "activation.commit")
        self.assertTrue(all(step["elapsed_ms"] >= 0 for step in result["steps"]))
        self.assertGreaterEqual(result["total_elapsed_ms"], 0)
        self.assert_clean()

    def test_guard_rejects_all_non_explicit_fly_modes_without_io(self):
        for runtime, activation in [("", ""), ("vps", "fly"), ("container", ""),
                                    ("container", "container"), ("fly", "fly"),
                                    ("container", "Fly")]:
            with self.subTest(runtime=runtime, activation=activation), patch.dict(os.environ, {
                "UNITAG_AGENT_MANAGER_RUNTIME": runtime,
                "UNITAG_AGENT_MANAGER_ACTIVATION_MODE": activation,
            }), self.assertRaisesRegex(ValueError, "requires"):
                self.manager.activate_instance(self.request)
        self.catalog.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assert_clean()

    def test_method_rejects_template_zip_local_inputs(self):
        for fields in [{"template_name": "template"}, {"agent_zip": "missing.zip"}, {"local": True}, {"model": "override"}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.manager.activate_instance(replace(self.request, **fields))
        self.catalog.assert_not_called()

    def test_empty_key_and_missing_prebuilt_agents_fail_without_publication(self):
        with self.assertRaisesRegex(RuntimeError, "model_key is required"):
            self.manager.activate_instance(replace(self.request, model_key=" "))
        self.config_path.write_text('{"agents": {"list": []}}', encoding="utf-8")
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "No prebuilt agents"):
            self.manager.activate_instance(self.request)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assert_clean()

    def test_workspace_failure_discards_staged_config_even_without_rollback_flag(self):
        with patch.object(InstanceManagerV2, "_configure_workspace_defaults", side_effect=OSError("workspace failure")):
            with self.assertRaisesRegex(RuntimeError, "workspace failure") as raised:
                self.manager.activate_instance(replace(self.request, rollback_on_fail=False))
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assertFalse(json.loads(str(raised.exception))["rollback"][0]["result"]["config_published"])
        self.assert_clean()

    def test_real_missing_workspace_fails_without_publication(self):
        (self.workspace / "AGENTS.md").unlink()
        self.workspace.rmdir()
        with self.assertRaisesRegex(RuntimeError, "Workspace not found"):
            self.manager.activate_instance(self.request)
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assert_clean()

    def test_staged_config_write_failure_does_not_touch_final_or_backup(self):
        with patch.object(InstanceManagerV2, "_configure_config_tools", side_effect=OSError("tools failure")):
            with self.assertRaisesRegex(RuntimeError, "tools failure"):
                self.manager.activate_instance(self.request)
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assert_clean()

    def test_final_replace_failure_restores_previous_backup(self):
        real_replace = os.replace

        def fail(source, destination):
            if Path(destination) == self.config_path:
                raise OSError("publish failure")
            return real_replace(source, destination)

        with patch("agent_manage.activation.os.replace", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "publish failure"):
                self.manager.activate_instance(self.request)
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assert_clean()

    def test_final_replace_failure_removes_new_backup_when_none_existed(self):
        self.backup.unlink()
        real_replace = os.replace

        def fail(source, destination):
            if Path(destination) == self.config_path:
                raise OSError("publish failure")
            return real_replace(source, destination)

        with patch("agent_manage.activation.os.replace", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.manager.activate_instance(self.request)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assert_clean()

    def test_concurrent_config_write_is_preserved_not_rolled_back(self):
        defaults = InstanceManagerV2._configure_workspace_defaults

        def concurrent(manager, *args, **kwargs):
            result = defaults(manager, *args, **kwargs)
            self.config_path.write_bytes(b'{"concurrent": true}\n')
            return result

        with patch.object(InstanceManagerV2, "_configure_workspace_defaults", concurrent):
            with self.assertRaisesRegex(RuntimeError, "Concurrent configuration write"):
                self.manager.activate_instance(self.request)
        self.assertEqual(self.config_path.read_bytes(), b'{"concurrent": true}\n')
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assert_clean()

    def test_second_snapshot_check_detects_writer_after_backup(self):
        real_replace = os.replace

        def concurrent(source, destination):
            result = real_replace(source, destination)
            if Path(destination) == self.backup:
                self.config_path.write_bytes(b'{"concurrent": true}\n')
            return result

        with patch("agent_manage.activation.os.replace", side_effect=concurrent):
            with self.assertRaisesRegex(RuntimeError, "Concurrent configuration write"):
                self.manager.activate_instance(self.request)
        self.assertEqual(self.config_path.read_bytes(), b'{"concurrent": true}\n')
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assert_clean()

    def test_existing_activation_lock_is_not_removed(self):
        lock = self.root / ".openclaw.json.activation.lock"
        lock.write_bytes(b"other owner")
        lock.chmod(0o600)
        with self.manager._activation_lock(lock):
            with self.assertRaisesRegex(RuntimeError, "lock is busy"):
                self.manager.activate_instance(self.request)
        self.assertEqual(lock.read_bytes(), b"other owner")
        self.catalog.assert_not_called()

    def test_dry_run_never_writes_or_requests_activation(self):
        self.runner.dry_run = True
        result = self.manager.activate_instance(self.request)
        self.assertFalse(result["activationRequired"])
        self.assertIsNone(result["requestedConfigSha256"])
        self.assertTrue(result["dryRun"])
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assertEqual((self.workspace / "AGENTS.md").read_text(encoding="utf-8"), "user policy\n")
        self.assertFalse((self.root / "skills").exists())
        self.assert_clean()

    def test_retry_preserves_gateway_token_and_deterministic_hash(self):
        first = self.manager.activate_instance(self.request)
        second = self.manager.activate_instance(self.request)
        self.assertEqual(first["requestedConfigSha256"], second["requestedConfigSha256"])
        self.assertEqual(first["gateway_token"], second["gateway_token"])
        self.assertEqual((self.workspace / "AGENTS.md").read_text(encoding="utf-8").count(self.manager.RUNTIME_POLICY_START), 1)
        self.assert_clean()

    def run_cli(self, arguments, stdin=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        return exit_code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_cli_stdin_dispatch_real_transaction_and_secret_safety(self):
        with patch.object(InstanceManagerV2, "CONTAINER_OPENCLAW_ROOT", str(self.root)):
            code, payload, logs = self.run_cli(["activate-instance", "--model-key-stdin"], "model-secret\r\n")
        self.assertEqual(code, 0)
        self.assertTrue(payload["activationRequired"])
        self.assertNotIn("activationRequired", payload["result"])
        self.assertNotIn("restart_required", payload["result"])
        self.assertFalse(payload["restartRequired"])
        self.assertEqual(payload["result"]["requestedConfigSha256"], hashlib.sha256(self.config_path.read_bytes()).hexdigest())
        self.assertEqual(json.loads(self.config_path.read_bytes())["models"]["providers"]["unipay-fun"]["apiKey"], "model-secret")
        self.assertNotIn("model-secret", json.dumps(payload) + logs)

    def test_cli_guard_runs_before_stdin_read_and_client_construction(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_ACTIVATION_MODE": ""}), patch("sys.stdin") as stdin, \
                patch.object(InstanceManagerV2, "__init__", side_effect=AssertionError("Must guard first")) as construct, \
                redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(["activate-instance", "--model-key-stdin"])
            stdin.read.assert_not_called()
            construct.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertIn("requires", payload["message"])

    def test_cli_rejects_zip_local_template_path_overrides_and_no_rollback(self):
        for argument in ["--agent-zip=x.zip", "--template-name=base", "--local", "--no-rollback",
                         "--model=override", "--workspace-root=/tmp", "--config-path=/tmp/openclaw.json", "--openclaw-bin=other"]:
            with self.subTest(argument=argument):
                code, payload, _ = self.run_cli(["activate-instance", "--model-key-stdin", argument])
                self.assertEqual(code, 1)
                self.assertIsNone(payload["result"])
        self.catalog.assert_not_called()

    def test_cli_empty_stdin_fails_without_io(self):
        code, payload, _ = self.run_cli(["activate-instance", "--model-key-stdin"], "\n")
        self.assertEqual(code, 1)
        self.assertIn("must not be empty", payload["message"])
        self.catalog.assert_not_called()

    def test_cli_redacts_trimmed_secret_in_errors_and_logs(self):
        with patch.object(InstanceManagerV2, "CONTAINER_OPENCLAW_ROOT", str(self.root)), patch.object(
            InstanceManagerV2, "_configure_workspace_defaults", side_effect=OSError('failed model-secret "quoted"')
        ):
            code, payload, logs = self.run_cli(["activate-instance", "--model-key-stdin"], "  model-secret  \n")
        self.assertEqual(code, 1)
        self.assertNotIn("model-secret", json.dumps(payload) + logs)
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assert_clean()

    def test_cli_rejects_plaintext_key_input(self):
        code, payload, _ = self.run_cli(["activate-instance", "--model-key", "model-secret"])
        self.assertEqual(code, 1)
        self.assertIsNone(payload["result"])
        self.catalog.assert_not_called()

    def test_cli_requires_exactly_one_key_input(self):
        for args in [["activate-instance"], ["activate-instance", "--model-key", "model-secret", "--model-key-stdin"]]:
            with self.subTest(args=args):
                code, payload, logs = self.run_cli(args)
                self.assertEqual(code, 1)
                self.assertIsNone(payload["result"])
        self.catalog.assert_not_called()

    def test_backup_stage_failure_leaves_original_backup_and_config(self):
        real_stage = self.manager._activation_stage

        def fail(path, data):
            if path == self.backup:
                raise OSError("backup failure")
            return real_stage(path, data)

        with patch.object(self.manager, "_activation_stage", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "backup failure"):
                self.manager.activate_instance(self.request)
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b"previous backup")
        self.assert_clean()

    def test_secret_with_quotes_is_redacted_without_breaking_error_json(self):
        secret = 'secret-"quoted"'
        with patch.object(InstanceManagerV2, "_configure_workspace_defaults", side_effect=OSError(f"failed {secret}")):
            with self.assertRaises(RuntimeError) as raised:
                self.manager.activate_instance(replace(self.request, model_key=secret))
        payload = json.loads(str(raised.exception))
        self.assertEqual(payload["error"], "failed [REDACTED]")
        self.assertNotIn(secret, json.dumps(payload))
        self.assert_clean()

    def test_cli_short_secret_cannot_corrupt_requested_hash(self):
        with patch.object(InstanceManagerV2, "CONTAINER_OPENCLAW_ROOT", str(self.root)):
            code, payload, _ = self.run_cli(["activate-instance", "--model-key-stdin"], "a\n")
        self.assertEqual(code, 0)
        self.assertTrue(payload["activationRequired"])
        digest = payload["result"]["requestedConfigSha256"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, hashlib.sha256(self.config_path.read_bytes()).hexdigest())
        self.assertEqual(payload["result"]["config_path"], str(self.config_path))
        self.assertEqual(payload["result"]["agent_names"], ["team"])

    def test_cli_dry_run_marker_is_root_false(self):
        with patch.object(InstanceManagerV2, "CONTAINER_OPENCLAW_ROOT", str(self.root)):
            code, payload, _ = self.run_cli(["--dry-run", "activate-instance", "--model-key-stdin"], "model-secret\n")
        self.assertEqual(code, 0)
        self.assertFalse(payload["activationRequired"])
        self.assertNotIn("activationRequired", payload["result"])
        self.assertIsNone(payload["result"]["requestedConfigSha256"])
        self.assertEqual(self.config_path.read_bytes(), self.original)

    @unittest.skipIf(os.name == "nt", "POSIX file modes require Linux")
    def test_staged_final_and_backup_files_are_private(self):
        real_stage = self.manager._activation_stage

        def private_stage(path, data):
            staged = real_stage(path, data)
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            return staged

        with patch.object(self.manager, "_activation_stage", side_effect=private_stage):
            self.manager.activate_instance(self.request)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
