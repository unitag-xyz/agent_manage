"""Exercise the extension points without a second production engine."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, UnixConnector, web

from agent_manage.adapters.base import HostIsolation
from agent_manage.adapters.registry import ENGINES, EngineDriver
from agent_manage.adapters.versions import VersionFamily, select_family
from agent_manage.core.errors import APIError
from agent_manage.core.runtime import RuntimeContext
from agent_manage.platform_runtime import SharedPlatformRuntimePool
from agent_manage.server import build_service, create_app
from test_cli_engine_dispatch import run_cli


FAMILIES = (
    VersionFamily("v1", lambda version: version in ("1", "2"), frozenset({"read"})),
    VersionFamily("v1", lambda version: version == "1", frozenset({"write"})),
    VersionFamily("v2", lambda version: version == "2", frozenset({"write"})),
)


class FixtureExecution:
    restart_required = False
    supported = True
    engine = "fixture"

    def __init__(self, registration, calls):
        self.registration = registration
        self.calls = calls
        self.runtime_id = registration["id"]
        self.version = registration["nativeVersion"]
        root = Path(registration["configPath"]).parent
        self.context = RuntimeContext(self.runtime_id, self.runtime_id, root / "config", root,
                                      executable="fixture", home=root)
        self.instance_chat = "bindingId" in registration
        self.host_isolation = (HostIsolation(registration["bindingId"], (root,)) if self.instance_chat else None)

    def operation(self, name):
        capability = {"get_current_model": "read", "list_threads": "read", "set_model": "write"}[name]
        family = select_family(FAMILIES, self.version, capability)
        if family is None:
            raise APIError("UNSUPPORTED_VERSION", "No compatible fixture implementation")

        def execute(*_):
            self.calls.append((self.version, family, name))
            if name == "set_model":
                raise APIError("DISPATCH_UNKNOWN", "Fixture write may have been applied")
            return {"version": self.version, "implementation": family}
        return execute

    def resolve_scope(self, method, params):
        return params.get("agentId")

    async def invoke(self, method, params):
        operation = {"models.get": "get_current_model", "threads.list": "list_threads", "models.select": "set_model"}[method]
        return self.operation(operation)()

    async def close(self):
        pass


class FixtureHost:
    profile_fields = shared_profile_fields = {"nativeVersion"}

    @staticmethod
    def shared_profile(profile, relay_root):
        return profile

    def __init__(self, profile, mount, relay_root):
        self.profile, self.mount = profile, mount

    def registration(self, grant):
        return {"id": self.profile["instanceIdentify"], "bindingId": self.profile["bindingId"],
                "nativeVersion": self.profile["nativeVersion"], "configPath": str(self.mount / "config")}

    def prepare(self, adapter, *, idle):
        assert adapter.host_isolation.binding_id == self.profile["bindingId"]

    async def verify_grant(self, grant):
        assert grant.runtime_id == self.profile["instanceIdentify"]

    async def resolve_grant_agent(self, adapter, workers, grant):
        return "fixture-agent"

    async def connect(self, adapter):
        pass

    async def probe_shared_relay(self):
        pass

    def resume(self, adapter):
        pass

    def pause(self, adapter):
        pass

    async def disconnect(self, adapter):
        pass


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "needs trusted Linux /run")
class EngineExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_versions_across_cli_http_ws_and_shared_host(self):
        with tempfile.TemporaryDirectory(dir="/run") as directory:
            root = Path(directory)
            for name in ("bindings", "relays"):
                (root / name).mkdir()
            calls = []
            generation = "1" * 64
            (root / "bindings/host.json").write_text(json.dumps({
                "instanceIdentify": "host", "bindingId": generation, "mountRoot": str(root / "host"),
                "engine": "fixture", "nativeVersion": "2",
            }))
            registrations = [{"id": f"fixture{version}", "engine": "fixture", "nativeVersion": version,
                              "configPath": str(root / f"version{version}" / "config")}
                             for version in ("1", "2", "unknown")]
            driver = EngineDriver(lambda _: FixtureExecution(registrations[1], calls),
                                  lambda registration, *_: FixtureExecution(registration, calls), FixtureHost)
            with patch.dict(ENGINES, fixture=driver), patch.dict(os.environ, {"UNITAG_AGENT_MANAGER_ENGINE": "fixture"}):
                code, local = run_cli(["current-model"])
                self.assertEqual(code, 0)

                service = build_service({"stateDirectory": str(root / "state"), "runtimes": registrations})
                pool = SharedPlatformRuntimePool(root / "state", "test", {
                    "bindingDirectory": str(root / "bindings"), "relayDirectory": str(root / "relays"),
                })
                runner = web.AppRunner(create_app(service, pool))
                await runner.setup()
                try:
                    socket_path = str(root / "manager.sock")
                    await web.UnixSite(runner, socket_path).start()
                    async with ClientSession(connector=UnixConnector(path=socket_path)) as client:
                        ticket = service.auth.issue_ticket(issuer="test", subject="user", runtime_ids=["*"],
                                                           actions=["models.get", "models.select", "operations.get"])

                        def rpc(method, runtime, **params):
                            return {"type": "request", "requestId": method, "method": method,
                                    "params": {"runtimeId": runtime, **params}}

                        async with client.ws_connect("http://manager/v1/ws") as ws:
                            await ws.send_json({"type": "auth", "ticket": ticket})
                            hello = await ws.receive_json(timeout=5)
                            self.assertEqual(hello["type"], "hello", hello)
                            headers = {"Authorization": "Bearer " + hello["leaseToken"]}

                            async def http(request):
                                async with client.post("http://manager/v1/rpc", headers=headers, json=request) as response:
                                    return await response.json()

                            self.assertEqual((await http(rpc("models.get", "fixture1")))["result"],
                                             {"version": "1", "implementation": "v1"})
                            self.assertEqual((await http(rpc("models.get", "fixture2")))["result"], local["result"])
                            await ws.send_json(rpc("models.get", "fixture2"))
                            self.assertEqual((await ws.receive_json(timeout=5))["result"], local["result"])
                            denied = await http(rpc("models.get", "fixtureunknown"))
                            self.assertEqual(denied["error"]["code"], "UNSUPPORTED_VERSION")
                            write = rpc("models.select", "fixture2", clientRequestId="write-once", modelId="test")
                            await ws.send_json(write)
                            identifier = (await ws.receive_json(timeout=5))["result"]["operationId"]
                            await asyncio.wait_for(asyncio.gather(*tuple(service.tasks)), 5)
                            self.assertEqual((await http(write))["result"]["operationId"], identifier)
                            result = (await http(rpc("operations.get", "fixture2", operationId=identifier)))["result"]
                            self.assertEqual(result["status"], "unknown")
                            self.assertEqual(result["error"]["code"], "DISPATCH_UNKNOWN")
                            self.assertEqual([call for call in calls if call[2] == "set_model"], [("2", "v2", "set_model")])

                        async with client.post("http://manager/v1/platform/readiness",
                                               json={"runtimeId": "host", "bindingId": generation}) as response:
                            self.assertEqual(response.status, 204)
                        async with client.ws_connect("http://manager/v1/platform/ws") as ws:
                            await ws.send_json({"type": "platform.bind", "runtimeId": "host", "bindingId": generation})
                            await ws.send_json({"type": "auth", "ticket": "2" * 64})
                            self.assertEqual((await ws.receive_json(timeout=5))["type"], "platform.redeem")
                            await ws.send_json({"type": "platform.grant", "runtimeId": "host", "bindingId": generation,
                                "issuer": "test", "subject": "user", "tenantId": "test", "nativeAgentId": "fixture",
                                "agentIdentify": "fixture", "installationId": "install", "actions": ["chat.instance"],
                                "expiresAt": int(time.time() * 1000) + 60_000})
                            hello = await ws.receive_json(timeout=5)
                            self.assertEqual(hello["type"], "hello", hello)
                            await ws.send_json(rpc("threads.list", "host", agentId="fixture-agent"))
                            self.assertEqual((await ws.receive_json(timeout=5))["result"], local["result"])
                    self.assertFalse((root / "host/.openclaw").exists())
                finally:
                    await runner.cleanup()
