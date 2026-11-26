import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from aiohttp import ClientSession, UnixConnector, web

from agent_manage.core.errors import APIError
from agent_manage.core.service import Service
from agent_manage.platform_runtime import SharedPlatformRuntimePool, shared_listen_path
from agent_manage.server import create_app


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0 and Path("/run").is_dir(),
                     "requires a disposable Linux root-owned catalog")
class SharedPlatformRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_socket_is_separate_from_private_state_and_not_peer_writable(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(dir="/run") as directory:
            root = Path(directory)
            state, public = root / "state", root / "export"
            state.mkdir(mode=0o700)
            public.mkdir(mode=0o750)
            pool = SimpleNamespace(bindings=root / "bindings", relays=root / "relays")
            self.assertEqual(shared_listen_path(str(public / "manager.sock"), state, pool), public / "manager.sock")
            with self.assertRaises(ValueError):
                shared_listen_path(str(state / "manager.sock"), state, pool)
            public.chmod(0o770)
            with self.assertRaises(ValueError):
                shared_listen_path(str(public / "manager.sock"), state, pool)

    async def test_one_manager_socket_handshakes_to_two_distinct_native_sockets(self):
        with tempfile.TemporaryDirectory(dir="/run") as directory:
            root = Path(directory)
            bindings, relays = root / "bindings", root / "relays"
            bindings.mkdir()
            relays.mkdir()
            shared = SharedPlatformRuntimePool(root / "shared", "test", {
                "bindingDirectory": str(bindings), "relayDirectory": str(relays)})
            runners, hits, departures = [], [], asyncio.Queue()

            async def publish(runtime, generation):
                mount = root / runtime
                (mount / "config").mkdir(parents=True, exist_ok=True)
                (mount / "workspace").mkdir(exist_ok=True)
                origin = f"https://{runtime}.agent.example"
                cfg = mount / "config/openclaw.json"
                token = f"fixture-{runtime}"
                cfg.write_text(json.dumps({"gateway": {"auth": {"token": token}, "controlUi": {"allowedOrigins": [origin]}},
                    "agents": {"list": [{"id": "main", "workspace": "/home/node/.openclaw/workspace"}]}}))
                native_socket = relays / generation / "openclaw.sock"
                native_socket.parent.mkdir()

                async def protocol(request):
                    self.assertEqual(request.headers["Origin"], origin)
                    ws = web.WebSocketResponse()
                    await ws.prepare(request)
                    await ws.send_json({"event": "connect.challenge", "payload": {"nonce": "fixture"}})
                    async for message in ws:
                        frame = message.json()
                        hits.append((runtime, generation, frame["method"]))
                        if frame["method"] == "connect":
                            self.assertEqual(frame["params"]["auth"]["token"], token)
                            payload = {"protocol": 4, "server": {"version": "2026.7.1-2"}}
                        else:
                            self.assertEqual(frame["method"], "config.get")
                            payload = {"path": "/home/node/.openclaw/openclaw.json",
                                       "hash": hashlib.sha256(cfg.read_bytes()).hexdigest()}
                        await ws.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": payload})
                    departures.put_nowait((runtime, generation))
                    return ws

                app = web.Application()
                app.router.add_get("/", protocol)
                runner = web.AppRunner(app)
                runners.append(runner)
                await runner.setup()
                await web.UnixSite(runner, str(native_socket)).start()
                pending = bindings / (runtime + ".tmp")
                pending.write_text(json.dumps({"instanceIdentify": runtime, "bindingId": generation, "mountRoot": str(mount),
                    "gatewayOriginDomain": "agent.example", "nativeVersion": "2026.7.1-2"}))
                pending.replace(bindings / (runtime + ".json"))

            a, b, a2 = ("1" * 64, "2" * 64, "3" * 64)
            try:
                await publish("alpha", a)
                await publish("beta", b)
                manager = web.AppRunner(create_app(Service(root / "manager"), shared))
                runners.append(manager)
                await manager.setup()
                path = str(root / "manager.sock")
                await web.UnixSite(manager, path).start()
                async with ClientSession(connector=UnixConnector(path=path)) as client:
                    async def ready(runtime, binding, expected):
                        async with client.post("http://manager/v1/platform/readiness",
                                               json={"runtimeId": runtime, "bindingId": binding}) as response:
                            self.assertEqual(response.status, expected, await response.text())

                    async def connect(runtime, binding):
                        ws = await client.ws_connect("http://manager/v1/platform/ws")
                        await ws.send_json({"type": "platform.bind", "runtimeId": runtime, "bindingId": binding})
                        await ws.send_json({"type": "auth", "ticket": "1" * 64})
                        self.assertEqual((await ws.receive_json(timeout=5))["type"], "platform.redeem")
                        await ws.send_json({"type": "platform.grant", "runtimeId": runtime, "bindingId": binding,
                            "issuer": "platform", "tenantId": "owner", "subject": "owner", "nativeAgentId": "main",
                            "agentIdentify": "template", "installationId": "install", "actions": ["chat.instance"],
                            "expiresAt": int(time.time() * 1000) + 60_000})
                        hello = await ws.receive_json(timeout=5)
                        self.assertEqual(hello["type"], "hello", hello)
                        return ws

                    await asyncio.gather(ready("alpha", a, 204), ready("beta", b, 204))
                    self.assertEqual(hits, [])
                    alpha, second, beta = await asyncio.gather(connect("alpha", a), connect("alpha", a), connect("beta", b))
                    await alpha.close()
                    self.assertTrue(departures.empty())
                    await second.close()
                    self.assertEqual(await asyncio.wait_for(departures.get(), 5), ("alpha", a))
                    await publish("alpha", a2)
                    await ready("alpha", a, 404)
                    await asyncio.gather(ready("alpha", a2, 204), ready("beta", b, 204))
                    alpha = await connect("alpha", a2)
                    await beta.send_json({"type": "ping"})
                    self.assertEqual((await beta.receive_json(timeout=5))["type"], "pong")
                    await alpha.close()
                    await beta.close()
                self.assertEqual([hit for hit in hits if hit[0] == "beta"],
                                 [("beta", b, "connect"), ("beta", b, "config.get")])
                self.assertEqual([hit for hit in hits if hit[:2] == ("alpha", a)],
                                 [("alpha", a, "connect"), ("alpha", a, "config.get")])
                self.assertIn(("alpha", a2, "config.get"), hits)
            finally:
                for runner in reversed(runners):
                    await runner.cleanup()

    async def test_rebind_retires_only_one_runtime_and_old_binding_cannot_resume(self):
        with tempfile.TemporaryDirectory(dir="/run") as directory:
            root = Path(directory)
            bindings = root / "bindings"
            relays = root / "relays"
            bindings.mkdir()
            relays.mkdir()

            def publish(runtime, generation, **fields):
                temp = bindings / (runtime + ".next")
                temp.write_text(json.dumps({"instanceIdentify": runtime, "bindingId": generation,
                    "mountRoot": str(root / runtime), "gatewayOriginDomain": "agent.example",
                    "nativeVersion": "2026.7.1-2", **fields}))
                temp.replace(bindings / (runtime + ".json"))

            a, b, a2 = ("a" * 64, "b" * 64, "c" * 64)
            publish("alpha", a)
            publish("beta", b, engine="openclaw")
            publish("delta", "d" * 64, engine="dsh")
            shared = SharedPlatformRuntimePool(root / "private", "test", {
                "bindingDirectory": str(bindings), "relayDirectory": str(relays)})
            try:
                first = shared.select("alpha", a)
                other = shared.select("beta", b)
                self.assertEqual(first.host.origin, "https://alpha.agent.example")
                self.assertEqual(other.host.gateway_socket, str(relays / b / "openclaw.sock"))
                self.assertFalse(shared.admits("alpha", b))
                self.assertFalse(shared.admits("delta", "d" * 64))
                with self.assertRaises(APIError):
                    shared.select("delta", "d" * 64)
                publish("alpha", a2)
                with self.assertRaises(APIError):
                    first.binding_guard()
                with self.assertRaises(APIError):
                    await shared.probe_ready("alpha", a)
                self.assertIs(shared.select("beta", b), other)
                other.binding_guard()
                self.assertIsNot(shared.select("alpha", a2), first)
                await asyncio.gather(*shared.retiring)
                self.assertTrue(first.closed)
                self.assertFalse(other.closed)
                (bindings / "beta.json").chmod(0o666)
                self.assertFalse(shared.admits("beta", b))
                shared.reap()
                self.assertNotIn("beta", shared.pools)
            finally:
                await shared.close()

    async def test_catalog_rejects_symlinks_and_tenant_writable_root(self):
        with tempfile.TemporaryDirectory(dir="/run") as directory:
            root = Path(directory)
            bindings = root / "bindings"
            bindings.mkdir()
            relays = root / "relays"
            relays.mkdir()
            settings = {"bindingDirectory": str(bindings), "relayDirectory": str(relays)}
            binding = bindings / "alpha.json"
            binding.symlink_to("/etc/passwd")
            pool = SharedPlatformRuntimePool(root, "test", settings)
            self.assertFalse(pool.admits("alpha", "a" * 64))
            await pool.close()
            bindings.chmod(0o777)
            with self.assertRaises(ValueError):
                SharedPlatformRuntimePool(root, "test", settings)
