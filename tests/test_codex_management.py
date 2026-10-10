import io
import json
import os
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_manage.cli import main
from agent_manage.local import LocalRunner
from agent_manage.models import RefreshAgentRequest
from agent_manage.orchestrator import InstanceManagerV2
import test_refresh_management as refresh_tests


class CodexManagementTest(unittest.TestCase):
    def setUp(self):
        self.fixture = refresh_tests.RefreshManagementTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.path = self.fixture.config_path
        config = json.loads(self.path.read_text())
        for provider in config["models"]["providers"].values():
            for model in provider.get("models", []):
                model.setdefault("name", model["id"])
        config["models"]["providers"]["openai"] = {
            "api": "openai-completions", "apiKey": "test-image-key",
            "baseUrl": "https://api.dola.io/aigateway/mystore/v1",
            "models": [{"id": "gpt-image-2", "name": "Image"}],
        }
        config["agents"]["defaults"].update(utilityModel="dola/chat", heartbeat={"model": "dola/chat", "every": "30m"},
                                           subagents={"model": "dola/chat"})
        config["agents"]["list"].append({"id": "second", "model": "custom/mine",
                                         "utilityModel": "custom/mine", "models": {"custom/mine": {}},
                                         "heartbeat": {"model": "custom/mine"}})
        config["auth"] = {"profiles": {"openai:chatgpt": {"provider": "openai", "mode": "oauth"}},
                          "order": {"openai": ["openai:chatgpt"]}}
        self.path.write_text(json.dumps(config))
        self.original = config
        self.auth_file = self.path.parent / "agents/main/agent/auth-profiles.json"
        self.auth_file.parent.mkdir(parents=True)
        self.auth_file.write_text(json.dumps({"version": 1, "profiles": {"openai:chatgpt": {
            "type": "oauth", "provider": "openai", "access": "test-existing-access",
            "refresh": "test-existing-refresh", "expires": 4102444800000}}}))
        self.auth_bytes = self.auth_file.read_bytes()

    def login(self):
        return self.manager._codex_configure_models()

    def state(self):
        return json.loads(self.manager._codex_state_path().read_text())

    def test_installs_new_models_and_switches_all_chat_selectors_without_authentication(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No HTTP expected")), \
             patch("urllib.request.build_opener", side_effect=AssertionError("No HTTP expected")):
            result = self.login()
        config = json.loads(self.path.read_text())
        expected = {"openai/gpt-6.1-sol", "openai/gpt-6-astra", "openai/gpt-6-sol", "openai/gpt-6-luna"}
        self.assertEqual(set(result["supported_model_refs"]), expected)
        self.assertEqual(result["status"], "configured")
        self.assertEqual(result["mode"], "models_only")
        self.assertEqual(result["scope"], "environment")
        self.assertEqual(result["model"], "openai/gpt-6.1-sol")
        self.assertNotIn("login_id", result)
        self.assertNotIn("verification_url", result)
        self.assertNotIn("test-existing-access", json.dumps(result) + json.dumps(self.state()))
        defaults = config["agents"]["defaults"]
        self.assertEqual(defaults["utilityModel"], result["model"])
        self.assertEqual(defaults["heartbeat"], {"model": result["model"], "every": "30m"})
        self.assertEqual(defaults["subagents"]["model"], result["model"])
        for agent in config["agents"]["list"]:
            self.assertEqual(agent["model"], {"primary": result["model"], "fallbacks": []})
        self.assertEqual(set(config["agents"]["list"][1]["models"]), expected)
        self.assertEqual(config["auth"], self.original["auth"])
        self.assertEqual(self.auth_file.read_bytes(), self.auth_bytes)
        self.assertEqual(config["channels"], self.original["channels"])
        self.assertEqual(config["gateway"], self.original["gateway"])
        self.assertEqual([call[0][1:] for call in self.fixture.runner.calls], [["config", "validate"]])
        self.assertFalse(result["restart_required"])

    def test_image_uses_codex_and_audio_keeps_original_api_routing(self):
        config = json.loads(self.path.read_text())
        config["tools"]["media"] = {"audio": {"models": [{"provider": "openai", "model": "audio-model"}]}}
        self.path.write_text(json.dumps(config))
        result = self.login()
        changed = json.loads(self.path.read_text())
        provider = changed["models"]["providers"]["openai"]
        self.assertEqual(provider["api"], self.manager.CODEX_API)
        self.assertEqual(provider["baseUrl"], self.manager.CODEX_BASE_URL)
        self.assertEqual(provider["apiKey"], "test-image-key")
        self.assertEqual(provider["models"][0]["baseUrl"], config["models"]["providers"]["openai"]["baseUrl"])
        self.assertEqual(provider["models"][0]["api"], "openai-completions")
        for row in provider["models"][1:]:
            self.assertEqual(row["baseUrl"], self.manager.CODEX_BASE_URL)
            self.assertEqual(row["api"], self.manager.CODEX_API)
            self.assertNotIn("apiKey", row)
        self.assertEqual(changed["agents"]["defaults"]["imageGenerationModel"]["primary"], "openai/gpt-image-2")
        self.assertEqual(changed["agents"]["defaults"]["imageGenerationModel"]["fallbacks"], [])
        self.assertEqual(changed["tools"]["media"]["audio"]["models"], [
            {"provider": "openai", "model": "audio-model", "baseUrl": config["models"]["providers"]["openai"]["baseUrl"]}])
        self.assertTrue(result["media"]["image_generation_switched"])
        self.assertFalse(result["media"]["audio_switched"])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_media_switch_restores_other_image_and_preserves_non_openai_audio(self):
        config = json.loads(self.path.read_text())
        config["agents"]["defaults"]["imageGenerationModel"] = {
            "primary": "google/other-image", "fallbacks": ["custom/image"], "timeoutMs": 240000}
        config["tools"]["media"] = {"audio": {"enabled": True, "models": [
            {"provider": "deepgram", "model": "nova-3"},
            {"provider": "openai", "model": "gpt-4o-transcribe", "baseUrl": "https://audio.example/v1"}]}}
        self.path.write_text(json.dumps(config))
        self.login()
        changed = json.loads(self.path.read_text())
        self.assertEqual(changed["agents"]["defaults"]["imageGenerationModel"], {
            "primary": "openai/gpt-image-2", "fallbacks": [], "timeoutMs": 240000})
        self.assertEqual(changed["tools"]["media"], config["tools"]["media"])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_legacy_active_login_upgrades_media_without_changing_original_backup(self):
        self.login()
        state = self.state()
        state["restore"].pop("media")
        state["provider_mode"] = "mixed_api_and_codex"
        self.manager._codex_state_path().write_text(json.dumps(state))
        config = json.loads(self.path.read_text())
        config["models"]["providers"]["openai"]["baseUrl"] = self.original["models"]["providers"]["openai"]["baseUrl"]
        config["models"]["providers"]["openai"]["api"] = "openai-completions"
        config["models"]["providers"]["openai"]["models"][0].pop("baseUrl")
        config["models"]["providers"]["openai"]["models"][0].pop("api")
        config["tools"].pop("media")
        config["agents"]["defaults"]["imageGenerationModel"] = self.original["agents"]["defaults"]["imageGenerationModel"]
        self.path.write_text(json.dumps(config))
        result = self.login()
        self.assertTrue(result["media"]["image_generation_switched"])
        self.assertEqual(self.state()["restore"]["provider"], state["restore"]["provider"])
        self.assertEqual(self.state()["restore"]["default_fields"], state["restore"]["default_fields"])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_restore_string_or_missing_image_keeps_later_settings(self):
        for image in ("google/custom-image", None):
            with self.subTest(image=image):
                config = json.loads(self.path.read_text())
                if image is None:
                    config["agents"]["defaults"].pop("imageGenerationModel", None)
                else:
                    config["agents"]["defaults"]["imageGenerationModel"] = image
                self.path.write_text(json.dumps(config))
                self.login()
                changed = json.loads(self.path.read_text())
                changed["agents"]["defaults"]["imageGenerationModel"]["timeoutMs"] = 240000
                changed["tools"]["media"]["audio"]["enabled"] = False
                self.path.write_text(json.dumps(changed))
                self.manager._codex_restore_models()
                restored = json.loads(self.path.read_text())
                self.assertEqual(restored["agents"]["defaults"]["imageGenerationModel"],
                                 {"timeoutMs": 240000, **({"primary": image} if image else {})})
                self.assertEqual(restored["tools"]["media"], {"audio": {"enabled": False}})
                self.path.write_text(json.dumps(self.original))

    def test_legacy_media_upgrade_failure_preserves_original_restore_record(self):
        self.login()
        state = self.state()
        state["restore"].pop("media")
        self.manager._codex_state_path().write_text(json.dumps(state))
        original_state = self.manager._codex_state_path().read_bytes()
        original_config = self.path.read_bytes()
        from agent_manage.codex_management import _private_json

        def fail_active(path, body):
            if path == self.manager._codex_state_path() and body.get("status") == "active":
                raise OSError("state write failed")
            return _private_json(path, body)

        with patch("agent_manage.codex_management._private_json", side_effect=fail_active):
            with self.assertRaisesRegex(OSError, "state write failed"):
                self.login()
        self.assertEqual(self.path.read_bytes(), original_config)
        self.assertEqual(self.manager._codex_state_path().read_bytes(), original_state)

    def test_pure_codex_provider_has_native_format_and_no_key(self):
        config = json.loads(self.path.read_text())
        config["models"]["providers"].pop("openai")
        self.path.write_text(json.dumps(config))
        self.login()
        provider = json.loads(self.path.read_text())["models"]["providers"]["openai"]
        self.assertEqual(provider["api"], self.manager.CODEX_API)
        self.assertEqual(provider["baseUrl"], self.manager.CODEX_BASE_URL)
        self.assertEqual(provider["auth"], "oauth")
        self.assertEqual(provider["agentRuntime"], {"id": "openclaw"})
        self.assertNotIn("apiKey", provider)
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_environment_switch_does_not_require_an_existing_agent(self):
        config = json.loads(self.path.read_text())
        config["agents"]["list"] = []
        self.path.write_text(json.dumps(config))
        self.assertEqual(self.login()["switched_agents"], [])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_repeated_switch_keeps_original_backup(self):
        self.login()
        config, backup = self.path.read_bytes(), self.manager._codex_state_path().read_bytes()
        self.assertEqual(self.login()["model"], "openai/gpt-6.1-sol")
        self.assertEqual(self.path.read_bytes(), config)
        self.assertEqual(self.manager._codex_state_path().read_bytes(), backup)
        self.assertEqual(stat.S_IMODE(self.manager._codex_state_path().stat().st_mode), 0o600)

    def test_failed_validation_changes_nothing(self):
        original = self.path.read_bytes()
        self.fixture.runner.fail_validate = True
        with self.assertRaisesRegex(RuntimeError, "invalid config"):
            self.login()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.manager._codex_state_path().exists())

    def test_active_state_write_failure_rolls_back_config_bytes_and_mode(self):
        from agent_manage.codex_management import _private_json
        original, mode = self.path.read_bytes(), stat.S_IMODE(self.path.stat().st_mode)
        def fail_active(path, body):
            if path == self.manager._codex_state_path() and body.get("status") == "active":
                raise OSError("state write failed")
            return _private_json(path, body)
        with patch("agent_manage.codex_management._private_json", side_effect=fail_active):
            with self.assertRaisesRegex(OSError, "state write failed"):
                self.login()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)
        self.assertFalse(self.manager._codex_state_path().exists())
        self.assertEqual(self.auth_file.read_bytes(), self.auth_bytes)

    def test_restore_preserves_later_channel_auth_and_media_changes(self):
        self.login()
        config = json.loads(self.path.read_text())
        config["channels"]["telegram"]["accounts"]["added"] = {"botToken": "later-channel"}
        config["auth"]["order"]["openai"] = ["openai:later-login"]
        config["agents"]["defaults"]["imageGenerationModel"]["timeoutMs"] = 240000
        config["agents"]["list"].reverse()
        self.path.write_text(json.dumps(config))
        self.auth_file.write_bytes(b"later credentials owned by OpenClaw")
        result = self.manager._codex_restore_models()
        restored = json.loads(self.path.read_text())
        self.assertTrue(result["models_restored"])
        self.assertNotIn("credentials_removed", result)
        self.assertEqual(restored["models"], self.original["models"])
        self.assertEqual(restored["agents"]["list"], list(reversed(self.original["agents"]["list"])))
        self.assertEqual(restored["auth"], config["auth"])
        self.assertEqual(restored["channels"], config["channels"])
        self.assertEqual(restored["agents"]["defaults"]["imageGenerationModel"]["timeoutMs"], 240000)
        self.assertEqual(self.auth_file.read_bytes(), b"later credentials owned by OpenClaw")
        self.assertFalse(self.manager._codex_state_path().exists())
        self.assertFalse(self.manager._codex_restore_models()["models_restored"])

    def test_restore_cleans_new_agents_codex_selectors(self):
        self.login()
        config = json.loads(self.path.read_text())
        config["agents"]["list"].extend([
            {"id": "new", "model": "openai/gpt-6-sol", "models": {"openai/gpt-6-sol": {}}, "utilityModel": "openai/gpt-6-luna"},
            {"id": "new-custom", "model": {"primary": "custom/mine", "fallbacks": ["openai/gpt-6-sol"]}},
        ])
        self.path.write_text(json.dumps(config))
        self.manager._codex_restore_models()
        agents = json.loads(self.path.read_text())["agents"]["list"]
        self.assertEqual(agents[-2], {"id": "new"})
        self.assertEqual(agents[-1]["model"], {"primary": "custom/mine", "fallbacks": []})

    def test_failed_restore_keeps_backup_for_retry(self):
        self.login()
        self.fixture.runner.fail_validate = True
        with self.assertRaisesRegex(RuntimeError, "invalid config"):
            self.manager._codex_restore_models()
        self.assertTrue(self.manager._codex_models_active())
        self.fixture.runner.fail_validate = False
        self.assertTrue(self.manager._codex_restore_models()["models_restored"])

    def test_model_refresh_does_not_overwrite_codex_configuration(self):
        self.login()
        before = self.path.read_bytes()
        self.assertEqual(self.manager.get_supported_models()["supported_model_refs"], self.login()["supported_model_refs"])
        self.assertEqual(self.manager.update_model_catalog()["reason"], "codex_login_active")
        refreshed = self.manager.refresh_agent(RefreshAgentRequest("demo", models_only=True))
        self.assertEqual(refreshed["models"]["reason"], "codex_login_active")
        with self.assertRaisesRegex(FileExistsError, "Log out of Codex"):
            self.manager._configure_config_models(model_key="test-key", supported_models=self.fixture.catalog["models"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_dry_run_does_not_write_or_call_openclaw(self):
        self.fixture.runner.dry_run = True
        before = self.path.read_bytes()
        self.assertEqual(self.login()["status"], "preview")
        self.assertTrue(self.manager._codex_restore_models()["skipped"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.manager._codex_state_path().exists())
        self.assertEqual(self.fixture.runner.calls, [])

    def test_cli_has_no_model_selection_login_job_or_agent(self):
        for method, arguments in (("codex_login", ["codex-login"]),
                                  ("codex_logout", ["codex-logout"])):
            with patch.object(InstanceManagerV2, method, return_value={"ok": True}) as mocked, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(arguments), 0)
            self.assertEqual(json.loads(output.getvalue())["typeCode"], 1)
            mocked.assert_called_once_with()
        for flag in ("--login-id", "--agent", "--model"):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["codex-login", flag, "unused"]), 1)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "INVALID_ARGUMENT")

    @unittest.skipUnless(os.environ.get("AGENT_MANAGE_TEST_OPENCLAW_BIN"), "Set server-target OpenClaw to validate its config")
    def test_new_models_validate_on_old_server_without_touching_native_auth(self):
        binary = os.environ["AGENT_MANAGE_TEST_OPENCLAW_BIN"]
        self.manager.runner = LocalRunner(openclaw_bin=binary)
        self.manager.bin = binary
        self.login()
        pure = json.loads(self.path.read_text())
        pure["models"]["providers"]["openai"] = {"api": self.manager.CODEX_API,
            "baseUrl": self.manager.CODEX_BASE_URL, "auth": "oauth",
            "agentRuntime": {"id": "openclaw"}, "models": self.state()["models"]}
        self.manager._codex_validate(pure)
        self.manager._codex_restore_models()
        self.assertEqual(self.auth_file.read_bytes(), self.auth_bytes)


if __name__ == "__main__":
    unittest.main()
