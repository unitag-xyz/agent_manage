import base64
import io
import json
import os
import stat
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_manage.codex_auth import GLOBAL_PROFILE, run_login_worker
from agent_manage.cli import main
from agent_manage.local import LocalRunner
from agent_manage.orchestrator import InstanceManagerV2
from agent_manage.refresh_management import _private_json
import test_codex_management as model_tests


class GlobalCodexTest(unittest.TestCase):
    def setUp(self):
        self.fixture = model_tests.CodexManagementTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.manager = self.fixture.manager
        self.path = self.fixture.path
        self.original = self.path.read_bytes()

    def job(self, status="pending", **extra):
        value = {"id": "test-generation", "config_path": str(self.path), "status": status,
                 "expires_at": int(time.time() * 1000) + 900000, **extra}
        _private_json(self.manager._codex_auth_path("job"), value)
        return value

    def auth(self, available=False, logged_in=False):
        return {"canonical_auth_available": available, "logged_in": logged_in, "auth_status": "valid" if logged_in else "missing",
                "account": None, "agents": []}

    def bridge(self, available=True):
        def call(action, **extra):
            if action == "inspect":
                return self.auth(available)
            if action == "apply":
                _private_json(self.manager._codex_auth_path("transaction"), [])
                return {"credentials_shared": True, "account": {"email": "test@example.com"}}
            if action == "logout":
                return {"credentials_removed": 1}
            if action == "rollback":
                return {"rolled_back": True}
            self.fail(action)
        return call

    def test_adopts_main_auth_globally_and_restores_models_on_logout(self):
        with patch.object(self.manager, "_codex_bridge", side_effect=self.bridge()):
            result = self.manager.codex_login()
            self.assertTrue(result["logged_in"])
            self.assertEqual(result["mode"], "oauth_global")
            config = json.loads(self.path.read_text())
            self.assertEqual(config["auth"]["order"]["openai"], [GLOBAL_PROFILE])
            self.assertEqual(config["models"]["providers"]["openai"]["apiKey"], "test-image-key")
            result = self.manager.codex_logout()
        self.assertEqual(json.loads(self.path.read_text()), json.loads(self.original))
        self.assertEqual(result["status"], "logged_out")
        self.assertEqual(result["credentials_removed"], 1)
        self.assertTrue(result["models_restored"])

    def test_repeated_login_keeps_backup_and_does_not_reinstall_auth(self):
        with patch.object(self.manager, "_codex_bridge", side_effect=self.bridge()):
            self.manager.codex_login()
        before = self.path.read_bytes()
        with patch.object(self.manager, "_codex_bridge", return_value=self.auth(True, True)) as bridge:
            self.assertTrue(self.manager.codex_login()["logged_in"])
        bridge.assert_called_once_with("inspect")
        self.assertEqual(self.path.read_bytes(), before)

    def test_pending_returns_device_code_without_overwriting_models_or_starting_worker(self):
        self.job(user_code="TEST-CODE", verification_url="https://auth.openai.com/codex/device")
        with patch.object(self.manager, "_codex_launch_worker", side_effect=AssertionError("duplicate worker")), \
             patch.object(self.manager, "_codex_bridge", side_effect=AssertionError("unexpected auth")):
            result = self.manager.codex_login()
        self.assertEqual(result["user_code"], "TEST-CODE")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_start_returns_verification_from_worker_and_preserves_config(self):
        def ready(job):
            self.job(user_code="TEST-CODE", verification_url="https://auth.openai.com/codex/device", id=job["id"])
        with patch.object(self.manager, "_codex_bridge", return_value=self.auth()), \
             patch.object(self.manager, "_codex_bridge_input", return_value={}), \
             patch.object(self.manager, "_codex_launch_worker", side_effect=ready) as launch:
            result = self.manager.codex_login()
        self.assertEqual(result["status"], "pending")
        launch.assert_called_once()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.manager._codex_auth_path("job").stat().st_mode), 0o600)

    def test_status_reports_expiry_and_is_fast_without_network_or_model_calls(self):
        self.job(expires_at=0, user_code="EXPIRED")
        before = self.manager._codex_auth_path("job").read_bytes()
        with patch.object(self.manager, "_codex_bridge", return_value=self.auth()) as bridge:
            result = self.manager.codex_status()
        self.assertEqual(result["status"], "expired")
        self.assertNotIn("user_code", result["login"])
        self.assertEqual(self.manager._codex_auth_path("job").read_bytes(), before)
        bridge.assert_called_once_with("inspect")
        self.assertEqual(self.fixture.fixture.runner.calls, [])

    def test_failed_auth_apply_rolls_back_models_and_keeps_secrets_out_of_error(self):
        def fail(action, **extra):
            if action == "inspect":
                return self.auth(True)
            raise RuntimeError("access=SECRET refresh=SECRET")
        with patch.object(self.manager, "_codex_bridge", side_effect=fail):
            with self.assertRaises(RuntimeError) as error:
                self.manager.codex_login()
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.manager._codex_state_path().exists())
        self.assertFalse(self.manager._codex_auth_path("restore").exists())

    def test_concurrent_config_change_is_preserved(self):
        def change(action, **extra):
            if action == "inspect":
                return self.auth(True)
            if action == "apply":
                config = json.loads(self.path.read_text())
                config["gateway"]["port"] = 23456
                self.path.write_text(json.dumps(config))
                return {"credentials_shared": True}
            self.fail(action)
        with patch.object(self.manager, "_codex_bridge", side_effect=change):
            with self.assertRaisesRegex(RuntimeError, "recover"):
                self.manager.codex_login()
        self.assertEqual(json.loads(self.path.read_text())["gateway"]["port"], 23456)
        self.assertTrue(self.manager._codex_state_path().exists())

    def test_logout_cancels_pending_and_worker_cannot_apply_afterwards(self):
        job = self.job(user_code="TEST-CODE")
        with patch.object(self.manager, "_codex_bridge", side_effect=self.bridge()):
            self.manager.codex_logout()
        self.assertEqual(self.manager._codex_auth_read("job")["status"], "cancelled")
        process = unittest.mock.Mock()
        process.stdout = io.StringIO(json.dumps({"event": "credential", "credential": {"access": "SECRET"}}) + "\n")
        with patch.object(InstanceManagerV2, "_codex_bridge_input", return_value={}), \
             patch.object(InstanceManagerV2, "_codex_bridge_process", return_value=process), \
             patch.object(InstanceManagerV2, "_codex_finish_login_locked", side_effect=AssertionError("stale worker")):
            run_login_worker(str(self.path), "openclaw", job["id"])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_worker_success_does_not_store_credentials_in_job(self):
        job = self.job()
        process = unittest.mock.Mock()
        process.stdout = io.StringIO(json.dumps({"event": "credential", "credential": {"access": "SECRET"}}) + "\n")
        process.wait.return_value = 0
        with patch.object(InstanceManagerV2, "_codex_bridge_input", return_value={}), \
             patch.object(InstanceManagerV2, "_codex_bridge_process", return_value=process), \
             patch.object(InstanceManagerV2, "_codex_finish_login_locked", return_value={}) as apply:
            run_login_worker(str(self.path), "openclaw", job["id"])
        apply.assert_called_once_with({"access": "SECRET"})
        self.assertEqual(self.manager._codex_auth_read("job")["status"], "completed")
        self.assertNotIn("SECRET", self.manager._codex_auth_path("job").read_text())

    def test_worker_failure_is_sanitized(self):
        job = self.job()
        with patch.object(InstanceManagerV2, "_codex_bridge_input", side_effect=RuntimeError("SECRET")):
            run_login_worker(str(self.path), "openclaw", job["id"])
        self.assertEqual(self.manager._codex_auth_read("job")["error_code"], "CODEX_AUTH_FAILED")
        self.assertNotIn("SECRET", self.manager._codex_auth_path("job").read_text())

    def test_dry_run_never_starts_auth_or_mutates(self):
        self.manager.runner.dry_run = True
        with patch.object(self.manager, "_codex_bridge", side_effect=AssertionError("native auth")):
            self.assertEqual(self.manager.codex_login()["status"], "preview")
            self.assertTrue(self.manager.codex_logout()["skipped"])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_cli_status(self):
        with patch.object(InstanceManagerV2, "codex_status", return_value={"logged_in": True}) as status, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["codex-status"]), 0)
        status.assert_called_once_with()
        self.assertTrue(json.loads(out.getvalue())["result"]["logged_in"])

    def test_cli_pending_is_accepted(self):
        with patch.object(InstanceManagerV2, "codex_login", return_value={"status": "pending"}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["codex-login"]), 0)
        self.assertEqual(json.loads(out.getvalue())["typeCode"], 2)

    def test_logout_preserves_later_auth_order_and_other_provider(self):
        with patch.object(self.manager, "_codex_bridge", side_effect=self.bridge()):
            self.manager.codex_login()
            config = json.loads(self.path.read_text())
            config["auth"]["order"]["openai"] = ["openai:new-api", GLOBAL_PROFILE]
            config["auth"]["profiles"]["anthropic:new"] = {"provider": "anthropic", "mode": "api_key"}
            self.path.write_text(json.dumps(config))
            self.manager.codex_logout()
        auth = json.loads(self.path.read_text())["auth"]
        self.assertEqual(auth["order"]["openai"], ["openai:new-api"])
        self.assertIn("anthropic:new", auth["profiles"])

    def test_logout_preserves_config_changed_during_auth_validation_and_allows_retry(self):
        with patch.object(self.manager, "_codex_bridge", side_effect=self.bridge()):
            self.manager.codex_login()
            validate = self.manager._codex_validate

            def concurrent_edit(candidate):
                result = validate(candidate)
                if candidate.get("auth", {}).get("order", {}).get("openai") != [GLOBAL_PROFILE]:
                    current = json.loads(self.path.read_text())
                    current["gateway"]["port"] = 23456
                    self.path.write_text(json.dumps(current))
                return result

            with patch.object(self.manager, "_codex_validate", side_effect=concurrent_edit):
                with self.assertRaisesRegex(RuntimeError, "auth restore"):
                    self.manager.codex_logout()
            self.assertEqual(json.loads(self.path.read_text())["gateway"]["port"], 23456)
            self.assertTrue(self.manager._codex_auth_path("restore").exists())
            self.assertEqual(self.manager.codex_logout()["status"], "logged_out")
        self.assertEqual(json.loads(self.path.read_text())["gateway"]["port"], 23456)
        self.assertFalse(self.manager._codex_auth_path("restore").exists())

    def test_restore_without_prior_auth_section_is_exact(self):
        config = json.loads(self.path.read_text())
        config.pop("auth")
        self.path.write_text(json.dumps(config))
        with patch.object(self.manager, "_codex_bridge", side_effect=self.bridge()):
            self.manager.codex_login()
            self.manager.codex_logout()
        self.assertEqual(json.loads(self.path.read_text()), config)

    def test_restore_record_is_environment_scoped_and_not_a_link(self):
        path = self.manager._codex_auth_path("restore")
        _private_json(path, {"config_path": "/other/openclaw.json"})
        with self.assertRaisesRegex(ValueError, "another environment"):
            self.manager._codex_auth_read("restore")
        path.unlink()
        path.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "link"):
            self.manager._codex_auth_read("restore")

    def test_worker_old_generation_cannot_overwrite_new_login(self):
        job = self.job(id="new-generation")
        process = unittest.mock.Mock()
        process.stdout = io.StringIO(json.dumps({"event": "verification", "user_code": "OLD-CODE"}) + "\n")
        with patch.object(InstanceManagerV2, "_codex_bridge_input", return_value={}), \
             patch.object(InstanceManagerV2, "_codex_bridge_process", return_value=process):
            run_login_worker(str(self.path), "openclaw", "old-generation")
        self.assertEqual(self.manager._codex_auth_read("job"), job)

    def test_interrupted_native_transaction_requires_logout(self):
        _private_json(self.manager._codex_auth_path("transaction"), [])
        with patch.object(self.manager, "_codex_bridge", side_effect=AssertionError("auth must not start")):
            with self.assertRaisesRegex(FileExistsError, "codex-logout"):
                self.manager.codex_login()

    def test_runtime_resolution_follows_server_symlink_and_rejects_newer_version(self):
        package = self.path.parent / "fake-openclaw"
        package.mkdir()
        metadata = package / "package.json"
        binary = package / "openclaw.mjs"
        binary.write_text("#!/usr/bin/env node\n")
        binary.chmod(0o755)
        link = self.path.parent / "openclaw"
        link.symlink_to(binary)
        self.manager.runner = LocalRunner(str(link))
        self.manager.bin = str(link)
        for version in ("2026.7.1", "2026.7.1-1", "2026.7.1-2"):
            with self.subTest(version=version):
                metadata.write_text(json.dumps({"name": "openclaw", "version": version}))
                self.assertEqual(self.manager._codex_bridge_input("inspect")["package"], str(package))
        for name, version in (("openclaw", "2026.9.2"), ("other-package", "2026.7.1")):
            with self.subTest(name=name, version=version):
                metadata.write_text(json.dumps({"name": name, "version": version}))
                with self.assertRaisesRegex(ValueError, "server OpenClaw") as error:
                    self.manager._codex_bridge_input("inspect")
                self.assertIn(repr(name), str(error.exception))
                self.assertIn(repr(version), str(error.exception))
                self.assertIn(str(package), str(error.exception))


