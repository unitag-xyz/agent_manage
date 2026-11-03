import copy
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from agent_manage.cli import _success_response
from agent_manage.models import AddAgentRequest, AddAgentsRequest, CreateInstanceRequest
from agent_manage.orchestrator import InstanceManagerV2
from test_create_instance_v2 import FakeRunner


class HostRestartContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "openclaw.json"
        self.templates = self.root / "templates"
        self.templates.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = {"agents": {"list": [{"id": "prebuilt", "workspace": str(self.workspace)}]},
                       "gateway": {"auth": {"mode": "token", "token": "fixture-token"}}}
        self.path.write_text(json.dumps(self.config))
        self.env = patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container",
            "OPENCLAW_GATEWAY_TOKEN": "fixture-token",
            "UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN": "https://fixture.invalid"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manager = InstanceManagerV2(FakeRunner(), template_root=str(self.templates), config_path=str(self.path))
        # Existing releases have no Host-specific environment flag; isolate capability detection here.
        self.manager._host_reload_available = Mock(return_value=True)
        self.manager._fetch_supported_gateway_models = Mock(return_value={"models": [
            {"id": "chat", "model_ref": "fixture/chat", "definition": {"id": "chat"}}]})
        self.request = CreateInstanceRequest(model_key="fixture-key", workspace_root=str(self.root / "data"))

    def configure(self, mode=None):
        if mode is not None:
            config = json.loads(self.path.read_text())
            config["gateway"]["reload"] = {"mode": mode}
            self.path.write_text(json.dumps(config))
        return self.manager.configure_instance(self.request)

    def test_default_hybrid_and_restart_do_not_request_an_extra_restart(self):
        for mode in (None, "hybrid", "restart"):
            with self.subTest(mode=mode):
                self.path.write_text(json.dumps(self.config))
                result = self.configure(mode)
                self.assertFalse(result["restart_required"])
                self.assertFalse(_success_response(result, self.manager)["restartRequired"])

    def test_hot_first_prebuilt_configure_requires_restart_but_model_key_update_does_not(self):
        self.assertTrue(self.configure("hot")["restart_required"])
        self.request.model_key = "fixture-key-v2"
        self.assertFalse(self.configure()["restart_required"])

    def test_off_changes_require_restart_but_a_second_identical_configure_is_a_noop(self):
        self.assertTrue(self.configure("off")["restart_required"])
        self.assertFalse(self.configure()["restart_required"])

    def test_pending_client_restart_is_never_cleared(self):
        self.manager.restart_required = True
        result = self.configure()
        self.assertTrue(result["restart_required"])
        self.assertTrue(_success_response(result, self.manager)["restartRequired"])

    def test_unsupported_runtime_keeps_existing_create_and_configure_contract(self):
        self.manager._host_reload_available.return_value = False
        self.assertTrue(self.configure()["restart_required"])

    def test_create_and_attach_aggregate_changes_instead_of_only_counting_new_agents(self):
        for name in ("trial", "basic"):
            with zipfile.ZipFile(self.templates / (name + ".zip"), "w") as archive:
                archive.writestr("SOUL.md", "fixture")
        self.request.template_name = "trial"
        result = self.manager.create_instance(self.request)
        self.assertFalse(result["restart_required"])
        self.assertFalse(_success_response(result, self.manager)["restartRequired"])
        result = self.manager.add_agent(template_name="basic", workspace_root=str(self.root / "data"))
        self.assertGreater(result["added_count"], 0)
        self.assertFalse(result["restart_required"])
        self.assertFalse(_success_response(result, self.manager)["restartRequired"])
        config = json.loads(self.path.read_text())
        config["gateway"]["reload"] = {"mode": "off"}
        config["tools"]["web"]["fetch"]["useTrustedEnvProxy"] = False
        self.path.write_text(json.dumps(config))
        result = self.manager.add_agents(AddAgentsRequest(
            agents=[AddAgentRequest("prebuilt", template_name="basic")],
            workspace_root=str(self.root / "data")))
        self.assertEqual(result["added_count"], 0)
        self.assertTrue(result["restart_required"])
        result = self.manager.add_agents(AddAgentsRequest(
            agents=[AddAgentRequest("prebuilt", template_name="basic")],
            workspace_root=str(self.root / "data")))
        self.assertEqual(result["added_count"], 0)
        self.assertFalse(result["restart_required"])
        self.assertFalse(_success_response(result, self.manager)["restartRequired"])


