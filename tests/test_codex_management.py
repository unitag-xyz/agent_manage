import io
import json
import os
import stat
import unittest
import zipfile
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
        self.assertEqual(set(config["models"]["providers"]), {"openai"})
        self.assertEqual(config["models"]["mode"], "replace")
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
        self.assertEqual({row["id"] for row in provider["models"]},
                         {model_id for model_id, _ in self.manager.CODEX_MODELS} | {"gpt-image-2"})
        for row in provider["models"]:
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
        state["restore"].pop("catalog")
        state["provider_mode"] = "mixed_api_and_codex"
        self.manager._codex_state_path().write_text(json.dumps(state))
        config = json.loads(self.path.read_text())
        config["models"]["mode"] = self.original["models"]["mode"]
        config["models"]["providers"].update({provider: value for provider, value in self.original["models"]["providers"].items()
                                             if provider != "openai"})
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
        state["restore"].pop("catalog")
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

    def test_cleans_all_provider_catalogs_and_restores_them_on_logout(self):
        config = json.loads(self.path.read_text())
        config["models"]["providers"]["google"] = {
            "baseUrl": "https://api.dola.io/aigateway/shop/v1", "apiKey": "old-google-key",
            "api": "google-generative-ai", "models": [{"id": "gemini-3.1-flash-image-preview", "name": "Old image"}]}
        config["agents"]["defaults"]["imageGenerationModel"] = {
            "primary": "google/gemini-3.1-flash-image-preview", "fallbacks": ["custom/mine"]}
        config["agents"]["defaults"]["models"]["google/gemini-3.1-flash-image-preview"] = {"alias": "old-image"}
        self.path.write_text(json.dumps(config))
        self.login()
        changed = json.loads(self.path.read_text())
        self.assertEqual(set(changed["models"]["providers"]), {"openai"})
        self.assertEqual(changed["models"]["mode"], "replace")
        self.assertEqual(set(changed["agents"]["defaults"]["models"]),
                         {"openai/" + model_id for model_id, _ in self.manager.CODEX_MODELS} | {"openai/gpt-image-2"})
        self.assertEqual(self.state()["restore"]["catalog"]["providers"], config["models"]["providers"])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_audio_connection_survives_without_its_old_model_catalog(self):
        config = json.loads(self.path.read_text())
        config["models"]["providers"]["google"] = {
            "api": "google-generative-ai", "apiKey": "google-audio-key", "baseUrl": "https://generativelanguage.googleapis.com",
            "models": [{"id": "gemini-3.1-flash-image-preview", "name": "Old image"}]}
        config["tools"]["media"] = {"audio": {"models": [{"provider": "google", "model": "gemini-2.5-flash"}]}}
        self.path.write_text(json.dumps(config))
        self.login()
        changed = json.loads(self.path.read_text())
        self.assertEqual(changed["models"]["providers"]["google"],
                         {**config["models"]["providers"]["google"], "models": []})
        self.assertEqual(changed["tools"]["media"], config["tools"]["media"])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_existing_media_login_cleans_catalog_without_replacing_first_backup(self):
        self.login()
        state = self.state()
        state["restore"].pop("catalog")
        self.manager._codex_state_path().write_text(json.dumps(state))
        changed = json.loads(self.path.read_text())
        changed["models"]["mode"] = self.original["models"]["mode"]
        changed["models"]["providers"].update({provider: value for provider, value in self.original["models"]["providers"].items()
                                              if provider != "openai"})
        changed["models"]["providers"]["openai"]["models"] += [{"id": "old-chat", "name": "Old Chat"}]
        changed["agents"]["defaults"]["models"]["custom/mine"] = {}
        changed["agents"]["list"][1]["models"]["custom/mine"] = {}
        self.path.write_text(json.dumps(changed))
        self.login()
        current = json.loads(self.path.read_text())
        self.assertEqual(set(current["models"]["providers"]), {"openai"})
        self.assertNotIn("custom/mine", current["agents"]["defaults"]["models"])
        self.assertNotIn("custom/mine", current["agents"]["list"][1]["models"])
        self.assertNotIn("old-chat", [row["id"] for row in current["models"]["providers"]["openai"]["models"]])
        self.assertEqual(self.state()["restore"]["provider"], state["restore"]["provider"])
        self.assertEqual(self.state()["restore"]["media"], state["restore"]["media"])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_catalog_restore_preserves_new_providers_and_original_missing_mode(self):
        config = json.loads(self.path.read_text())
        config["models"].pop("mode")
        self.path.write_text(json.dumps(config))
        self.login()
        changed = json.loads(self.path.read_text())
        added = {"api": "openai-completions", "baseUrl": "https://new.example/v1", "models": [{"id": "new", "name": "New"}]}
        changed["models"]["providers"]["later"] = added
        self.path.write_text(json.dumps(changed))
        self.manager._codex_restore_models()
        config["models"]["providers"]["later"] = added
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_tts_connection_survives_with_no_old_model_definitions(self):
        config = json.loads(self.path.read_text())
        config["messages"] = {"tts": {"provider": "custom"}}
        self.path.write_text(json.dumps(config))
        self.login()
        current = json.loads(self.path.read_text())
        self.assertEqual(current["models"]["providers"]["custom"],
                         {**config["models"]["providers"]["custom"], "models": []})
        self.assertEqual(current["messages"], config["messages"])
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), config)

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

    def test_existing_login_updates_context_and_preserves_selection_and_restore(self):
        self.login()
        state = self.state()
        restore = state["restore"]
        for row in state["models"]:
            row.pop("contextWindow")
        self.manager._codex_state_path().write_text(json.dumps(state))
        config = json.loads(self.path.read_text())
        config["agents"]["defaults"]["model"]["primary"] = "openai/gpt-6-luna"
        for row in config["models"]["providers"]["openai"]["models"]:
            row["contextWindow"] = 200000
        self.path.write_text(json.dumps(config))
        auth_before = self.auth_file.read_bytes()
        result = self.login()
        current = json.loads(self.path.read_text())
        self.assertEqual(result["model"], "openai/gpt-6-luna")
        self.assertEqual(self.state()["restore"], restore)
        self.assertEqual(self.auth_file.read_bytes(), auth_before)
        for row in current["models"]["providers"]["openai"]["models"]:
            self.assertEqual(row["contextWindow"], 200000 if row["id"] == "gpt-image-2" else 258000)
        self.assertTrue(all(row["contextWindow"] == 258000 for row in result["models"]))
        before = self.path.read_bytes(), self.manager._codex_state_path().read_bytes()
        self.login()
        self.assertEqual((self.path.read_bytes(), self.manager._codex_state_path().read_bytes()), before)
        self.manager._codex_restore_models()
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_context_upgrade_write_failure_restores_active_login(self):
        self.login()
        state = self.state()
        for row in state["models"]:
            row.pop("contextWindow")
        self.manager._codex_state_path().write_text(json.dumps(state))
        config = json.loads(self.path.read_text())
        for row in config["models"]["providers"]["openai"]["models"]:
            row.pop("contextWindow", None)
        self.path.write_text(json.dumps(config))
        before = self.path.read_bytes(), self.manager._codex_state_path().read_bytes()
        from agent_manage.codex_management import _private_json

        def fail_active(path, body):
            if path == self.manager._codex_state_path() and body.get("status") == "active":
                raise OSError("state write failed")
            return _private_json(path, body)

        with patch("agent_manage.codex_management._private_json", side_effect=fail_active):
            with self.assertRaisesRegex(OSError, "state write failed"):
                self.login()
        self.assertEqual((self.path.read_bytes(), self.manager._codex_state_path().read_bytes()), before)

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

    def test_dola_model_refresh_does_not_overwrite_codex_configuration(self):
        self.login()
        before = self.path.read_bytes()
        self.assertEqual(self.manager.get_supported_models()["supported_model_refs"], self.login()["supported_model_refs"])
        self.assertEqual(self.manager.update_model_catalog()["reason"], "codex_login_active")
        with self.assertRaisesRegex(FileExistsError, "Log out of Codex"):
            self.manager._configure_config_models(model_key="test-key", supported_models=self.fixture.catalog["models"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_agent_refresh_syncs_codex_catalog_and_logout_restores_original_models(self):
        self.login()
        before = json.loads(self.path.read_text())
        restore = self.manager._codex_state_path().read_bytes()
        before["agents"]["list"].append({"id": "new"})
        self.path.write_text(json.dumps(before))
        rows = [{"id": "gpt-6.2-sol", "name": "GPT-6.2 Sol", "input": ["text"]}]
        request = RefreshAgentRequest("demo", template_dir=str(self.fixture.source))
        with patch.object(self.manager, "_codex_bridge", return_value={"models": rows}) as bridge:
            result = self.manager.refresh_agent(request)
        bridge.assert_called_once_with("list-models")
        expected = ["openai/gpt-6.2-sol", "openai/gpt-6.1-sol"]
        self.assertEqual(result["models"]["supported_model_refs"], expected)
        self.assertEqual(result["models"]["retained_model_refs"], ["openai/gpt-6.1-sol"])
        self.assertEqual(self.manager.get_supported_models()["supported_model_refs"], expected)
        self.assertEqual(self.manager._codex_state_path().read_bytes(), restore)
        self.assertEqual((self.fixture.workspace / "SOUL.md").read_text(), "version one\n")
        active = json.loads(self.path.read_text())
        self.assertEqual(active["agents"]["defaults"]["model"], before["agents"]["defaults"]["model"])
        self.assertEqual(active["agents"]["defaults"]["imageGenerationModel"],
                         before["agents"]["defaults"]["imageGenerationModel"])
        self.assertEqual(active["tools"], before["tools"])
        self.assertEqual(active["agents"]["list"][0]["model"], before["agents"]["list"][0]["model"])
        self.assertIn("openai/gpt-6.2-sol", active["agents"]["list"][1]["models"])
        active["agents"]["list"][-1]["model"] = "openai/gpt-6.2-sol"
        self.path.write_text(json.dumps(active))
        self.assertEqual(self.login()["supported_model_refs"], expected)
        self.manager._codex_restore_models()
        restored = json.loads(self.path.read_text())
        self.assertEqual(restored["agents"]["list"].pop(), {"id": "new"})
        self.assertEqual(restored, self.original)

    def test_unavailable_codex_catalog_keeps_models_and_refreshes_template(self):
        self.login()
        before = self.path.read_bytes()
        request = RefreshAgentRequest("demo", template_dir=str(self.fixture.source))
        with patch.object(self.manager, "_codex_bridge", side_effect=RuntimeError("upstream unavailable")):
            result = self.manager.refresh_agent(request)
        self.assertEqual(result["models"]["reason"], "codex_catalog_unavailable")
        self.assertTrue(result["models"]["skipped"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.fixture.workspace / "SOUL.md").read_text(), "version one\n")

    def test_refresh_uses_pre_login_region_and_saves_models_when_new_zip_url_is_missing(self):
        provider = self.original["models"]["providers"]["dola"]
        provider["baseUrl"] = "https://api.dolaio.cn/aigateway/mystore/v1"
        self.path.write_text(json.dumps(self.original))
        cache = self.manager.template_root / "demo.zip"
        cache.parent.mkdir()
        with zipfile.ZipFile(cache, "w") as archive:
            archive.writestr("template.yaml", "version: 0.1.2\n")
            archive.writestr("install.sh", "echo installed >> install.log\n")
        self.manager.refresh_agent(RefreshAgentRequest("demo", agent_zip=str(cache), template_only=True))
        cached_zip = cache.read_bytes()
        template_state = self.fixture.state()
        self.login()
        self.assertNotIn("dola", json.loads(self.path.read_text())["models"]["providers"])
        release = {"typeCode": 1, "content": {"identify": "demo", "version": "0.1.3"}}
        models = {"models": [{"id": "gpt-6.2-sol", "input": ["text"]}]}
        for config_changes in (True, False):
            with self.subTest(config_changes=config_changes), \
                 patch("agent_manage.refresh_management.urlopen", return_value=io.BytesIO(json.dumps(release).encode())) as lookup, \
                 patch.object(self.manager, "_codex_bridge", return_value=models):
                result = self.manager.refresh_agent(RefreshAgentRequest("demo"))
                self.assertEqual(lookup.call_args.args[0].full_url,
                                 "https://api.dolaio.cn/vpsmanagement/api/frontend/Template/demo")
                self.assertTrue(result["ok"])
                self.assertTrue(result["partial"])
                self.assertEqual(bool(result["backup_path"]), config_changes)
                self.assertFalse(result["models"]["skipped"])
                self.assertEqual(result["template"]["status"], "failed")
                self.assertEqual(result["template"]["error"]["code"], "TEMPLATE_ZIP_URL_MISSING")
                self.assertEqual(result["template"]["target_version"], "0.1.3")
                self.assertEqual(result["install"], {"skipped": True, "reason": "template_update_failed"})
        self.assertIn("openai/gpt-6.2-sol", self.manager.get_supported_models()["supported_model_refs"])
        self.assertEqual(cache.read_bytes(), cached_zip)
        self.assertEqual(self.fixture.state(), template_state)
        self.assertEqual((self.fixture.workspace / "install.log").read_text(), "installed\n")

    def test_template_lookup_uses_pre_login_region_without_generic_dola_provider(self):
        providers = self.original["models"]["providers"]
        del providers["dola"]
        providers["openai"]["baseUrl"] = "https://api.dolaio.cn/aigateway/mystore/v1"
        self.path.write_text(json.dumps(self.original))
        self.login()
        config = json.loads(self.path.read_text())
        release = {"typeCode": 1, "content": {"identify": "demo", "version": "0.1.3"}}
        with patch("agent_manage.refresh_management.urlopen", return_value=io.BytesIO(json.dumps(release).encode())) as lookup:
            self.assertEqual(self.manager._latest_template_release("demo", config), release["content"])
        self.assertEqual(lookup.call_args.args[0].host, "api.dolaio.cn")

    def test_codex_model_refresh_uses_config_transaction_and_dry_run_never_refreshes_auth(self):
        self.login()
        before = self.path.read_bytes()
        request = RefreshAgentRequest("demo", models_only=True)
        self.fixture.runner.dry_run = True
        with patch.object(self.manager, "_codex_bridge") as bridge:
            self.assertEqual(self.manager.refresh_agent(request)["models"]["reason"], "codex_catalog_dry_run")
        bridge.assert_not_called()
        self.fixture.runner.dry_run = False
        with patch.object(self.manager, "_codex_bridge", return_value={"models": [{"id": "gpt-6.2-sol"}]}):
            self.fixture.runner.fail_validate = True
            with self.assertRaisesRegex(RuntimeError, "invalid config"):
                self.manager.refresh_agent(request)
            self.assertEqual(self.path.read_bytes(), before)
            self.fixture.runner.fail_validate = False
            result = self.manager.refresh_agent(request)
            self.assertIn("openai/gpt-6.2-sol", result["models"]["supported_model_refs"])

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