@unittest.skipUnless(os.environ.get("AGENT_MANAGE_TEST_OPENCLAW_BIN"), "Set pinned server OpenClaw for native auth tests")
class NativeGlobalCodexTest(unittest.TestCase):
    def setUp(self):
        GlobalCodexTest.setUp(self)
        self.manager.runner = LocalRunner(os.environ["AGENT_MANAGE_TEST_OPENCLAW_BIN"])
        self.manager.bin = os.environ["AGENT_MANAGE_TEST_OPENCLAW_BIN"]

    def test_native_sqlite_shared_auth_and_logout_preserve_api_keys(self):
        second = self.path.parent / "agents/second/agent/auth-profiles.json"
        second.parent.mkdir(parents=True)
        second.write_text(json.dumps({"version": 1, "profiles": {
            "openai:other": {"type": "oauth", "provider": "openai", "access": "other-test-access", "refresh": "other-test-refresh", "expires": 4102444800000},
            "openai:api": {"type": "api_key", "provider": "openai", "key": "fake-media-key"},
            "anthropic:api": {"type": "api_key", "provider": "anthropic", "key": "fake-unrelated-key"}},
            "order": {"openai": ["openai:other", "openai:api"]}}))
        import subprocess
        payload = self.manager._codex_bridge_input("inspect")
        # Seed through the actual server SDK; this version uses SQLite, not legacy auth-profiles.json.
        seed = """
const sdk=await import(process.argv[1]+'/dist/plugin-sdk/provider-auth.js');
const fs=await import('node:fs');
for(const dir of process.argv.slice(2)){
 const saved=JSON.parse(fs.readFileSync(dir+'/auth-profiles.json','utf8'));
 const result=await sdk.updateAuthProfileStoreWithLock({agentDir:dir,updater:s=>{Object.assign(s,saved);return true;},saveOptions:{syncExternalCli:false,filterExternalAuthProfiles:false}});
 if(!result)process.exit(1);
}
"""
        subprocess.run(["node", "--input-type=module", "-e", seed, payload["package"], payload["main"], str(second.parent)],
                       check=True, capture_output=True, env={**os.environ, "OPENCLAW_STATE_DIR": str(self.path.parent)})
        self.assertTrue(self.manager._codex_bridge("inspect")["canonical_auth_available"])
        with patch.object(self.manager, "_codex_launch_worker", side_effect=AssertionError("No real device auth allowed in this test")):
            result = self.manager.codex_login()
        self.assertTrue(result["credentials_shared"])
        status = self.manager.codex_status()
        self.assertTrue(status["logged_in"])
        self.assertTrue(all(agent["shared_auth"] for agent in status["agents"]))
        self.assertNotIn("test-existing-access", json.dumps(status))
        self.assertNotIn("test-existing-refresh", json.dumps(status))
        # Exercise the pinned image plugin with a mocked service response:
        # a retained media API key must not take precedence over global OAuth.
        routing = self.path.parent / "check-media-routing.mjs"
        routing.write_text("""
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const cfg=JSON.parse(fs.readFileSync(process.env.OPENCLAW_CONFIG_PATH,'utf8'));
let imageCalls=0,audioCalls=0;
globalThis.fetch=async(url,opts)=>{
 const headers=new Headers(opts.headers);
 if(String(url)==='https://chatgpt.com/backend-api/codex/responses'){
   if(headers.get('authorization')!=='Bearer test-existing-access')throw new Error('Wrong image credential');
   const body=JSON.parse(opts.body);
   if(body.tools[0].type!=='image_generation'||body.tools[0].model!=='gpt-image-2')throw new Error('Wrong image tool');
   imageCalls++;
   return new Response('data: '+JSON.stringify({type:'response.completed',response:{output:[{type:'image_generation_call',result:'aW1hZ2U='}]}})+'\\n\\n',{headers:{'content-type':'text/event-stream'}});
 }
 if(String(url)==='https://api.dola.io/aigateway/mystore/v1/audio/transcriptions'){
   if(headers.get('authorization')!=='Bearer test-image-key')throw new Error('Wrong audio credential');
   audioCalls++;
   return new Response(JSON.stringify({text:'mock transcript'}),{headers:{'content-type':'application/json'}});
 }
 throw new Error('Unexpected media endpoint');
};
// The SDK deliberately uses undici unless an injected fetch is marked as mocked.
globalThis.fetch.mock={};
const imageModule=await import(pathToFileURL(process.argv[2]+'/dist/extensions/openai/image-generation-provider.js'));
const image=await imageModule.buildOpenAIImageGenerationProvider().generateImage({
 cfg,agentDir:process.argv[3],model:'gpt-image-2',prompt:'test',quality:'low'});
if(image.images.length!==1)throw new Error('Missing mock image');
const audioModule=await import(pathToFileURL(process.argv[2]+'/dist/extensions/openai/media-understanding-provider.js'));
const entry=cfg.tools.media.audio.models[0];
const audio=await audioModule.transcribeOpenAiAudio({buffer:Buffer.alloc(2048),fileName:'test.wav',mime:'audio/wav',
 apiKey:cfg.models.providers.openai.apiKey,baseUrl:entry.baseUrl,model:entry.model,timeoutMs:5000});
if(audio.text!=='mock transcript'||imageCalls!==1||audioCalls!==1)throw new Error('Media route failed');
""")
        media_check = subprocess.run(["node", str(routing), payload["package"], payload["main"]], capture_output=True,
                                    env={**os.environ, "OPENCLAW_STATE_DIR": str(self.path.parent),
                                         "OPENCLAW_CONFIG_PATH": str(self.path)}, timeout=20)
        self.assertEqual(media_check.returncode, 0, media_check.stderr.decode())
        # New agents inherit the main account without copying the refresh token.
        config = json.loads(self.path.read_text())
        config["agents"]["list"].append({"id": "new"})
        self.path.write_text(json.dumps(config))
        self.assertTrue(self.manager.codex_status()["logged_in"])
        self.assertEqual(self.manager.codex_logout()["credentials_removed"], 1)
        self.assertFalse(self.manager.codex_status()["logged_in"])
        # Inspect native raw local stores via the same pinned public SDK, without external auth sync.
        payload = self.manager._codex_bridge_input("inspect")
        script = self.path.parent / "check-store.mjs"
        script.write_text("""
import {pathToFileURL} from 'node:url';
const s=await import(pathToFileURL(process.argv[2]+'/dist/plugin-sdk/provider-auth.js'));
const store=await s.updateAuthProfileStoreWithLock({agentDir:process.argv[3],updater:()=>false});
if(store.profiles['openai:api']?.key!=='fake-media-key'||store.profiles['anthropic:api']?.key!=='fake-unrelated-key')process.exit(1);
if(Object.values(store.profiles).some(c=>c.type==='oauth'))process.exit(2);
if(JSON.stringify(store.order?.openai)!=='["openai:api"]')process.exit(3);
""")
        subprocess.run(["node", str(script), payload["package"], str(second.parent)], check=True,
                       env={**os.environ, "OPENCLAW_STATE_DIR": str(self.path.parent)}, capture_output=True)

    def test_actual_device_worker_with_mocked_openai_responses(self):
        self.fixture.auth_file.unlink()
        claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "fake-account", "chatgpt_plan_type": "plus"},
                  "https://api.openai.com/profile": {"email": "fake@example.com"}, "exp": 4102444800}
        access = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fake"
        mock = self.path.parent / "mock-openai.mjs"
        mock.write_text("""
globalThis.fetch = async (url,opts) => {
 if(url.endsWith('/deviceauth/usercode'))return new Response(JSON.stringify({device_auth_id:'fake-device',user_code:'TEST-CODE',interval:1}));
 if(url.endsWith('/deviceauth/token')){
   await new Promise(r=>setTimeout(r,1500));
   return new Response(JSON.stringify({authorization_code:'fake-code',code_verifier:'fake-verifier'}));
 }
 if(url.endsWith('/oauth/token'))return new Response(JSON.stringify({access_token:ACCESS,refresh_token:'fake-refresh',expires_in:3600}));
 throw new Error('Unexpected URL');
};
""".replace("ACCESS", json.dumps(access)))
        with patch.dict(os.environ, {"NODE_OPTIONS": "--import=" + str(mock)}):
            result = self.manager.codex_login()
            self.assertIn(result["status"], {"starting", "pending"})
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                job = self.manager._codex_auth_read("job")
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "completed", job)
            status = self.manager.codex_status()
        self.assertTrue(status["logged_in"])
        self.assertEqual(status["account"]["email"], "fake@example.com")
        self.assertEqual(status["account"]["account_id"], "fake-account")
        self.assertNotIn(access, json.dumps(status))
        self.assertEqual(self.manager.codex_logout()["status"], "logged_out")


if __name__ == "__main__":
    unittest.main()