class StaticReloadDecisionTest(unittest.TestCase):
    def decision(self, before, after):
        from agent_manage.restart_policy import host_restart_required
        return host_restart_required(before, after)

    def test_modes_field_combinations_and_unknowns(self):
        for mode in (None, "hybrid", "hot", "restart", "off", "future"):
            for kind in ("model", "gateway", "both", "unknown", "noop"):
                with self.subTest(mode=mode, kind=kind):
                    before = {"gateway": {"auth": {"token": "a"}},
                              "models": {"providers": {"x": {"apiKey": "a"}}}}
                    if mode is not None:
                        before["gateway"]["reload"] = {"mode": mode}
                    after = copy.deepcopy(before)
                    if kind in ("model", "both"):
                        after["models"]["providers"]["x"]["apiKey"] = "b"
                    if kind in ("gateway", "both"):
                        after["gateway"]["auth"]["token"] = "b"
                    if kind == "unknown":
                        after["future"] = {"field": True}
                    expected = (mode == "future" or kind == "unknown" or
                                (kind != "noop" and (mode == "off" or
                                 (mode == "hot" and kind in ("gateway", "both")))))
                    self.assertEqual(self.decision(before, after), expected)

    def test_policy_transitions_invalid_settings_and_type_changes_are_conservative(self):
        before = {"gateway": {}, "tools": {"web": {"fetch": {"enabled": False}}}}
        for reload in ({"mode": "hot"}, {"mode": None}, {"mode": True},
                       {"debounceMs": 5000}, {"debounceMs": True}, {"future": True}):
            after = copy.deepcopy(before)
            after["gateway"]["reload"] = reload
            self.assertTrue(self.decision(before, after))
        after = copy.deepcopy(before)
        after["tools"]["web"]["fetch"]["enabled"] = 0
        before["gateway"]["reload"] = after["gateway"]["reload"] = {"mode": "off"}
        self.assertTrue(self.decision(before, after))

    def test_unknown_fields_and_plugin_changes_are_not_assumed_reloadable(self):
        for change in ({"tools": {"future": True}}, {"plugins": {"entries": {"x": True}}},
                       {"models": {"future": True}}, {"gateway": {"future": True}}):
            self.assertTrue(self.decision({}, change))

    def test_custom_plugin_reload_registrations_and_long_debounce_keep_the_fallback(self):
        before = {"gateway": {"reload": {"mode": "hybrid", "debounceMs": 5000}},
                  "models": {"providers": {"x": {"apiKey": "a"}}}}
        after = copy.deepcopy(before)
        after["models"]["providers"]["x"]["apiKey"] = "b"
        self.assertTrue(self.decision(before, after))
        before["gateway"]["reload"].pop("debounceMs")
        after["gateway"]["reload"].pop("debounceMs")
        before["plugins"] = after["plugins"] = {"load": {"paths": ["/custom"]}}
        self.assertTrue(self.decision(before, after))
        before["plugins"] = after["plugins"] = {"entries": {"unknown": {"enabled": True}}}
        self.assertTrue(self.decision(before, after))


class RuntimeIsolationTest(unittest.TestCase):
    def test_only_standard_host_installation_with_the_pinned_engine_can_opt_in(self):
        from agent_manage import restart_policy
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "dist").mkdir()
            binary = package / "openclaw.mjs"
            binary.write_text("fixture")
            (package / "package.json").write_text('{"version":"2026.7.1"}')
            (package / "dist" / "reload.js").write_bytes(b"pinned")
            hashes = {"reload.js": hashlib.sha256(b"pinned").hexdigest()}
            with patch.object(restart_policy, "RELOAD_SOURCE_HASHES", hashes), \
                    patch.object(restart_policy.shutil, "which", return_value=str(binary)), \
                    patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_ACTIVATION_MODE": ""}):
                for path, container, expected in (
                    ("/opt/unitag-agent-manager/current/agent_manage/restart_policy.py", True, True),
                    ("/opt/unitag/agent-manager/agent_manage/restart_policy.py", True, False),
                    ("/data/agent-manager/current/agent_manage/restart_policy.py", True, False),
                    ("/custom/agent_manage/restart_policy.py", True, False),
                    ("/opt/unitag-agent-manager/current/agent_manage/restart_policy.py", False, False),
                ):
                    with self.subTest(path=path, container=container), patch.object(restart_policy, "__file__", path):
                        self.assertEqual(restart_policy.standard_host_reload_available(str(binary), container_runtime=container), expected)
                with patch.object(restart_policy, "__file__", "/opt/unitag-agent-manager/current/agent_manage/restart_policy.py"):
                    with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_ACTIVATION_MODE": "fly"}):
                        self.assertFalse(restart_policy.standard_host_reload_available(str(binary), container_runtime=True))
                    (package / "dist" / "reload.js").write_bytes(b"changed")
                    self.assertFalse(restart_policy.standard_host_reload_available(str(binary), container_runtime=True))
                    (package / "dist" / "reload.js").write_bytes(b"pinned")
                    (package / "package.json").write_text('{"version":"future"}')
                    self.assertFalse(restart_policy.standard_host_reload_available(str(binary), container_runtime=True))

    def test_bad_or_missing_snapshot_and_pending_restart_keep_the_fallback(self):
        manager = object.__new__(InstanceManagerV2)
        manager.restart_required = False
        manager.runner = Mock(dry_run=False)
        manager._host_reload_available = Mock(return_value=True)
        for snapshot in (None, (b"not json", 0o600), (b"[]", 0o600)):
            self.assertTrue(manager._instance_restart_required(snapshot))
        manager.runner.dry_run = True
        self.assertFalse(manager._instance_restart_required(None))
        manager.restart_required = True
        self.assertTrue(manager._instance_restart_required(None))
