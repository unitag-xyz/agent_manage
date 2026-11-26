import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_manage.adapters.openclaw.manager_core import ManagerCore
from agent_manage.models import CreateInstanceRequest
from agent_manage.adapters.openclaw.orchestrator import InstanceManagerV2


class DummyRunner:
    dry_run = False
    openclaw_bin = "openclaw"

    def log(self, message):
        return None


class ManagerCoreConfigTest(unittest.TestCase):
    def _write_config(self, path: Path, config: dict) -> None:
        path.write_text(json.dumps(config), encoding="utf-8")

    def test_container_gateway_auth_without_control_ui_origin_leaves_allowed_origins_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "UNITAG_AGENT_MANAGER_RUNTIME": "container",
                "UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN": "",
            },
        ):
            config_path = Path(tmpdir) / "openclaw.json"
            original = {
                "gateway": {
                    "auth": {"mode": "none"},
                    "controlUi": {"allowedOrigins": ["https://existing.example"], "theme": "dark"},
                }
            }
            self._write_config(config_path, original)
            result = InstanceManagerV2(DummyRunner(), config_path=str(config_path))._configure_gateway_auth("gateway-token")

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["gateway"]["controlUi"]["allowedOrigins"], ["https://existing.example"])
            self.assertEqual(saved["gateway"]["controlUi"]["theme"], "dark")
            self.assertTrue(saved["gateway"]["controlUi"]["dangerouslyDisableDeviceAuth"])
            self.assertNotIn("control_ui_allowed_origins", result)
            self.assertTrue(result["control_ui_device_auth_disabled"])
            self.assertNotIn("gateway_token", result)

    def test_container_gateway_auth_merges_and_deduplicates_control_ui_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "UNITAG_AGENT_MANAGER_RUNTIME": "container",
                "UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN": "https://new.example/",
            },
        ):
            config_path = Path(tmpdir) / "openclaw.json"
            self._write_config(
                config_path,
                {
                    "gateway": {
                        "auth": {"mode": "none"},
                        "controlUi": {
                            "allowedOrigins": [
                                "https://existing.example",
                                "https://existing.example/",
                                123,
                                "not an origin",
                            ],
                            "theme": "dark",
                            "custom": {"keep": True},
                        },
                        "otherGatewayField": "preserved",
                    }
                },
            )
            result = InstanceManagerV2(DummyRunner(), config_path=str(config_path))._configure_gateway_auth("gateway-token")

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["gateway"]["controlUi"]["allowedOrigins"],
                ["https://existing.example", "https://new.example"],
            )
            self.assertEqual(saved["gateway"]["controlUi"]["theme"], "dark")
            self.assertEqual(saved["gateway"]["controlUi"]["custom"], {"keep": True})
            self.assertTrue(saved["gateway"]["controlUi"]["dangerouslyDisableDeviceAuth"])
            self.assertEqual(saved["gateway"]["otherGatewayField"], "preserved")
            self.assertEqual(result["control_ui_allowed_origins"], ["https://existing.example", "https://new.example"])
            self.assertTrue(result["control_ui_device_auth_disabled"])
            self.assertNotIn("gateway_token", result)

    def test_invalid_container_control_ui_origin_fails_before_config_write(self):
        invalid_origins = [
            "ftp://control.example",
            "https://user:pass@control.example",
            "https://control.example/path",
            "https://control.example?query=true",
            "https://control.example#fragment",
        ]
        for origin in invalid_origins:
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as tmpdir, patch.dict(
                os.environ,
                {
                    "UNITAG_AGENT_MANAGER_RUNTIME": "container",
                    "UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN": origin,
                },
            ):
                config_path = Path(tmpdir) / "openclaw.json"
                original = '{"gateway":{"controlUi":{"theme":"dark"}}}\n'
                config_path.write_text(original, encoding="utf-8")
                manager = InstanceManagerV2(DummyRunner(), config_path=str(config_path))

                with self.assertRaisesRegex(ValueError, "CONTROL_UI_ORIGIN"):
                    manager._configure_gateway_auth("gateway-token")

                self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_container_gateway_auth_dry_run_reports_control_ui_origin_without_writing_config(self):
        class DryRunRunner(DummyRunner):
            dry_run = True

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "UNITAG_AGENT_MANAGER_RUNTIME": "container",
                "UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN": "https://control.example",
            },
        ):
            config_path = Path(tmpdir) / "openclaw.json"
            original = '{"gateway":{"controlUi":{"allowedOrigins":["https://existing.example"]}}}\n'
            config_path.write_text(original, encoding="utf-8")
            result = InstanceManagerV2(DryRunRunner(), config_path=str(config_path))._configure_gateway_auth("gateway-token")

            self.assertTrue(result["skipped"])
            self.assertEqual(result["control_ui_allowed_origins"], ["https://control.example"])
            self.assertTrue(result["control_ui_device_auth_disabled"])
            self.assertNotIn("gateway_token", result)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_vps_gateway_auth_ignores_control_ui_origin_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "UNITAG_AGENT_MANAGER_RUNTIME": "vps",
                "UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN": "not-an-origin",
            },
        ):
            config_path = Path(tmpdir) / "openclaw.json"
            self._write_config(config_path, {"gateway": {"controlUi": {"theme": "dark"}}})
            result = InstanceManagerV2(DummyRunner(), config_path=str(config_path))._configure_gateway_auth("gateway-token")

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["gateway"]["controlUi"], {"theme": "dark"})
            self.assertNotIn("control_ui_allowed_origins", result)
            self.assertNotIn("control_ui_device_auth_disabled", result)

    def test_config_write_is_atomic_private_and_cleans_failed_temp_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text('{"before": true}\n', encoding="utf-8")
            manager = ManagerCore(DummyRunner(), config_path=str(config_path))

            manager._write_config({"after": True}, note="test")

            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {"after": True},
            )
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

            with self.assertRaises(TypeError):
                manager._write_config({"invalid": object()}, note="invalid")

            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {"after": True},
            )
            self.assertEqual(
                sorted(path.name for path in Path(tmpdir).iterdir()),
                ["openclaw.json", "openclaw.json.bak"],
            )
            self.assertEqual(
                json.loads(
                    (Path(tmpdir) / "openclaw.json.bak").read_text(encoding="utf-8")
                ),
                {"before": True},
            )

    def test_load_config_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "openclaw.json"
            config_path.write_text("[]\n", encoding="utf-8")
            manager = ManagerCore(DummyRunner(), config_path=str(config_path))

            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                manager._load_config()

    def test_container_runtime_defers_gateway_restart_without_systemctl(self):
        runner = DummyRunner()
        with patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_RUNTIME": "container"}):
            manager = InstanceManagerV2(runner)
            result = manager._restart_gateway_service()

        self.assertTrue(manager.restart_required)
        self.assertEqual(result["method"], "container_restart_required")

    def test_container_runtime_uses_persistent_openclaw_paths_and_gateway_token(self):
        runner = DummyRunner()
        with patch.dict(
            os.environ,
            {
                "UNITAG_AGENT_MANAGER_RUNTIME": "container",
                "OPENCLAW_GATEWAY_TOKEN": "container-gateway-token",
            },
            clear=False,
        ):
            manager = ManagerCore(runner)
            self.assertTrue(
                str(manager.template_root)
                .replace("\\", "/")
                .endswith("/home/node/.openclaw/templates")
            )
            self.assertTrue(
                str(manager.config_path)
                .replace("\\", "/")
                .endswith("/home/node/.openclaw/openclaw.json")
            )
            self.assertEqual(manager._container_gateway_token(), "container-gateway-token")

    def test_container_create_instance_writes_environment_gateway_token(self):
        runner = DummyRunner()
        with patch.dict(
            os.environ,
            {
                "UNITAG_AGENT_MANAGER_RUNTIME": "container",
                "OPENCLAW_GATEWAY_TOKEN": "container-gateway-token",
            },
            clear=False,
        ):
            manager = InstanceManagerV2(runner)
            configured_tokens = []
            with (
                patch.object(manager, "resolve_agent_name", return_value="base"),
                patch.object(manager, "resolve_archive_path", return_value=Path("/tmp/base.zip")),
                patch.object(manager, "resolve_template_dir", return_value=Path("/tmp/template/base")),
                patch.object(
                    manager,
                    "_provision_agent_from_template",
                    return_value={
                        "created_agent": True,
                        "created_template_dir": False,
                        "created_workspace": False,
                    },
                ),
                patch.object(manager, "_load_template_manifest", return_value={}),
                patch.object(manager, "_multi_agent_specs_from_template", return_value=[]),
                patch.object(
                    manager,
                    "_fetch_supported_gateway_models",
                    return_value={"models": [], "official_image_model_available": False},
                ),
                patch.object(manager, "_configure_config_models"),
                patch.object(
                    manager,
                    "_configure_gateway_auth",
                    side_effect=lambda token: configured_tokens.append(token) or {},
                ),
                patch.object(manager, "_configure_config_tools"),
                patch.object(manager, "_configure_workspace_defaults"),
                patch.object(manager, "_generate_gateway_token", side_effect=AssertionError),
            ):
                result = manager.create_instance(
                    CreateInstanceRequest(template_name="base", model_key="model-key")
                )

        self.assertEqual(result["gateway_token"], "container-gateway-token")
        self.assertTrue(result["gateway_token_preserved"])
        self.assertEqual(configured_tokens, ["container-gateway-token"])

    def test_catalog_step_returns_summary_without_large_model_payload(self):
        manager = ManagerCore(DummyRunner())
        catalog = {
            "source_url": "https://gateway.example/catalog",
            "model_count": 2,
            "primary_model": "provider-a/model-1",
            "official_image_model_available": True,
            "models": [{"provider": "provider-a", "definition": {"blob": "x" * 5_000_000}}],
            "models_config": {
                "providers": {
                    "provider-a": {"models": [{"blob": "x" * 5_000_000}]},
                    "provider-b": {"models": []},
                }
            },
        }
        steps = []

        returned = manager._run_timed_step(
            steps, "models.fetch_catalog", lambda: catalog
        )

        step_result = steps[0]["result"]
        serialized = json.dumps(steps)
        self.assertIs(returned, catalog)
        self.assertEqual(step_result["provider_names"], ["provider-a", "provider-b"])
        self.assertEqual(step_result["provider_count"], 2)
        self.assertNotIn("models", step_result)
        self.assertNotIn("models_config", step_result)
        self.assertLess(len(serialized.encode("utf-8")), 8_192)


if __name__ == "__main__":
    unittest.main()
