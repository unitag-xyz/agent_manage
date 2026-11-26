import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from aiohttp import ClientSession, UnixConnector, web
    from aiohttp.test_utils import TestServer
    from agent_manage.server import build_service, create_app
    SERVER_AVAILABLE = True
except ModuleNotFoundError:
    SERVER_AVAILABLE = False

from agent_manage.core.auth import LeaseAuthority
from agent_manage.core.errors import APIError
from agent_manage.core.store import Store


class StoreTest(unittest.TestCase):
    def test_schema_one_migrates_without_exposing_unscoped_operations(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "state"
            root.mkdir(mode=0o700)
            with sqlite3.connect(root / "manager.sqlite3") as db:
                db.executescript("""CREATE TABLE operations (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, request_key TEXT NOT NULL,
                    digest TEXT NOT NULL, runtime TEXT NOT NULL, method TEXT NOT NULL,
                    status TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    result TEXT, error TEXT, UNIQUE(scope,request_key)); PRAGMA user_version=1;""")
            store = Store(root)
            owner = {"issuer": "dola", "subject": "user"}
            op, _ = store.claim(owner, "runtime", "method", "old", {})
            self.assertIsNone(op["agentId"])
            self.assertEqual(store.operations(owner, "runtime", agent_ids=["agent"]), [])
            self.assertEqual(len(Store(root).operations(owner, "runtime", agent_ids=["*"])), 1)

    def test_lookup_filters_owner_and_request_before_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary).resolve() / "state")
            owner = {"issuer": "dola", "subject": "one"}
            store.claim(owner, "runtime", "method", "old", {})
            for number in range(205):
                store.claim({"issuer": "dola", "subject": "other"}, "runtime", "method", str(number), {})
            self.assertEqual(len(store.operations(owner, "runtime", "old")), 1)
            self.assertEqual(len(store.operations(owner, "runtime")), 1)

    def test_atomic_single_use_ticket_and_idempotency_survive_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "state"
            store = Store(root)
            authority = LeaseAuthority(store, "target")
            ticket = authority.issue_ticket(issuer="dola", subject="user", runtime_ids=["one"], actions=["*"])
            lease = authority.redeem(ticket)
            with self.assertRaises(APIError):
                authority.redeem(ticket)
            operation, created = store.claim(lease.context, "one", "runs.create", "key", {"message": "hello"})
            self.assertTrue(created)
            store.finish(operation["operationId"], "running")
            reopened = Store(root)
            reopened.recover_operations()
            other, created = reopened.claim(lease.context, "one", "runs.create", "key", {"message": "hello"})
            self.assertFalse(created)
            self.assertEqual(other["status"], "unknown")
            with self.assertRaises(APIError) as raised:
                reopened.claim(lease.context, "one", "runs.create", "key", {"message": "changed"})
            self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")
            authority.revoke(lease.context["leaseId"])
            with self.assertRaises(APIError):
                authority.authenticate(lease.token)


@unittest.skipUnless(SERVER_AVAILABLE, "Install the server extra to test adapter selection")
class AdapterSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_openclaw_uses_the_installed_version_of_each_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runtimes = []
            for identifier, version in (("tested", "2026.7.1-2"), ("july", "2026.7.1-35"), ("future", "2026.8.1")):
                package = root / identifier
                package.mkdir()
                executable = package / "openclaw"
                executable.touch(mode=0o755)
                (package / "package.json").write_text(json.dumps({"name": "openclaw", "version": version}))
                registration = {"id": identifier, "environmentId": identifier, "engine": "openclaw",
                                "executable": str(executable), "configPath": str(package / "state/openclaw.json")}
                if identifier == "future":
                    registration["gatewayUrl"] = "ws://127.0.0.1:18789"
                runtimes.append(registration)

            with patch("agent_manage.adapters.openclaw.v2026_7.adapter.Gateway") as gateway:
                service = build_service({"stateDirectory": str(root / "manager"), "runtimes": runtimes})
                gateway.assert_not_called()
            try:
                tested = service.adapter("tested")
                self.assertEqual(tested.capabilities()["adapter"], "openclaw-july")
                self.assertEqual(tested.capabilities()["validation"], "native-tested")
                july = service.adapter("july")
                self.assertEqual(july.capabilities()["adapter"], "openclaw-july")
                self.assertEqual(july.capabilities()["validation"], "unverified")
                future = service.adapter("future")
                self.assertFalse(future.supported)
                self.assertEqual(future.version, "2026.8.1")
                self.assertIsNone(future.capabilities()["adapter"])
                self.assertEqual(future.capabilities()["methods"]["agents.list"]["support"], "unsupported")
                with self.assertRaises(APIError) as error:
                    await future.invoke("agents.list", {})
                self.assertEqual(error.exception.code, "UNSUPPORTED_VERSION")
            finally:
                await service.close()


