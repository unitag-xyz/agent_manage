import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from agent_manage.adapters.openclaw.seed import (
    INITIALIZING_MARKER,
    assert_seed_has_no_runtime_secrets,
    initialize_openclaw_seed,
)


class ContainerImageTest(unittest.TestCase):
    def test_seed_shared_skill_permissions_allow_later_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            skill = seed / "skills/nginx-delivery"
            skill.mkdir(parents=True)
            (seed / "openclaw.json").write_text("{}")
            (skill / "SKILL.md").write_text("instructions")
            (skill / "SKILL.md").chmod(0o444)
            skill.chmod(0o555)
            try:
                initialize_openclaw_seed(seed, root / "target")
                installed = root / "target/skills/nginx-delivery"
                with tempfile.NamedTemporaryFile(dir=installed):
                    pass
                (installed / "SKILL.md").write_text("updated")
                self.assertEqual(skill.stat().st_mode & 0o777, 0o555)
            finally:
                skill.chmod(0o755)

    def test_nonempty_runtime_config_is_merged_over_prebuilt_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            target = root / "target"
            (seed / "data" / "base").mkdir(parents=True)
            (seed / "openclaw.json").write_text(
                json.dumps(
                    {
                        "agents": {"list": [{"id": "unipay-claw-base"}]},
                        "gateway": {"auth": {"mode": "token"}},
                        "seedOnly": {"enabled": True},
                    }
                ),
                encoding="utf-8",
            )
            (seed / "data" / "base" / "AGENTS.md").write_text("seed\n", encoding="utf-8")
            target.mkdir()
            (target / "openclaw.json").write_text(
                json.dumps(
                    {
                        "gateway": {"auth": {"token": "runtime-gateway-token"}},
                        "agents": {
                            "defaults": {"model": {"primary": "deepseek/deepseek-v4-flash"}}
                        },
                        "models": {
                            "providers": {
                                "deepseek": {
                                    "apiKey": "runtime-permission-ticket",
                                    "models": [{"id": "deepseek-v4-flash"}],
                                }
                            }
                        },
                        "plugins": {
                            "entries": {
                                "openclaw-weixin": {"enabled": True},
                                "feishu": {"enabled": True},
                            }
                        },
                        "runtimeOnly": {"value": 1},
                    }
                ),
                encoding="utf-8",
            )

            result = initialize_openclaw_seed(seed, target)
            merged = json.loads((target / "openclaw.json").read_text(encoding="utf-8"))

            self.assertTrue(result["initialized"])
            self.assertEqual(merged["agents"]["list"][0]["id"], "unipay-claw-base")
            self.assertEqual(merged["gateway"]["auth"]["token"], "runtime-gateway-token")
            self.assertEqual(merged["gateway"]["auth"]["mode"], "token")
            self.assertEqual(
                merged["agents"]["defaults"]["model"]["primary"],
                "deepseek/deepseek-v4-flash",
            )
            self.assertEqual(
                merged["models"]["providers"]["deepseek"]["apiKey"],
                "runtime-permission-ticket",
            )
            self.assertTrue(merged["plugins"]["entries"]["openclaw-weixin"]["enabled"])
            self.assertTrue(merged["plugins"]["entries"]["feishu"]["enabled"])
            self.assertEqual(merged["runtimeOnly"], {"value": 1})
            self.assertTrue((target / "data" / "base" / "AGENTS.md").is_file())
            self.assertNotIn("runtime-gateway-token", (seed / "openclaw.json").read_text(encoding="utf-8"))
            self.assertNotIn(
                "runtime-permission-ticket",
                (seed / "openclaw.json").read_text(encoding="utf-8"),
            )

    def test_initialized_nonempty_target_is_not_reinitialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            target = root / "target"
            seed.mkdir()
            target.mkdir()
            (seed / "openclaw.json").write_text('{"agents": {}}\n', encoding="utf-8")
            (target / "openclaw.json").write_text(
                '{"gateway": {"auth": {"token": "runtime-gateway-token"}}}\n',
                encoding="utf-8",
            )

            initialize_openclaw_seed(seed, target)
            (target / "runtime.db").write_text("runtime\n", encoding="utf-8")
            second = initialize_openclaw_seed(seed, target)

            self.assertFalse(second["initialized"])
            self.assertEqual(second["reason"], "already_initialized")
            self.assertEqual((target / "runtime.db").read_text(encoding="utf-8"), "runtime\n")

    def test_seed_initialization_is_idempotent_and_preserves_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            target = root / "target"
            (seed / "data" / "base").mkdir(parents=True)
            (seed / "openclaw.json").write_text('{"agents": {}}\n', encoding="utf-8")
            (seed / "data" / "base" / "AGENTS.md").write_text("seed\n", encoding="utf-8")

            first = initialize_openclaw_seed(seed, target)
            (target / "runtime.db").write_text("runtime\n", encoding="utf-8")
            second = initialize_openclaw_seed(seed, target)

            self.assertTrue(first["initialized"])
            self.assertFalse(second["initialized"])
            self.assertEqual((target / "runtime.db").read_text(encoding="utf-8"), "runtime\n")

    def test_seed_initialization_resumes_an_interrupted_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            target = root / "target"
            seed.mkdir()
            target.mkdir()
            (seed / "openclaw.json").write_text("{}\n", encoding="utf-8")
            (target / INITIALIZING_MARKER).touch()
            (target / "partial").write_text("old\n", encoding="utf-8")
            (target / "openclaw.json").write_text(
                '{"gateway": {"auth": {"token": "runtime-gateway-token"}}}\n',
                encoding="utf-8",
            )

            result = initialize_openclaw_seed(seed, target)

            self.assertTrue(result["initialized"])
            self.assertTrue((target / "openclaw.json").is_file())
            self.assertFalse((target / INITIALIZING_MARKER).exists())
            self.assertEqual(
                json.loads((target / "openclaw.json").read_text(encoding="utf-8"))["gateway"]["auth"]["token"],
                "runtime-gateway-token",
            )

    def test_seed_config_replace_failure_preserves_runtime_config_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            target = root / "target"
            seed.mkdir()
            target.mkdir()
            (seed / "openclaw.json").write_text('{"agents": {}}\n', encoding="utf-8")
            runtime_config = '{"gateway": {"auth": {"token": "runtime-token"}}}\n'
            (target / "openclaw.json").write_text(runtime_config, encoding="utf-8")

            with patch("agent_manage.file_io.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    initialize_openclaw_seed(seed, target)

            self.assertEqual(
                (target / "openclaw.json").read_text(encoding="utf-8"),
                runtime_config,
            )
            result = initialize_openclaw_seed(seed, target)
            merged = json.loads((target / "openclaw.json").read_text(encoding="utf-8"))
            self.assertTrue(result["initialized"])
            self.assertEqual(merged["gateway"]["auth"]["token"], "runtime-token")

    def test_missing_seed_fails_stably(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(FileNotFoundError, "OpenClaw seed is missing or empty"):
                initialize_openclaw_seed(root / "missing", root / "target")

    def test_image_build_assets_do_not_accept_runtime_secrets(self):
        image_directory = Path(__file__).resolve().parents[1] / "container-image"
        dockerfile = (image_directory / "Dockerfile").read_text(encoding="utf-8")
        upper = dockerfile.upper()
        self.assertNotIn("MODEL_KEY", upper)
        self.assertNotIn("GATEWAY_TOKEN", upper)
        self.assertNotIn("AUTH_TOKEN", upper)
        self.assertIn("/opt/unitag/openclaw-seed", dockerfile)
        self.assertIn("io.dola.unitag.template-identify", dockerfile)
        self.assertIn('"mode":"local"', dockerfile)
        self.assertIn('"auth":{"mode":"token"}', dockerfile)
        self.assertIn('npm install -g "@larksuite/cli@${LARKSUITE_CLI_VERSION}"', dockerfile)
        self.assertIn("checksums.txt", dockerfile)
        self.assertIn("sha256sum --check --strict", dockerfile)
        self.assertIn('openclaw plugins install "@tencent-weixin/openclaw-weixin@${WEIXIN_PLUGIN_VERSION}"', dockerfile)
        self.assertIn('openclaw plugins install "@openclaw/qqbot@${QQBOT_PLUGIN_VERSION}"', dockerfile)
        self.assertIn('openclaw plugins install "@openclaw/feishu@${FEISHU_PLUGIN_VERSION}"', dockerfile)
        self.assertIn("openclaw config set tools.agentToAgent.enabled true --strict-json", dockerfile)
        self.assertNotIn("openclaw config set tools.agentToAgent.allow '[]' --strict-json", dockerfile)
        self.assertIn(
            "/home/node/.openclaw/templates/${TEMPLATE_IDENTIFY}.zip",
            dockerfile,
        )
        self.assertIn("openclaw config set update.checkOnStart false", dockerfile)
        self.assertIn("apt-get", dockerfile)
        self.assertIn("--no-install-recommends nginx", dockerfile)
        self.assertIn("USER node", dockerfile)
        self.assertNotIn("NET_ADMIN", dockerfile)
        self.assertNotIn("NET_BIND_SERVICE", dockerfile)

        nginx_config = (image_directory / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("root /home/node/.openclaw/workspace/public;", nginx_config)
        self.assertIn("pid /tmp/nginx/nginx.pid;", nginx_config)
        self.assertIn("disable_symlinks on;", nginx_config)
        self.assertNotIn("proxy_pass", nginx_config)

        build_script = (image_directory / "build-prebuilt-image.ps1").read_text(encoding="utf-8")
        validation_script = (image_directory / "validate-prebuilt-image.ps1").read_text(encoding="utf-8")
        publish_script = (image_directory / "publish-prebuilt-image.ps1").read_text(encoding="utf-8")
        self.assertIn("validate-prebuilt-image.ps1", build_script)
        self.assertIn("OPENCLAW_GATEWAY_TOKEN", validation_script)
        self.assertIn("GatewayHttp=200", validation_script)
        self.assertIn("NginxHttp=200", validation_script)
        self.assertIn('"--cap-drop", "ALL"', validation_script)
        self.assertIn('"no-new-privileges:true"', validation_script)
        self.assertIn("GracefulStop=true", validation_script)
        self.assertIn("@larksuite/cli", validation_script)
        self.assertIn("ExtensionsConfigured=true", validation_script)
        self.assertIn(".unitag-seed-initialized", validation_script)
        self.assertIn("ImmutableReference=", publish_script)

        seed_module = (Path(__file__).resolve().parents[1] / "agent_manage/adapters/openclaw/seed.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("dirs_exist_ok=True, symlinks=True", seed_module)

    def test_base_image_build_assets_do_not_contain_a_prebuilt_agent(self):
        image_directory = Path(__file__).resolve().parents[1] / "container-image"
        dockerfile = (image_directory / "Dockerfile.base").read_text(encoding="utf-8")
        upper = dockerfile.upper()

        self.assertNotIn("ADD-AGENT", dockerfile)
        self.assertNotIn("TEMPLATE_IDENTIFY", upper)
        self.assertNotIn("CONTAINER-IMAGE/TEMPLATES", upper)
        self.assertNotIn("MODEL_KEY", upper)
        self.assertNotIn("GATEWAY_TOKEN", upper)
        self.assertNotIn("AUTH_TOKEN", upper)
        self.assertIn('io.dola.unitag.image-kind="agent-base"', dockerfile)
        self.assertIn("/opt/unitag/openclaw-seed", dockerfile)
        self.assertIn('npm install -g "@larksuite/cli@${LARKSUITE_CLI_VERSION}"', dockerfile)
        self.assertIn('openclaw plugins install "@tencent-weixin/openclaw-weixin@${WEIXIN_PLUGIN_VERSION}"', dockerfile)
        self.assertIn('openclaw plugins install "@openclaw/qqbot@${QQBOT_PLUGIN_VERSION}"', dockerfile)
        self.assertIn('openclaw plugins install "@openclaw/feishu@${FEISHU_PLUGIN_VERSION}"', dockerfile)
        self.assertIn("openclaw config set tools.web.fetch.useTrustedEnvProxy true --strict-json", dockerfile)
        self.assertIn("--no-install-recommends nginx", dockerfile)

        build_script = (image_directory / "build-base-image.ps1").read_text(encoding="utf-8")
        validation_script = (image_directory / "validate-base-image.ps1").read_text(encoding="utf-8")
        publish_script = (image_directory / "publish-base-image.ps1").read_text(encoding="utf-8")
        self.assertIn("Dockerfile.base", build_script)
        self.assertIn("validate-base-image.ps1", build_script)
        self.assertNotIn("TemplateArchive", build_script)
        self.assertNotIn("TemplateIdentify", build_script)
        self.assertIn("NoPrebuiltAgent=true", validation_script)
        self.assertIn('assert not config.get("agents", {}).get("list", [])', validation_script)
        self.assertIn('config["tools"]["web"]["fetch"]["useTrustedEnvProxy"] is True', validation_script)
        self.assertIn('"--cap-drop", "ALL"', validation_script)
        self.assertIn('"no-new-privileges:true"', validation_script)
        self.assertIn("ImmutableReference=", publish_script)

    def test_agent_image_derives_from_base_and_only_adds_the_template_agent(self):
        image_directory = Path(__file__).resolve().parents[1] / "container-image"
        dockerfile = (image_directory / "Dockerfile.agent").read_text(encoding="utf-8")

        self.assertIn("ARG UNITAG_AGENT_BASE_IMAGE", dockerfile)
        self.assertIn("FROM ${UNITAG_AGENT_BASE_IMAGE}", dockerfile)
        self.assertIn("container-image/templates/${TEMPLATE_IDENTIFY}.zip", dockerfile)
        self.assertIn("agentctl.py add-agent", dockerfile)
        self.assertIn(
            "/home/node/.openclaw/templates/${TEMPLATE_IDENTIFY}.zip",
            dockerfile,
        )
        self.assertIn("rm -rf /opt/unitag/openclaw-seed", dockerfile)
        self.assertIn("cp -a /home/node/.openclaw /opt/unitag/openclaw-seed", dockerfile)
        self.assertNotIn("apt-get", dockerfile)
        self.assertNotIn("npm install", dockerfile)
        self.assertNotIn("openclaw plugins install", dockerfile)

        build_script = (image_directory / "build-agent-image.ps1").read_text(encoding="utf-8")
        publish_script = (image_directory / "publish-agent-image.ps1").read_text(encoding="utf-8")
        self.assertIn("Dockerfile.agent", build_script)
        self.assertIn("validate-prebuilt-image.ps1", build_script)
        self.assertIn("BaseImageReference", build_script)
        self.assertIn("ImmutableReference=", publish_script)

        validation_script = (image_directory / "validate-prebuilt-image.ps1").read_text(encoding="utf-8")
        self.assertIn('config["tools"]["web"]["fetch"]["useTrustedEnvProxy"] is True', validation_script)

    def test_seed_validation_rejects_config_and_profile_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp)
            (seed / "openclaw.json").write_text(
                '{"gateway": {"auth": {"token": "must-not-ship"}}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "gateway.auth.token"):
                assert_seed_has_no_runtime_secrets(seed)

            (seed / "openclaw.json").write_text("{}\n", encoding="utf-8")
            (seed / "auth-profiles.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Secret-bearing file"):
                assert_seed_has_no_runtime_secrets(seed)


if __name__ == "__main__":
    unittest.main()
