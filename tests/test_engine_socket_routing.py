"""Run both transports against a test-only engine without any OpenClaw runtime."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_manage.adapters.registry import ENGINES, EngineDriver
from agent_manage.core.runtime import RuntimeContext
from test_cli_engine_dispatch import RecordingExecution, run_cli

try:
    from aiohttp import ClientSession, UnixConnector, web
    from agent_manage.server import build_service, create_app
except ModuleNotFoundError:
    web = None


class FixtureRuntime:
    engine = "fixture"
    version = "1"
    supported = True

    def __init__(self, registration, execution):
        self.registration = registration
        self.runtime_id = registration["id"]
        path = Path(registration["configPath"])
        self.context = RuntimeContext(self.runtime_id, self.runtime_id, path, path.parent)
        self.execution = execution

    def resolve_scope(self, method, params):
        return None

    async def invoke(self, method, params):
        return self.execution.operation({"models.get": "get_current_model", "models.list": "get_supported_models"}[method])()

    async def close(self):
        pass


@unittest.skipUnless(web and os.name == "posix" and os.geteuid() == 0, "needs server dependencies and trusted Linux /run")
class EngineSocketRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_and_socket_use_bound_engine_and_never_fallback(self):
        with tempfile.TemporaryDirectory(dir="/run") as tmp:
            root = Path(tmp)
            generation = "a" * 64
            binding = root / "one.json"
            profile = {"instanceIdentify": "one", "mountRoot": str(root / "mount"),
                       "bindingId": generation, "engine": "fixture"}
            binding.write_text(json.dumps(profile))
            execution = RecordingExecution()
            driver = EngineDriver(lambda options: execution,
                                  lambda r, *_: FixtureRuntime(r, execution),
                                  type("FixtureProfile", (), {"shared_profile_fields": set()}))
            registration = {"id": "one", "environmentId": "one", "engine": profile["engine"],
                            "configPath": str(root / "tenant" / "never-created")}
            env = {"UNITAG_AGENT_MANAGER_RUNTIME": "container", "UNITAG_AGENT_MANAGER_BINDING_FILE": str(binding),
                   "UNITAG_AGENT_MANAGER_BINDING_ID": generation}
            with patch.dict(ENGINES, fixture=driver), patch.dict(os.environ, env):
                code, local = run_cli(["current-model"])
                self.assertEqual(code, 0)
                for change in ({"UNITAG_AGENT_MANAGER_ENGINE": "openclaw"}, {"UNITAG_AGENT_MANAGER_BINDING_ID": "b"*64}):
                    with patch.dict(os.environ, change):
                        self.assertEqual(run_cli(["current-model"])[0], 1)
                self.assertEqual(run_cli(["--config-path", "/other", "current-model"])[0], 1)
                binding.chmod(0o666)
                self.assertEqual(run_cli(["current-model"])[0], 1)
                binding.chmod(0o600)
                service = build_service({"stateDirectory": str(root / "manager"), "runtimes": [registration]})
                runner = web.AppRunner(create_app(service))
                await runner.setup()
                try:
                    path = root / "manager" / "manager.sock"
                    await web.UnixSite(runner, str(path)).start()
                    ticket = service.auth.issue_ticket(issuer="test", subject="user", runtime_ids=["one"],
                                                       actions=["models.get", "models.list"])
                    async with ClientSession(connector=UnixConnector(path=str(path))) as client:
                        async with client.ws_connect("http://manager/v1/ws") as ws:
                            await ws.send_json({"type": "auth", "ticket": ticket})
                            self.assertEqual((await ws.receive_json(timeout=5))["type"], "hello")
                            async def rpc(method, **params):
                                await ws.send_json({"type": "request", "method": method, "requestId": method,
                                                    "params": {"runtimeId": "one", **params}})
                                return await ws.receive_json(timeout=5)
                            self.assertEqual((await rpc("models.get"))["result"], local["result"])
                            self.assertEqual((await rpc("models.list"))["error"]["code"], "UNSUPPORTED")
                            self.assertFalse((await rpc("models.get", engine="openclaw"))["ok"])
                    self.assertEqual(execution.calls, ["get_current_model", "get_current_model"])
                finally:
                    await runner.cleanup()