@unittest.skipUnless(SERVER_AVAILABLE, "Install the server extra to test HTTP/WSS adapters")
class APITest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.native_calls, self.native_connections = [], []
        self.native_delay = None
        app = web.Application()
        app.router.add_get("/", self.native_handler)
        self.native = TestServer(app)
        await self.native.start_server()
        package = self.root / "package"
        package.mkdir()
        (package / "package.json").write_text(json.dumps({"name": "openclaw", "version": "2026.7.1-2"}))
        binary = package / "openclaw"
        binary.write_text("#!/bin/sh\nprintf '{}\\n'\n")
        binary.chmod(0o755)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.workspace = self.runtime / "workspace"
        self.workspace.mkdir()
        self.media = b"image-data-" * 200000
        (self.workspace / "image.png").write_bytes(self.media)
        self.config = self.runtime / "openclaw.json"
        self.config.write_text(json.dumps({"gateway": {"auth": {"token": "fixture-native-token"}},
            "agents": {"list": [{"id": "main", "workspace": str(self.workspace)}, {"id": "other", "workspace": str(self.workspace)}]}}))
        self.sessions = self.runtime / "agents/main/sessions"
        self.sessions.mkdir(parents=True)
        self.key = "agent:main:dashboard:fixture"
        self.physical_ids = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
        self.index_path = self.sessions / "sessions.json"
        self.index_path.write_text(json.dumps({self.key: {"sessionId": self.physical_ids[1], "usageFamilySessionIds": self.physical_ids,
                                                        "updatedAt": 42, "label": "unchanged"}}))
        for index, identifier in enumerate(self.physical_ids):
            rows = [self.record("u1", "user", [{"type": "text", "text": "question"}], None),
                    self.record("a1", "assistant", [{"type": "thinking", "thinking": "visible reasoning", "thinkingSignature": "secret-replay"},
                        {"type": "text", "text": "x" * 3001 + "\nMEDIA:" + str(self.workspace / "image.png")},
                        {"type": "toolCall", "id": "tc", "name": "exec", "arguments": {"command": "echo hello", "code": "huge-code"}}], "u1")]
            (self.sessions / (identifier + (".jsonl.reset.2026-10-12T02-00-00.000Z" if index == 0 else ".jsonl"))).write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.service = build_service({"stateDirectory": str(self.root / "manager"), "runtimes": [{"id": "one", "environmentId": "env", "engine": "openclaw",
            "configPath": str(self.config), "executable": str(binary), "gatewayUrl": str(self.native.make_url("/")).replace("http:", "ws:")}]})
        self.adapter = self.service.adapters["one"]
        socket_path = self.service.store.path.with_name("manager.sock")
        self.manager_runner = web.AppRunner(create_app(self.service))
        await self.manager_runner.setup()
        await web.UnixSite(self.manager_runner, str(socket_path)).start()
        self.client = ClientSession(base_url="http://agent-manager", connector=UnixConnector(path=str(socket_path)))
        self.lease = self.service.auth.redeem(self.ticket())
        self.agent = self.adapter.agents()[0]["agentId"]
        self.thread = self.adapter.threads(self.agent)[0]["threadId"]
        self.segment = self.adapter.segment_id(self.thread, self.physical_ids[0])
        self.current_segment = self.adapter.segment_id(self.thread, self.physical_ids[1])
        self.counter = 0

    async def asyncTearDown(self):
        await self.client.close()
        await self.manager_runner.cleanup()
        await self.native.close()
        self.tmp.cleanup()

    def ticket(self, **changes):
        return self.service.auth.issue_ticket(**{"issuer": "dola", "subject": "user", "runtime_ids": ["one"], "actions": ["*"], **changes})

    @staticmethod
    def record(identifier, role, content, parent):
        return {"type": "message", "id": identifier, "parentId": parent, "timestamp": "2026-10-01T00:00:00Z",
                "message": {"role": role, "content": content}}

    async def native_handler(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.native_connections.append(socket)
        await socket.send_json({"type": "event", "event": "connect.challenge", "payload": {"nonce": "fixture-nonce"}})
        async for item in socket:
            frame = json.loads(item.data)
            self.native_calls.append(frame)
            method, params = frame["method"], frame["params"]
            result = {}
            if method == "connect":
                self.assertEqual(params["minProtocol"], 4)
                self.assertEqual(params["device"]["nonce"], "fixture-nonce")
                self.assertEqual(params["auth"]["token"], "fixture-native-token")
                result = {"protocol": 4, "server": {"version": "2026.7.1-2"}, "features": {"methods": ["config.get", "chat.send", "chat.abort", "sessions.create", "sessions.reset", "sessions.patch", "sessions.delete", "sessions.describe", "sessions.list", "models.list"]}}
            if method == "config.get":
                result = {"path": str(self.config)}
            if method == "sessions.create":
                result = {"key": f"agent:{params['agentId']}:dashboard:created-by-fixture"}
            if method == "chat.send":
                if self.native_delay:
                    await self.native_delay.wait()
                result = {"runId": params["idempotencyKey"], "status": "started"}
            if method == "chat.abort":
                result = {"aborted": True, "runIds": [params.get("runId")]}
            if method == "sessions.reset":
                index = json.loads(self.index_path.read_text())
                index[self.key]["sessionId"] = "33333333-3333-4333-8333-333333333333"
                self.index_path.write_text(json.dumps(index))
            if method == "models.list":
                self.assertEqual({"view": "configured"}, params)
                result = {"models": [{"id": model, "provider": "fixture", "name": model}
                                     for model in ("default", "alternate")]}
            if method == "sessions.list":
                result = {"sessions": [{"key": params["search"], "hasActiveRun": getattr(self, "model_busy", False)}]}
            if method in ("sessions.describe", "sessions.patch") and (method == "sessions.describe" or "model" in params or "thinkingLevel" in params):
                index = json.loads(self.index_path.read_text())
                entry = index[params["key"]]
                if method == "sessions.patch":
                    for field, parameter in (("modelOverride", "model"), ("thinkingLevel", "thinkingLevel")):
                        if parameter not in params:
                            continue
                        if params[parameter] is None:
                            entry.pop(field, None)
                        else:
                            entry[field] = params[parameter].split("/", 1)[-1]
                    self.index_path.write_text(json.dumps(index))
                effective = {"modelProvider": "fixture", "model": entry.get("modelOverride", "default")}
                if getattr(self, "thinking_supported", False):
                    effective.update(thinkingDefault="low", thinkingLevels=[{"id": "low"}, {"id": "high", "label": "High"}])
                result = ({"key": params["key"], "entry": entry, "resolved": effective} if method == "sessions.patch"
                          else {"session": effective})
            await socket.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": result})
        return socket

    async def rpc(self, method, params=None, lease=None):
        self.counter += 1
        response = await self.client.post("/v1/rpc", json={"requestId": str(self.counter), "method": method, "params": {"runtimeId": "one", **(params or {})}},
            headers={"Authorization": "Bearer " + (lease or self.lease).token})
        return await response.json()

    async def wait_operation(self, identifier):
        for _ in range(200):
            result = self.service.store.operation(identifier, self.lease.context, "one")
            if result["status"] not in ("running", "accepted"):
                return result
            await asyncio.sleep(.005)
        self.fail("operation did not finish")

    async def test_history_reconciles_explicit_user_record_ids_on_compact_and_full_reads(self):
        native = "history-input"
        rid = self.service.store.object_id("run", "one", native)
        self.service.store.object("run", "one", native, {"runId": rid, "threadId": self.thread,
            "segmentId": self.current_segment, "status": "completed"})
        path = self.sessions / (self.physical_ids[1] + ".jsonl")
        big = self.record("stable-user", "user", "x" * 700000, "a1")
        big["message"]["idempotencyKey"] = native + ":user"
        with path.open("a") as fp:
            fp.write(json.dumps(big) + "\n")
        params = {"threadId": self.thread, "segmentId": self.current_segment}
        response = await self.rpc("messages.list", {**params, "limit": 1})
        self.assertTrue(response["ok"], response)
        msg, = response["result"]["items"]
        self.assertEqual(("stable-user", "reference"), (msg["id"], msg["completeness"]))
        self.assertNotIn("idempotencyKey", msg)
        self.assertEqual("stable-user", self.service.store.get(rid)["data"].get("triggerMessageId"))
        # Even an otherwise matching record with no native entry ID must not bind an entry-offset fallback.
        other = self.record("missing", "user", "x", "stable-user")
        del other["id"]
        other["message"]["idempotencyKey"] = native + ":user"
        with path.open("a") as fp:
            fp.write(json.dumps(other) + "\n")
        await self.rpc("messages.list", {**params, "limit": 1, "view": "full"})
        self.assertEqual("stable-user", self.service.store.get(rid)["data"].get("triggerMessageId"))

    async def test_history_projection_resources_and_byte_range(self):
        before = self.index_path.read_bytes(), self.index_path.stat().st_mtime_ns
        page = await self.rpc("messages.list", {"threadId": self.thread, "segmentId": self.segment, "omitThinking": True, "limit": 30})
        self.assertTrue(page["ok"], page)
        result = page["result"]
        self.assertEqual(len(result["items"]), 2)
        serialized = json.dumps(result)
        self.assertNotIn("secret-replay", serialized)
        self.assertNotIn("visible reasoning", serialized)
        self.assertNotIn("huge-code", serialized)
        self.assertIn("echo hello", serialized)
        self.assertIn("x" * 3001, serialized)
        resource = next(block["resourceId"] for block in result["items"][1]["content"] if block["type"] == "resource")
        response = await self.client.get(f"/v1/resources/{resource}?runtimeId=one", headers={"Authorization": "Bearer " + self.lease.token, "Range": "bytes=123-456"})
        self.assertEqual(response.status, 206, await response.text() if response.status != 206 else "")
        self.assertEqual(await response.read(), self.media[123:457])
        self.assertEqual(response.headers["Content-Range"], f"bytes 123-456/{len(self.media)}")
        self.assertEqual(before, (self.index_path.read_bytes(), self.index_path.stat().st_mtime_ns))

    async def test_history_paging_after_a_day_still_requires_an_authorized_lease(self):
        params = {"threadId": self.thread, "segmentId": self.segment, "limit": 1}
        with patch("agent_manage.segment_reader.time", wraps=time) as reader_clock:
            reader_clock.time.return_value = 2000000000
            first = (await self.rpc("messages.list", {**params, "edge": "start"}))["result"]
            resume = {**params, "cursor": first["newerCursor"]}
            self.assertTrue(resume["cursor"])
            reader_clock.time.return_value += 86400
            other = self.service.auth.redeem(self.ticket(runtime_ids=["different"]))
            self.assertEqual((await self.rpc("messages.list", resume, lease=other))["error"]["code"], "FORBIDDEN")
            page = await self.rpc("messages.list", resume)
            self.assertTrue(page["ok"], page)
            self.assertNotEqual(page["result"]["items"][0]["id"], first["items"][0]["id"])

    async def test_reject_cross_runtime_scope_and_disallowed_agent(self):
        lease = self.service.auth.redeem(self.ticket(runtime_ids=["different"]))
        response = await self.rpc("agents.list", lease=lease)
        self.assertEqual(response["error"]["code"], "FORBIDDEN")
        other = self.adapter.agents()[1]["agentId"]
        lease = self.service.auth.redeem(self.ticket(agent_ids=[other]))
        response = await self.rpc("messages.list", {"threadId": self.thread, "segmentId": self.segment}, lease=lease)
        self.assertEqual(response["error"]["code"], "FORBIDDEN")
        response = await self.rpc("agents.list", lease=lease)
        self.assertEqual([agent["agentId"] for agent in response["result"]["items"]], [other])
        response = await self.rpc("dola.models.refresh", {"clientRequestId": "key"}, lease=lease)
        self.assertEqual(response["error"]["code"], "FORBIDDEN")

    async def test_thread_model_update_is_scoped_confirmed_and_recoverable(self):
        extra_key = "agent:main:dashboard:other-model-thread"
        index = json.loads(self.index_path.read_text())
        index[extra_key] = {"sessionId": "44444444-4444-4444-8444-444444444444"}
        self.index_path.write_text(json.dumps(index))
        extra = self.adapter.register_thread(self.agent, extra_key)
        original_config = self.config.read_bytes()

        async def update(model, request_key):
            response = await self.rpc("threads.models.update", {
                "threadId": self.thread, "modelId": model, "clientRequestId": request_key})
            self.assertTrue(response["ok"], response)
            return await self.wait_operation(response["result"]["operationId"])

        initial = (await self.rpc("threads.models.list", {"threadId": self.thread}))["result"]
        self.assertEqual("fixture/default", initial["modelId"])
        self.assertTrue(initial["usesDefault"])
        self.assertEqual({"fixture/default", "fixture/alternate"}, {item["modelId"] for item in initial["items"]})
        bad = await update("fixture/disallowed", "bad-model")
        self.assertEqual(("failed", "INVALID_ARGUMENT"), (bad["status"], bad["error"]["code"]))
        self.model_busy = True
        busy = await update("fixture/alternate", "busy-model")
        self.assertEqual(("failed", "THREAD_BUSY"), (busy["status"], busy["error"]["code"]))
        self.assertFalse(any(call["method"] == "sessions.patch" for call in self.native_calls))
        self.model_busy = False
        changed = await update("fixture/alternate", "ok-model")
        self.assertEqual("completed", changed["status"])
        self.assertEqual("fixture/alternate", changed["result"]["modelId"])
        self.assertEqual("fixture/default", (await self.rpc("threads.models.list", {"threadId": extra}))["result"]["modelId"])
        self.assertEqual(original_config, self.config.read_bytes())
        lookup = {"threadId": self.thread, "operationMethod": "threads.models.update", "clientRequestId": "ok-model"}
        discovered = (await self.rpc("operations.lookup", lookup))["result"]["items"]
        self.assertEqual([changed["operationId"]], [row["operationId"] for row in discovered])
        self.assertEqual([], (await self.rpc("operations.lookup", {**lookup, "threadId": extra}))["result"]["items"])
        cross = await self.rpc("threads.models.list", {"runtimeId": "other", "threadId": self.thread})
        self.assertEqual("FORBIDDEN", cross["error"]["code"])
        other_agent = self.adapter.agents()[1]["agentId"]
        lease = self.service.auth.redeem(self.ticket(agent_ids=[other_agent], actions=["threads.models.update"]))
        denied = await self.rpc("threads.models.update", {"threadId": self.thread, "modelId": None,
            "clientRequestId": "wrong-agent-model"}, lease=lease)
        self.assertEqual("FORBIDDEN", denied["error"]["code"])
        restored = await update(None, "restore-model")
        self.assertEqual(("completed", "fixture/default", True),
                         (restored["status"], restored["result"]["modelId"], restored["result"]["usesDefault"]))
        calls = [call["params"] for call in self.native_calls if call["method"] == "sessions.patch"]
        self.assertEqual([{"key": self.key, "model": "fixture/alternate"}, {"key": self.key, "model": None}], calls)

    async def test_create_operation_can_be_recovered_without_a_thread_id(self):
        params = {"agentId": self.agent, "clientRequestId": "recover-create"}
        accepted = await self.rpc("threads.create", params)
        operation = await self.wait_operation(accepted["result"]["operationId"])
        self.assertEqual("completed", operation["status"])
        self.assertIsNone(operation["threadId"])
        query = {"clientRequestId": params["clientRequestId"], "operationMethod": "threads.create"}
        recovered = (await self.rpc("operations.lookup", query))["result"]["items"]
        self.assertEqual([operation["operationId"]], [row["operationId"] for row in recovered])
        self.assertTrue(recovered[0]["result"]["threadId"].startswith("thread_"))
        stranger = self.service.auth.redeem(self.ticket(subject="other-principal"))
        self.assertEqual([], (await self.rpc("operations.lookup", query, lease=stranger))["result"]["items"])
        self.assertEqual(operation["operationId"], (await self.rpc("threads.create", params))["result"]["operationId"])
        self.assertEqual(1, sum(call["method"] == "sessions.create" for call in self.native_calls))

    async def test_thread_thinking_uses_native_model_options_and_never_changes_other_threads(self):
        self.thinking_supported = True
        other_key = "agent:main:dashboard:other-thinking"
        index = json.loads(self.index_path.read_text())
        index[other_key] = {}
        self.index_path.write_text(json.dumps(index))
        other = self.adapter.register_thread(self.agent, other_key)

        async def update(value, request):
            response = await self.rpc("threads.thinking.update", {"threadId": self.thread,
                "thinkingLevel": value, "clientRequestId": request})
            self.assertTrue(response["ok"], response)
            return await self.wait_operation(response["result"]["operationId"])

        profile = (await self.rpc("threads.thinking.get", {"threadId": self.thread}))["result"]
        self.assertEqual(("low", None, True), (profile["thinkingDefault"], profile["thinkingLevel"], profile["supported"]))
        self.assertEqual("INVALID_ARGUMENT", (await update("unsupported", "bad-thinking"))["error"]["code"])
        self.model_busy = True
        self.assertEqual("THREAD_BUSY", (await update("high", "busy-thinking"))["error"]["code"])
        self.model_busy = False
        self.assertEqual("completed", (await update("high", "apply-thinking"))["status"])
        self.assertEqual("high", (await self.rpc("threads.thinking.get", {"threadId": self.thread}))["result"]["thinkingLevel"])
        self.assertIsNone((await self.rpc("threads.thinking.get", {"threadId": other}))["result"]["thinkingLevel"])
        self.assertEqual("completed", (await update(None, "restore-thinking"))["status"])
        self.thinking_supported = False
        self.assertEqual("UNSUPPORTED", (await update("low", "unknown-thinking-profile"))["error"]["code"])
        self.assertEqual("FORBIDDEN", (await self.rpc("threads.thinking.get", {"threadId": self.thread, "runtimeId": "other"}))["error"]["code"])
        self.assertEqual([{"key": self.key, "thinkingLevel": "high"}, {"key": self.key, "thinkingLevel": None}],
                         [call["params"] for call in self.native_calls if call["method"] == "sessions.patch"])

    async def test_chat_socket_can_rename_only_threads_of_its_agent(self):
        from agent_manage.platform_exchange import _ACTIONS

        async def rename(agent_id, title, request_id):
            async with self.client.ws_connect("/v1/ws") as ws:
                await ws.send_json({"type": "auth", "ticket": self.ticket(
                    agent_ids=[agent_id], actions=sorted(_ACTIONS))})
                self.assertEqual("hello", (await ws.receive_json())["type"])
                await ws.send_json({"type": "request", "requestId": request_id, "method": "threads.update",
                    "params": {"runtimeId": "one", "threadId": self.thread, "title": title,
                               "clientRequestId": request_id}})
                return await ws.receive_json()

        other = self.adapter.agents()[1]["agentId"]
        denied = await rename(other, "Out of scope", "rename-denied")
        self.assertEqual("FORBIDDEN", denied["error"]["code"])
        self.assertFalse(any(call["method"] == "sessions.patch" for call in self.native_calls))

        accepted = await rename(self.agent, "New title", "rename-allowed")
        self.assertTrue(accepted["ok"], accepted)
        operation = await self.wait_operation(accepted["result"]["operationId"])
        self.assertEqual("completed", operation["status"], operation)
        calls = [call["params"] for call in self.native_calls if call["method"] == "sessions.patch"]
        self.assertEqual([{"key": self.key, "label": "New title"}], calls)

    async def test_idempotent_send_native_stream_and_cancel(self):
        ws = await self.client.ws_connect("/v1/ws")
        await ws.send_json({"type": "auth", "ticket": self.ticket()})
        hello = await ws.receive_json()
        self.assertEqual(hello["type"], "hello")
        await ws.send_json({"type": "request", "requestId": "sub", "method": "events.subscribe", "params": {"runtimeId": "one", "threadId": self.thread}})
        subscribed = await ws.receive_json()
        self.assertTrue(subscribed["ok"], subscribed)
        params = {"threadId": self.thread, "message": "hello", "clientRequestId": "send-once", "expectedCurrentSegmentId": self.current_segment}
        responses = await asyncio.gather(self.rpc("runs.create", params), self.rpc("runs.create", params))
        self.assertEqual(responses[0]["result"]["operationId"], responses[1]["result"]["operationId"])
        operation = await self.wait_operation(responses[0]["result"]["operationId"])
        self.assertEqual(operation["status"], "completed", operation)
        run_id = operation["result"]["runId"]
        native_id = self.service.store.get(run_id)["native"]
        self.assertEqual(len([call for call in self.native_calls if call["method"] == "chat.send"]), 1)
        await self.native_connections[0].send_json({"type": "event", "event": "chat", "payload": {"sessionKey": self.key, "runId": native_id,
            "state": "delta", "message": {"id": "live-message", "role": "assistant", "content": [{"type": "text", "text": "hello"}]}}})
        event = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertEqual(event["type"], "message.snapshot")
        self.assertEqual(event["message"]["content"][0]["text"], "hello")
        self.assertIn("authContext", event)
        cancelled = await self.rpc("runs.cancel", {"runId": run_id, "clientRequestId": "cancel-once"})
        operation = await self.wait_operation(cancelled["result"]["operationId"])
        self.assertEqual(operation["result"]["status"], "cancel_requested")
        self.assertEqual(len([call for call in self.native_calls if call["method"] == "chat.abort"]), 1)
        await ws.close()

    async def test_revocation_during_read_suppresses_late_payload(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.adapter.invoke
        async def delayed(method, params):
            started.set()
            await release.wait()
            return await original(method, params)
        with patch.object(self.adapter, "invoke", delayed):
            task = asyncio.create_task(self.rpc("agents.list"))
            await started.wait()
            self.service.auth.revoke(self.lease.context["leaseId"])
            release.set()
            response = await task
        self.assertFalse(response["ok"])
        self.assertNotIn("result", response)
        self.assertEqual(response["error"]["code"], "UNAUTHENTICATED")

    async def test_websocket_refresh_clears_subscriptions_and_rejects_reused_ticket(self):
        ticket = self.ticket()
        ws = await self.client.ws_connect("/v1/ws")
        await ws.send_json({"type": "auth", "ticket": ticket})
        initial = await ws.receive_json()
        await ws.send_json({"type": "auth.refresh", "ticket": self.ticket(actions=["system.info"])})
        refreshed = await ws.receive_json()
        self.assertEqual(refreshed["type"], "auth.refreshed")
        with self.assertRaises(APIError):
            self.service.auth.authenticate(initial["leaseToken"])
        await ws.send_json({"type": "request", "requestId": "denied", "method": "agents.list", "params": {"runtimeId": "one"}})
        response = await ws.receive_json()
        self.assertEqual(response["error"]["code"], "FORBIDDEN")
        await ws.close()
        response = await self.client.post("/v1/auth/redeem", json={"ticket": ticket})
        self.assertEqual(response.status, 401)

    async def test_origin_denied_and_capabilities_are_honest(self):
        response = await self.client.post("/v1/auth/redeem", headers={"Origin": "https://evil.example"}, json={"ticket": self.ticket()})
        self.assertEqual(response.status, 403)
        result = (await self.rpc("capabilities.get"))["result"]
        self.assertFalse(result["eventReplay"])
        self.assertEqual(result["methods"]["dola.shares.create"]["support"], "unsupported")
        self.assertEqual(result["engineVersion"], "2026.7.1-2")

    async def test_server_can_delegate_scoped_tickets_over_the_unix_socket(self):
        grant = {"issuer": "dola", "subject": "signed-in-user", "audience": self.service.auth.audience,
                 "runtimeIds": ["one"], "agentIds": [self.agent], "actions": ["agents.list"], "authzVersion": 1}
        for invalid, status in (({**grant, "agentIds": []}, 400), ({**grant, "audience": "other-manager"}, 403)):
            response = await self.client.post("/v1/auth/tickets", json=invalid)
            self.assertEqual(response.status, status)
        response = await self.client.post("/v1/auth/tickets", headers={"Origin": "https://chat.example"}, json=grant)
        self.assertEqual(response.status, 403)
        for transport in ("http", "websocket"):
            response = await self.client.post("/v1/auth/tickets", json=grant)
            self.assertEqual(response.status, 200)
            ticket = (await response.json())["ticket"]
            if transport == "http":
                response = await self.client.post("/v1/auth/redeem", json={"ticket": ticket})
                lease = self.service.auth.authenticate((await response.json())["leaseToken"])
                result = await self.rpc("agents.list", lease=lease)
                denied = await self.rpc("models.list", lease=lease)
                self.assertEqual(denied["error"]["code"], "FORBIDDEN")
            else:
                async with self.client.ws_connect("/v1/ws") as ws:
                    await ws.send_json({"type": "auth", "ticket": ticket})
                    self.assertEqual((await ws.receive_json())["type"], "hello")
                    await ws.send_json({"type": "request", "requestId": "list", "method": "agents.list",
                                        "params": {"runtimeId": "one"}})
                    result = await ws.receive_json()
            self.assertEqual([item["agentId"] for item in result["result"]["items"]], [self.agent])

    async def test_loopback_tcp_cannot_issue_tickets(self):
        await web.TCPSite(self.manager_runner, "127.0.0.1", 0).start()
        port = next(address[1] for address in self.manager_runner.addresses if isinstance(address, tuple))
        async with ClientSession() as client:
            response = await client.post(f"http://127.0.0.1:{port}/v1/auth/tickets", json={})
            self.assertEqual(response.status, 403)

    async def test_delta_only_streams_are_accumulated_once_and_terminal_is_sticky(self):
        received = []
        self.service.listeners.add(received.append)
        base = {"sessionKey": self.key, "runId": "external-run"}
        for payload in ({"state": "delta", "seq": 1, "deltaText": "hello"},
                        {"state": "delta", "seq": 2, "deltaText": " world"},
                        {"state": "delta", "seq": 2, "deltaText": " world"},
                        {"state": "final", "seq": 3, "message": {"role": "assistant", "content": [{"type": "text", "text": "hello world"}]}},
                        {"state": "delta", "seq": 4, "deltaText": "stale"}):
            await self.adapter.native_event({"event": "chat", "payload": {**base, **payload}})
        snapshots = [event for event in received if event["type"] == "message.snapshot"]
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(snapshots[1]["message"]["content"][0]["text"], "hello world")
        self.assertEqual(len({event["message"]["id"] for event in snapshots}), 1)
        self.assertEqual(snapshots[-1]["message"]["generationState"], "final")
        self.assertEqual(snapshots[-1]["correlation"], "unresolved")
        self.service.listeners.discard(received.append)

    async def test_expired_websocket_is_closed_without_waiting_for_next_request(self):
        ws = await self.client.ws_connect("/v1/ws")
        await ws.send_json({"type": "auth", "ticket": self.ticket()})
        hello = await ws.receive_json()
        self.service.auth.revoke(hello["authContext"]["leaseId"])
        message = await asyncio.wait_for(ws.receive(), 2)
        from aiohttp import WSMsgType
        self.assertIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSED))
        await ws.close()

    async def test_queued_control_write_rechecks_revocation_before_side_effects(self):
        import threading
        release = threading.Event()
        occupied = [asyncio.create_task(self.service.write_workers(release.wait, 3)) for _ in range(2)]
        while self.service.write_workers.active < 2:
            await asyncio.sleep(.001)
        before = self.config.read_bytes()
        response = await self.rpc("channels.configure", {"channel": "telegram", "clientRequestId": "queued",
            "options": {"agentName": "main", "botToken": "fixture", "botName": "queued"}})
        self.assertTrue(response["ok"], response)
        while self.service.write_workers.waiting == 0:
            await asyncio.sleep(.001)
        self.service.auth.revoke(self.lease.context["leaseId"])
        release.set()
        await asyncio.gather(*occupied)
        operation = await self.wait_operation(response["result"]["operationId"])
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(self.config.read_bytes(), before)

    async def test_projection_and_snapshot_survive_cursor_pagination(self):
        page = await self.rpc("messages.list", {"threadId": self.thread, "segmentId": self.segment,
                                               "edge": "end", "limit": 1, "omitThinking": True, "view": "full"})
        first = page["result"]
        next_page = await self.rpc("messages.list", {"threadId": self.thread, "segmentId": self.segment,
                                                    "cursor": first["olderCursor"], "limit": 1})
        self.assertTrue(next_page["ok"], next_page)
        self.assertEqual(first["snapshotId"], next_page["result"]["snapshotId"])
        self.assertEqual(first["projectionId"], next_page["result"]["projectionId"])
        self.assertTrue(next_page["result"]["items"][0]["projection"]["omitThinking"])
        self.assertEqual(next_page["result"]["items"][0]["projection"]["view"], "full")

    async def test_wrong_gateway_configuration_is_rejected_before_send(self):
        original = self.config
        self.config = self.runtime / "different-openclaw.json"
        self.config.write_text("{}")
        try:
            response = await self.rpc("runs.create", {"threadId": self.thread, "message": "must not send", "clientRequestId": "wrong-gateway", "expectedCurrentSegmentId": self.current_segment})
            operation = await self.wait_operation(response["result"]["operationId"])
            self.assertEqual(operation["status"], "failed")
            self.assertEqual(operation["error"]["code"], "RUNTIME_MISMATCH")
            self.assertFalse(any(call["method"] == "chat.send" for call in self.native_calls))
        finally:
            self.config = original

    async def test_scoped_sender_can_reconcile_before_run_exists_without_admin_access(self):
        lease = self.service.auth.redeem(self.ticket(agent_ids=[self.agent], actions=["runs.create", "runs.lookup", "operations.get", "operations.list", "operations.lookup"]))
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.adapter.invoke
        async def delayed(method, params):
            if method == "runs.create":
                entered.set()
                await release.wait()
            return await original(method, params)
        params = {"threadId": self.thread, "message": "hello", "clientRequestId": "pending", "expectedCurrentSegmentId": self.current_segment}
        with patch.object(self.adapter, "invoke", delayed):
            response = await self.rpc("runs.create", params, lease)
            await entered.wait()
            try:
                op = response["result"]["operationId"]
                accepted = await self.rpc("operations.get", {"operationId": op}, lease)
                self.assertTrue(accepted["ok"], accepted)
                lookup = (await self.rpc("runs.lookup", {"threadId": self.thread, "clientRequestId": "pending"}, lease))["result"]
                self.assertEqual(lookup["acceptance"], "accepted")
                self.assertEqual(lookup["items"], [])
                self.assertEqual(lookup["operations"][0]["operationId"], op)
                admin_op, _ = self.service.store.claim(lease.context, "one", "dola.models.refresh", "admin", {})
                denied = await self.rpc("operations.get", {"operationId": admin_op["operationId"]}, lease)
                self.assertEqual(denied["error"]["code"], "FORBIDDEN")
                listed = (await self.rpc("operations.list", lease=lease))["result"]["items"]
                self.assertEqual([row["operationId"] for row in listed], [op])
                other = self.service.auth.redeem(self.ticket(subject="other", agent_ids=[self.agent]))
                self.assertEqual((await self.rpc("operations.get", {"operationId": op}, other))["error"]["code"], "NOT_FOUND")
            finally:
                release.set()
            await self.wait_operation(op)
        found = (await self.rpc("runs.lookup", {"threadId": self.thread, "clientRequestId": "pending"}, lease))["result"]
        self.assertEqual(len(found["items"]), 1)
        foreign = (await self.rpc("runs.lookup", {"threadId": self.thread, "clientRequestId": "pending"}, other))["result"]
        self.assertEqual(foreign["acceptance"], "not_accepted")
        self.assertEqual(foreign["items"], [])

    async def test_send_after_queued_reset_rejects_old_segment_without_dispatch(self):
        reset = await self.rpc("threads.reset", {"threadId": self.thread, "clientRequestId": "reset"})
        sent = await self.rpc("runs.create", {"threadId": self.thread, "clientRequestId": "stale-send", "message": "preserve draft",
                                              "expectedCurrentSegmentId": self.current_segment})
        self.assertEqual((await self.wait_operation(reset["result"]["operationId"]))["status"], "completed")
        operation = await self.wait_operation(sent["result"]["operationId"])
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["error"]["code"], "SEGMENT_CHANGED")
        self.assertFalse(any(call["method"] == "chat.send" for call in self.native_calls))

    async def test_live_media_reference_degrades_without_dropping_text(self):
        received = []
        self.service.listeners.add(received.append)
        try:
            await self.adapter.native_event({"event": "chat", "payload": {"sessionKey": self.key, "state": "final", "runId": "media-run",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "generated image"}], "mediaRefs": [{"path": "/private/image.png"}]}}})
        finally:
            self.service.listeners.discard(received.append)
        message = received[0]["message"]
        self.assertEqual(message["content"][0]["text"], "generated image")
        self.assertEqual(message["content"][1]["code"], "MEDIA_PENDING_HISTORY")
        self.assertNotIn("/private", json.dumps(message))

    async def test_tool_input_image_is_not_an_assistant_deliverable(self):
        path = self.sessions / (self.physical_ids[0] + ".jsonl.reset.2026-10-12T02-00-00.000Z")
        rows = [self.record("u", "user", [{"type": "text", "text": "inspect image"}], None),
            self.record("a", "assistant", [{"type": "toolCall", "name": "read", "id": "read1", "arguments": {"path": str(self.workspace / "image.png")}}], "u")]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        page = (await self.rpc("messages.list", {"threadId": self.thread, "segmentId": self.segment}))["result"]
        resource = next(block["resourceId"] for item in page["items"] for block in item["content"] if block["type"] == "resource")
        metadata = (await self.rpc("resources.get", {"resourceId": resource}))["result"]
        self.assertEqual(metadata["role"], "tool_input")

    async def test_full_message_ref_inherits_projection_after_full_pagination(self):
        path = self.sessions / (self.physical_ids[0] + ".jsonl.reset.2026-10-12T02-00-00.000Z")
        body = "long message " * 25000
        rows = [self.record("u", "user", [{"type": "text", "text": "question"}], None),
                self.record("a", "assistant", [{"type": "text", "text": body}, {"type": "thinking", "thinking": "keep hidden"}], "u")]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        page = (await self.rpc("messages.list", {"threadId": self.thread, "segmentId": self.segment, "limit": 1,
                                                 "view": "full", "omitThinking": True}))["result"]
        next_page = await self.rpc("messages.list", {"threadId": self.thread, "segmentId": self.segment,
                                                     "limit": 1, "cursor": page["olderCursor"]})
        self.assertTrue(next_page["ok"], next_page)
        ref = page["items"][0]["fullMessageRef"]
        full = await self.rpc(ref["method"], ref["params"])
        self.assertTrue(full["ok"], full)
        self.assertEqual(full["result"]["message"]["content"], [{"blockId": "a:0", "type": "text", "text": body}])
        self.assertEqual(full["result"]["message"]["projection"]["view"], "full")
