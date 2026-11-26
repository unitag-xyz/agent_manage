import asyncio
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import time
from unittest.mock import AsyncMock, patch

from agent_manage.core.errors import APIError
from agent_manage.response import ProtocolError
from agent_manage.path_io import open_without_links, read_without_links

try:
    from agent_manage.server import build_service
    from agent_manage.server import create_app
    from aiohttp import ClientSession, UnixConnector, web
    from aiohttp.test_utils import TestServer
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    AVAILABLE = True
except ModuleNotFoundError:
    AVAILABLE = False
if AVAILABLE:
    from agent_manage.adapters.openclaw.host_binding import HostBinding


@unittest.skipUnless(AVAILABLE, "Install server extra and host binding implementation")
class HostBindingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.services = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()
        self.tmp.cleanup()

    def registration(self, identifier="one", port=18001):
        root = self.root / identifier
        config = root / "config/openclaw.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        native = "/home/node/.openclaw"
        config.write_text(json.dumps({"gateway": {"auth": {"mode": "token", "token": "fixture-" + identifier}},
            "agents": {"list": [{"id": "main", "workspace": native + "/workspace"},
                                   {"id": "attached", "workspace": native + "/workspace/attached"}]}}))
        return {"id": identifier, "engine": "openclaw", "environmentId": identifier,
            "home": str(root), "configPath": str(config), "gatewayUrl": f"ws://127.0.0.1:{port}",
            "hostBinding": {"bindingId": "generation-1", "nativeConfigPath": native + "/openclaw.json",
                "pathMappings": [{"native": native, "local": str(config.parent)},
                                 {"native": native + "/workspace", "local": str(workspace)}],
                "agentBindings": [{"agentIdentify": "purchase-A", "nativeAgentId": "main", "installationId": "install-A",
                                   "workspace": native + "/workspace"},
                                  {"agentIdentify": "purchase-B", "nativeAgentId": "attached", "installationId": "install-B",
                                   "workspace": native + "/workspace/attached"}]}}

    def service(self, *registrations):
        with patch("agent_manage.adapters.openclaw.factory.detect_version", return_value="2026.7.1-2"):
            service = build_service({"stateDirectory": str(self.root / ("manager-" + str(len(self.services)))),
                                     "runtimes": list(registrations)})
        self.services.append(service)
        return service

    async def test_owner_sidebar_and_agent_switch_share_a_single_platform_socket(self):
        from agent_manage.platform_runtime import PlatformRuntimePool

        self.registration()
        root = self.root / "one"
        for agent, rows in {
            "main": {"agent:main:recent": {"label": "Recent", "updatedAt": 20},
                     "agent:main:older": {"label": "Older", "updatedAt": 10},
                     "agent:other:forged": {"label": "Foreign", "updatedAt": 30}},
            "attached": {},
        }.items():
            sessions = root / "config/agents" / agent / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "sessions.json").write_text(json.dumps(rows))
        service = self.service()
        generation = "a" * 64
        pool = PlatformRuntimePool(self.root / "state", "agent-manager", {
            "instanceIdentify": "one", "bindingId": generation, "mountRoot": str(root),
            "gatewayOrigin": "https://local-chat.invalid"})
        runner = web.AppRunner(create_app(service, pool))
        await runner.setup()
        socket_path = str(self.root / "owner.sock")
        await web.UnixSite(runner, socket_path).start()
        try:
            with (patch("agent_manage.adapters.openclaw.factory.detect_version", return_value="2026.7.1-2"),
                  patch("agent_manage.adapters.openclaw.v2026_7.gateway.Gateway.connect", new_callable=AsyncMock),
                  patch("agent_manage.adapters.openclaw.v2026_7.gateway.Gateway.call", new_callable=AsyncMock) as gateway_call):
                def scoped_titles(method, params, **kwargs):
                    if method == "chat.history":
                        return {}
                    self.assertIn(method, ("sessions.list", "sessions.describe"))
                    self.assertTrue(params["includeDerivedTitles"])
                    self.assertNotIn("includeLastMessage", params)
                    row = {"key": "agent:attached:session", "sessionId": "11111111-2222-4333-8444-555555555555",
                           "displayName": "Attached", "label": "Attached", "derivedTitle": "Attached"}
                    if method == "sessions.describe":
                        return {"session": row if params["key"] == row["key"] else None}
                    return {"sessions": [row] if params["agentId"] == "attached" else []}
                gateway_call.side_effect = scoped_titles
                async with ClientSession(connector=UnixConnector(path=socket_path)) as client:
                    async def open_chat(native, marker="chat.instance", template_identify=None, subject="owner"):
                        ws = await client.ws_connect("http://manager/v1/platform/ws")
                        await ws.send_json({"type": "platform.bind", "runtimeId": "one", "bindingId": generation})
                        await ws.send_json({"type": "auth", "ticket": "b" * 64})
                        self.assertEqual("platform.redeem", (await ws.receive_json())["type"])
                        await ws.send_json({"type": "platform.grant", "runtimeId": "one", "bindingId": generation,
                            "issuer": "dola", "subject": subject, "tenantId": "owner", "agentIdentify": native + "-one",
                            "nativeAgentId": native, "installationId": native + "-install", "actions": [marker],
                            "expiresAt": int(time.time() * 1000) + 60_000,
                            **({"extensions": {"presentation": {"templateIdentify": template_identify},
                                                "attachedInstallations": [
                                                    {"nativeAgentId": "attached", "platformAgentIdentify": "platform-attached",
                                                     "installationId": "installation-attached"},
                                                    {"nativeAgentId": "absent", "platformAgentIdentify": "platform-absent",
                                                     "installationId": "installation-absent"}],
                                                "futureMetadata": ["ignored"]}} if template_identify else {})})
                        hello = await ws.receive_json(timeout=3)
                        self.assertEqual("hello", hello["type"])
                        return ws, hello["platformAgentId"]

                    async def rpc(ws, method, runtime="one", **params):
                        await ws.send_json({"type": "request", "requestId": method, "method": method,
                                            "params": {"runtimeId": runtime, **params}})
                        return await ws.receive_json(timeout=3)

                    first, main_id = await open_chat("main", template_identify="catalog-primary")
                    async with first:
                        capabilities = (await rpc(first, "capabilities.get"))["result"]
                        self.assertTrue(capabilities["methods"]["runs.create"]["authorized"])
                        self.assertEqual(("inline", 5, 23 * 1024 * 1024), tuple(capabilities["attachments"][key]
                            for key in ("mode", "maxCount", "maxBase64Bytes")))
                        sidebar = (await rpc(first, "chat.sidebar", limit=1))["result"]["items"]
                        main, attached = sidebar
                        self.assertEqual(main_id, main["agentId"])
                        self.assertEqual(["Recent"], [item["title"] for item in main["items"]])
                        self.assertTrue(main["hasMore"])
                        self.assertEqual(2, main["totalCount"])
                        self.assertTrue(main["isSystem"])
                        self.assertFalse(attached["isSystem"])
                        self.assertEqual("catalog-primary", main["templateIdentify"])
                        self.assertEqual(("ready", []), (attached["status"], attached["items"]))
                        self.assertEqual("attached", attached["templateIdentify"])
                        self.assertEqual("platform-attached", attached["platformAgentIdentify"])
                        self.assertTrue(attached["canRefresh"])
                        self.assertNotIn("platformAgentIdentify", main)
                        self.assertNotIn("installationId", attached)
                        config_path = root / "config/openclaw.json"
                        config = json.loads(config_path.read_text())
                        config["session"] = {"mainKey": "Home"}
                        config["agents"]["list"][1]["identity"] = {"avatarUrl": "https://images.example.test/attached.png"}
                        config_path.write_text(json.dumps(config))
                        state_dir = root / "config/agent-manage/agents"
                        state_dir.mkdir(parents=True)
                        state = {"schema_version": 1, "agent_name": "attached",
                                 "config_path": "/home/node/.openclaw/openclaw.json",
                                 "workspace": "/home/node/.openclaw/workspace/attached",
                                 "template_name": "catalog-attached"}
                        state_path = state_dir / "attached.json"
                        state_path.write_text(json.dumps({**state, "workspace": "/home/node/.openclaw/workspace/main"}))
                        nonmatching = (await rpc(first, "chat.sidebar"))["result"]["items"][1]
                        self.assertEqual("attached", nonmatching["templateIdentify"])
                        self.assertEqual("https://images.example.test/attached.png", nonmatching["avatarUrl"])
                        other_state = self.root / "other-agent-state.json"
                        other_state.write_text(json.dumps(state))
                        state_path.unlink()
                        state_path.symlink_to(other_state)
                        linked = (await rpc(first, "chat.sidebar"))["result"]["items"][1]
                        self.assertEqual("attached", linked["templateIdentify"])
                        state_path.unlink()
                        state_path.write_text(json.dumps(state))
                        bound = (await rpc(first, "chat.sidebar"))["result"]["items"][1]
                        self.assertEqual("catalog-attached", bound["templateIdentify"])
                        self.assertEqual(attached["agentId"], bound["agentId"])
                        state_path.unlink()
                        physical = "11111111-2222-4333-8444-555555555555"
                        sessions = root / "config/agents/attached/sessions"
                        (sessions / "sessions.json").write_text(json.dumps({"agent:attached:session": {
                            "label": "Attached", "updatedAt": 5, "sessionId": physical,
                            "totalTokens": 42, "contextTokens": 100, "totalTokensFresh": False,
                            "sessionFile": f"/home/node/.openclaw/agents/attached/sessions/{physical}.jsonl"},
                            "agent:attached:home": {"derivedTitle": "Primary", "updatedAt": 2}}))
                        (sessions / f"{physical}.jsonl").write_text(json.dumps({"type": "message", "id": "m1",
                            "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user",
                                "content": [{"type": "text", "text": "Attached private history fixture"},
                                            {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="}]}}) + "\n"
                            + json.dumps({"type": "message", "id": "m2", "parentId": "m1", "durationMs": 90,
                                          "message": {"role": "assistant", "content": [{"type": "text", "text": "usage fixture"}],
                                                      "model": "fixture", "provider": "openai", "api": "openai-chatgpt-responses",
                                                      "usage": {"input": 8, "output": 2, "cacheRead": 1, "totalTokens": 11}}}) + "\n")
                        attached_rows = (await rpc(first, "threads.list", agentId=attached["agentId"]))["result"]["items"]
                        thread_id = attached_rows[0]["threadId"]
                        detail = await rpc(first, "threads.get", threadId=thread_id)
                        self.assertEqual("Attached", detail["result"]["title"])
                        self.assertEqual("Attached", detail["result"]["derivedTitle"])
                        self.assertEqual("Attached", next(row["derivedTitle"] for row in attached_rows if row["threadId"] == thread_id))
                        self.assertTrue(detail["result"]["canDelete"])
                        self.assertEqual((42, 100, False), tuple(detail["result"][k]
                            for k in ("totalTokens", "contextTokens", "totalTokensFresh")))
                        protected = next(item for item in attached_rows if item["isMain"])
                        self.assertEqual("Primary", protected["title"])
                        self.assertFalse(protected["canDelete"])
                        sidebar_attached = (await rpc(first, "chat.sidebar"))["result"]["items"][1]
                        self.assertEqual("Attached", next(row["derivedTitle"] for row in sidebar_attached["items"] if row["threadId"] == thread_id))
                        self.assertEqual(2, sidebar_attached["totalCount"])
                        self.assertIn(protected["threadId"], [item["threadId"] for item in sidebar_attached["items"]
                                                               if item["isMain"] and not item["canDelete"]])
                        segment_id = detail["result"]["currentSegmentId"]
                        history = await rpc(first, "messages.list", threadId=thread_id, segmentId=segment_id)
                        self.assertTrue(history["ok"], history)
                        self.assertIn("Attached private history fixture", json.dumps(history["result"]))
                        self.assertIsNone(history["result"]["items"][0]["statistics"])
                        expected = {"model": "fixture", "provider": "openai", "durationMs": 90, "billingSource": "codex",
                                    "usage": {"input": 8, "output": 2, "cacheRead": 1, "totalTokens": 11}}
                        self.assertEqual(expected, history["result"]["items"][1]["statistics"])
                        full = await rpc(first, "messages.get", threadId=thread_id, segmentId=segment_id, messageId="m2")
                        self.assertEqual(expected, full["result"]["message"]["statistics"])

                        archive = "33333333-4444-4555-8666-777777777777"
                        index_path = sessions / "sessions.json"
                        index = json.loads(index_path.read_text())
                        index["agent:attached:session"]["usageFamilySessionIds"] = [archive]
                        index_path.write_text(json.dumps(index))
                        archive_rows = [
                            {"id": "m2", "message": {"role": "assistant", "provider": "openai-codex", "api": "openai-codex-responses", "model": "older",
                                "responseId": "resp_older", "durationMs": 0, "usage": {"input": 0, "cost": {"total": 0}},
                                "content": [{"type": "thinking", "thinking": "do not display"}]}},
                            {"id": "large", "message": {"role": "assistant", "usage": {"output": 9},
                                "content": [{"type": "text", "text": "x" * 340000}]}},
                            {"id": "image", "message": {"role": "toolResult", "toolCallId": "call1",
                                "toolName": "image_generate", "details": {"count": 0, "provider": "/private/file",
                                "task": {"taskId": "task1", "runId": "native-run"}, "paths": ["/private/file"]}}},
                            {"id": "api-key", "message": {"role": "assistant", "provider": "openai", "api": "openai-responses",
                                "model": "gpt-codex-named", "usage": {"input": 4}}},
                            {"id": "unknown", "api": "openai-codex-responses", "message": {
                                "role": "assistant", "provider": "openai", "model": "older", "usage": {"input": 2}}},
                            {"id": "other-provider", "message": {"role": "assistant", "provider": "sample",
                                "api": "openai-codex-responses", "usage": {"input": 3}}},
                            *({"id": f"later-{i}", "message": {"role": "user", "content": "later"}}
                              for i in range(220)),
                        ]
                        (sessions / f"{archive}.jsonl").write_text("".join(
                            json.dumps({"type": "message", **row}) + "\n" for row in archive_rows))
                        segments = await rpc(first, "segments.list", threadId=thread_id)
                        archived_segment = next(x["segmentId"] for x in segments["result"]["items"] if not x["isCurrent"])
                        args = {"threadId": thread_id, "segmentId": archived_segment}
                        tail_logs = await rpc(first, "sessions.usage.logs", **args, limit=200)
                        self.assertNotIn("m2", [row["messageId"] for row in tail_logs["result"]["logs"]])
                        page = await rpc(first, "messages.list", **args, edge="end", limit=100, omitThinking=True)
                        seen = {}
                        while True:
                            self.assertTrue(page["ok"], page)
                            seen.update((row["id"], row) for row in page["result"]["items"])
                            cursor = page["result"]["olderCursor"]
                            if not cursor:
                                break
                            page = await rpc(first, "messages.list", **args, cursor=cursor, limit=100)
                        self.assertEqual(226, len(seen))
                        self.assertEqual([], seen["m2"]["content"])
                        self.assertEqual({"model": "older", "provider": "openai-codex", "responseId": "resp_older",
                                          "billingSource": "codex",
                                          "durationMs": 0, "usage": {"input": 0, "cost": {"total": 0}}},
                                         seen["m2"]["statistics"])
                        self.assertIsNone(seen["later-0"]["statistics"])
                        self.assertEqual({"toolCallId": "call1", "mediaGeneration":
                                         {"kind": "image", "outputCount": 0, "taskId": "task1"}},
                                         seen["image"]["statistics"])
                        self.assertEqual("reference", seen["large"]["completeness"])
                        for identifier in ("api-key", "unknown", "other-provider"):
                            self.assertNotIn("billingSource", seen[identifier]["statistics"])
                        for identifier in ("large", "m2", "image", "api-key", "unknown", "other-provider"):
                            ref = seen[identifier].get("fullMessageRef", {}).get("params", {}).get("ref")
                            full = await rpc(first, "messages.get", **args, messageId=identifier,
                                             **({"ref": ref} if ref else {}))
                            self.assertTrue(full["ok"], full)
                            self.assertEqual(seen[identifier]["statistics"], full["result"]["message"]["statistics"])
                        forward = await rpc(first, "messages.list", **args, edge="start", limit=1, omitThinking=True)
                        self.assertEqual(seen["m2"]["statistics"], forward["result"]["items"][0]["statistics"])
                        self.assertTrue(forward["result"]["newerCursor"])
                        wrong = await rpc(first, "messages.get", threadId=protected["threadId"],
                                          segmentId=archived_segment, messageId="m2")
                        self.assertEqual("FORBIDDEN", wrong["error"]["code"])
                        resource_id = next(block["resourceId"] for message in history["result"]["items"]
                                           for block in message["content"] if block["type"] == "resource")
                        media = await rpc(first, "resources.get", resourceId=resource_id, offset=1, length=3)
                        self.assertTrue(media["ok"], media)
                        self.assertEqual(b"ell", base64.b64decode(media["result"]["data"]))
                        self.assertEqual(5, media["result"]["totalBytes"])
                        self.assertEqual("FORBIDDEN", (await rpc(first, "resources.get", "two",
                            resourceId=resource_id, offset=0, length=1))["error"]["code"])
                        self.assertEqual("FORBIDDEN", (await rpc(first, "chat.sidebar", "two"))["error"]["code"])
                        self.assertEqual("FORBIDDEN", (await rpc(first, "services.start",
                            clientRequestId="management-denied"))["error"]["code"])
                        adapter = next(iter(pool.services.values())).adapter("one")
                        quota = {"provider": "openai-codex", "accountEmail": "owner@example.test",
                                 "windows": [{"label": "5h", "usedPercent": 42, "resetAt": 1800000000000}]}
                        native_usage = {"providers": [
                            {**quota, "token": "must-not-leak", "windows": [
                                {**quota["windows"][0], "internal": "must-not-leak"},
                                {"label": "invalid", "usedPercent": 101}]},
                            {"provider": "other", "error": "raw diagnostic"}]}
                        with patch.object(adapter.gateway, "call", new_callable=AsyncMock, return_value=native_usage) as native_call:
                            usage = await rpc(first, "usage.status")
                            self.assertTrue(usage["ok"], usage)
                            self.assertEqual({"providers": [quota, {"provider": "other", "windows": [],
                                                                     "error": "Usage unavailable"}]}, usage["result"])
                            self.assertEqual("FORBIDDEN", (await rpc(first, "usage.status", "two"))["error"]["code"])
                            self.assertEqual("INVALID_ARGUMENT", (await rpc(first, "usage.status", provider="openai"))["error"]["code"])
                            native_call.assert_awaited_once_with("usage.status", {}, timeout=15)
                        task = {"id": "task-a", "taskId": "task-a", "sessionKey": "agent:attached:session", "status": "running"}
                        foreign = {**task, "sessionKey": "agent:main:recent"}
                        replies = {"commands.list": {"commands": [{"name": "status"}]},
                                   "tasks.list": {"tasks": [task, foreign], "nextCursor": "2"},
                                   "tasks.get": {"task": task}, "tasks.cancel": {"cancelled": True},
                                   "usage.cost": {"totals": {"totalTokens": 12}},
                                   "sessions.usage": {"totals": {"totalTokens": 8}},
                                   "sessions.usage.logs": {"logs": [{"role": "assistant", "content": "do not return"}]},
                                   "sessions.usage.timeseries": {"points": [{"totalTokens": 8}]}}
                        async def wait_operation(ws, response):
                            self.assertTrue(response["ok"], response)
                            for _ in range(40):
                                current = (await rpc(ws, "operations.get", operationId=response["result"]["operationId"]))["result"]
                                if current["status"] not in ("accepted", "running"):
                                    return current
                                await asyncio.sleep(.01)
                            self.fail("operation did not settle")

                        with patch.object(adapter.gateway, "call", new_callable=AsyncMock,
                                          side_effect=lambda method, *args, **kwargs: replies.get(method, {})) as native_call:
                            result = await rpc(first, "commands.list", threadId=thread_id)
                            self.assertTrue(result["ok"], result)
                            native_call.assert_awaited_with("commands.list", {"agentId": "attached", "scope": "text", "includeArgs": False})
                            listed = (await rpc(first, "tasks.list", threadId=thread_id))["result"]
                            self.assertEqual(["task-a"], [item["taskId"] for item in listed["tasks"]])
                            self.assertEqual("2", listed["nextCursor"])
                            self.assertNotIn("sessionKey", listed["tasks"][0])
                            self.assertTrue((await rpc(first, "tasks.list", threadId=thread_id,
                                                       status=["queued", "running"], limit=1))["ok"])
                            native_call.assert_awaited_with("tasks.list", {"sessionKey": "agent:attached:session",
                                                                          "limit": 1, "status": ["queued", "running"]})
                            self.assertEqual("INVALID_ARGUMENT", (await rpc(first, "tasks.list", threadId=thread_id,
                                                                           status=["bogus"]))["error"]["code"])
                            self.assertTrue((await rpc(first, "tasks.get", threadId=thread_id, taskId="task-a"))["ok"])
                            for method in ("usage.cost", "sessions.usage", "sessions.usage.logs", "sessions.usage.timeseries"):
                                data = await rpc(first, method, **({"threadId": thread_id} if method.startswith("sessions.") else {}))
                                self.assertTrue(data["ok"], data)
                                self.assertNotIn("do not return", json.dumps(data))
                                if method == "sessions.usage.logs":
                                    actual = next(row for row in data["result"]["logs"] if row["messageId"] == "m2")
                                    self.assertEqual(("fixture", "openai", 90, 11),
                                                     tuple(actual[k] for k in ("model", "provider", "durationMs", "tokens")))
                                    self.assertEqual(1, actual["usage"]["cacheRead"])
                                    self.assertNotIn("runId", actual)
                                    self.assertNotIn("content", actual)
                                    self.assertNotIn("billingSource", actual)
                            replies["tasks.get"] = {"task": foreign}
                            self.assertEqual("NOT_FOUND", (await rpc(first, "tasks.get", threadId=thread_id, taskId="task-a"))["error"]["code"])
                            await wait_operation(first, await rpc(first, "tasks.cancel", threadId=thread_id,
                                                                  taskId="task-a", clientRequestId="deny-cancel"))
                            self.assertNotIn("tasks.cancel", [call.args[0] for call in native_call.await_args_list])
                            replies["tasks.get"] = {"task": task}
                            done = await wait_operation(first, await rpc(first, "tasks.cancel", threadId=thread_id,
                                                                        taskId="task-a", clientRequestId="allow-cancel"))
                            self.assertEqual("completed", done["status"], done)
                            native_call.assert_awaited_with("tasks.cancel", {"taskId": "task-a"})
                            done = await wait_operation(first, await rpc(first, "threads.delete",
                                threadId=protected["threadId"], clientRequestId="protect-main"))
                            self.assertEqual(("failed", "MAIN_THREAD_PROTECTED"), (done["status"], done["error"]["code"]))
                            self.assertNotIn("sessions.delete", [call.args[0] for call in native_call.await_args_list])
                            for method, native in (("threads.reset", "sessions.reset"), ("threads.delete", "sessions.delete")):
                                done = await wait_operation(first, await rpc(first, method, threadId=thread_id, clientRequestId=method))
                                self.assertEqual("completed", done["status"], done)
                                native_call.assert_awaited_with(native, {"key": "agent:attached:session",
                                    **({"deleteTranscript": True} if method == "threads.delete" else {})})
                        with patch.object(adapter.gateway, "call", new_callable=AsyncMock,
                                          return_value={"subscribed": True}) as native_call:
                            self.assertTrue((await rpc(first, "sessions.subscribe"))["ok"])
                            native_call.assert_awaited_once_with("sessions.subscribe", {})
                            await adapter.native_event({"event": "sessions.changed", "payload": {"sessionKey": "do-not-forward"}})
                            changed = await first.receive_json(timeout=3)
                            self.assertEqual("sessions.changed", changed["type"])
                            self.assertNotIn("do-not-forward", json.dumps(changed))
                            self.assertTrue((await rpc(first, "sessions.unsubscribe"))["ok"])
                        with patch.object(adapter.gateway, "call", new_callable=AsyncMock) as native_call:
                            renamed = await rpc(first, "threads.update", threadId=thread_id,
                                                title="New title", clientRequestId="rename-once")
                            self.assertTrue(renamed["ok"], renamed)
                            for _ in range(40):
                                done = await rpc(first, "operations.get", operationId=renamed["result"]["operationId"])
                                if done["result"]["status"] == "completed":
                                    break
                                await asyncio.sleep(.01)
                            self.assertEqual("completed", done["result"]["status"])
                            native_call.assert_awaited_once_with("sessions.patch", {
                                "key": "agent:attached:session", "label": "New title"})
                        with (patch.object(adapter.gateway, "call", new_callable=AsyncMock, return_value={}) as native_call,
                              patch.object(adapter.gateway, "retain_session", new_callable=AsyncMock,
                                           return_value=(None, "fixture-observer"))):
                            attachment = {"type": "image", "mimeType": "image/png", "fileName": "image.png",
                                          "content": base64.b64encode(b"\xfb" * (1024 * 1024)).decode()}
                            self.assertEqual("INVALID_ARGUMENT", (await rpc(first, "runs.create", threadId=thread_id, message="Fixture message",
                                expectedCurrentSegmentId=segment_id, clientRequestId="bad-image",
                                attachments=[{**attachment, "fileName": "../image.png"}]))["error"]["code"])
                            self.assertEqual("INVALID_ARGUMENT", (await rpc(first, "runs.create", threadId=thread_id, message="Fixture message",
                                expectedCurrentSegmentId=segment_id, clientRequestId="too-many-images",
                                attachments=[{**attachment, "content": "YQ=="}] * 6))["error"]["code"])
                            large_image = {**attachment, "content": base64.b64encode(b"x" * (6 * 1024 * 1024 + 1)).decode()}
                            self.assertEqual("REQUEST_TOO_LARGE", (await rpc(first, "runs.create", threadId=thread_id, message="Fixture message",
                                expectedCurrentSegmentId=segment_id, clientRequestId="oversized-image",
                                attachments=[large_image]))["error"]["code"])
                            sent = await rpc(first, "runs.create", threadId=thread_id, message="Fixture message", attachments=[attachment],
                                             expectedCurrentSegmentId=segment_id, clientRequestId="send-once")
                            self.assertTrue(sent["ok"], sent)
                            for _ in range(40):
                                done = await rpc(first, "operations.get", operationId=sent["result"]["operationId"])
                                if done["result"]["status"] == "completed":
                                    break
                                await asyncio.sleep(.01)
                            self.assertEqual("completed", done["result"]["status"])
                            method, payload = native_call.await_args.args
                            self.assertEqual(("chat.send", "agent:attached:session"), (method, payload["sessionKey"]))
                            self.assertEqual([attachment], payload["attachments"])
                            run_id = done["result"]["result"]["runId"]
                            detail = await rpc(first, "threads.get", threadId=thread_id)
                            self.assertEqual(run_id, detail["result"]["runActivity"]["activeRunId"])

                    second, default_id = await open_chat("attached", template_identify="catalog-secondary")
                    async with second:
                        self.assertEqual(attached["agentId"], default_id)
                        resumed = await rpc(second, "messages.list", **args,
                                            cursor=forward["result"]["newerCursor"], limit=1)
                        self.assertTrue(resumed["ok"], resumed)
                        self.assertEqual(seen["large"]["statistics"], resumed["result"]["items"][0]["statistics"])
                        groups = (await rpc(second, "chat.sidebar"))["result"]["items"]
                        self.assertNotIn("templateIdentify", groups[0])
                        self.assertEqual("catalog-secondary", groups[1]["templateIdentify"])
                        rows = (await rpc(second, "threads.list", agentId=attached["agentId"]))["result"]["items"]
                        self.assertEqual(thread_id, rows[0]["threadId"])
                        self.assertEqual(run_id, rows[0]["runActivity"]["activeRunId"])
                        self.assertEqual(1, len(pool.services))
                        with (sessions / f"{physical}.jsonl").open("a") as transcript:
                            transcript.write(json.dumps({"type": "message", "id": "m3", "parentId": "m2",
                                "runId": payload["idempotencyKey"], "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": "Final"}]}}) + "\n")
                            transcript.write(json.dumps({"type": "message", "id": "m4", "parentId": "m3",
                                "message": {"role": "toolResult", "toolName": "video_generate", "toolCallId": "call-four",
                                    "details": {"count": 1, "provider": "fixture", "model": "sample/video",
                                                "durationSeconds": 8, "paths": ["/private/internal.mp4"],
                                                "task": {"taskId": "task-four", "runId": payload["idempotencyKey"]}}}}) + "\n")
                            transcript.write(json.dumps({"type": "message", "id": "m5", "parentId": "m4",
                                "message": {"role": "toolResult", "toolName": "music_generate", "toolCallId": "call-five",
                                    "details": {"count": 2, "provider": "fixture", "model": "sample/music",
                                                "durationSeconds": 30, "lyrics": ["must not return lyrics"]}}}) + "\n")
                            transcript.write(json.dumps({"type": "message", "id": "m6", "parentId": "m5",
                                "message": {"role": "toolResult", "toolName": "tts", "toolCallId": "call-six",
                                    "details": {"provider": "fixture", "audioPath": "/private/internal.mp3"}}}) + "\n")
                        usage = await rpc(second, "sessions.usage.logs", threadId=thread_id, segmentId=segment_id)
                        correlated = next(row for row in usage["result"]["logs"] if row["messageId"] == "m3")
                        self.assertEqual(run_id, correlated["runId"])
                        output = next(row for row in usage["result"]["logs"] if row["messageId"] == "m4")
                        self.assertEqual(("call-four", run_id), (output["toolCallId"], output["runId"]))
                        self.assertEqual({"kind": "video", "outputCount": 1, "provider": "fixture", "model": "sample/video",
                                          "durationSeconds": 8, "taskId": "task-four"}, output["mediaGeneration"])
                        self.assertNotIn("/private/", json.dumps(output))
                        self.assertNotIn("cost", output)
                        audio = [row["mediaGeneration"] for row in usage["result"]["logs"] if
                                 row.get("mediaGeneration", {}).get("kind") == "audio"]
                        self.assertEqual([2, 1], [row["outputCount"] for row in audio])
                        self.assertNotIn("must not return lyrics", json.dumps(audio))
                        self.assertNotIn("/private/", json.dumps(audio))
                        with patch.object(adapter.gateway, "release_session", new_callable=AsyncMock) as release:
                            await adapter.native_event({"event": "chat", "payload": {"runId": payload["idempotencyKey"],
                                "sessionKey": "agent:attached:session", "state": "final",
                                "message": {"id": "m3", "role": "assistant", "content": "Final"}}})
                            await asyncio.gather(*adapter.observer_releases)
                            release.assert_awaited_once()
                        detail = (await rpc(second, "threads.get", threadId=thread_id))["result"]
                        self.assertIsNone(detail["runActivity"]["activeRunId"])
                        self.assertEqual("completed", detail["runActivity"]["lastRun"]["status"])
                        self.assertEqual("unverified", detail["runActivity"]["lastRun"]["resultBoundary"]["status"])
                        usage = await rpc(second, "sessions.usage.logs", threadId=thread_id, segmentId=segment_id)
                        final_row = next(row for row in usage["result"]["logs"] if row["messageId"] == "m3")
                        self.assertTrue(final_row["isFinalAssistantMessage"])

                    different, _ = await open_chat("attached", subject="other-operator")
                    async with different:
                        denied = await rpc(different, "runs.cancel", runId=run_id, clientRequestId="other-operator-cancel")
                        self.assertEqual("FORBIDDEN", denied["error"]["code"])
                    owner_again, _ = await open_chat("attached")
                    async with owner_again:
                        already_complete = await wait_operation(owner_again, await rpc(owner_again, "runs.cancel",
                            runId=run_id, clientRequestId="owner-cancel-completed"))
                        self.assertEqual("completed", already_complete["status"])
                        self.assertEqual("completed", already_complete["result"]["status"])
                        key = "agent:attached:session"
                        native_run = "chat1-request"
                        started_at = int(time.time() * 1000) - 720_000
                        for scoped_id in ("other-physical-session", physical):
                            def native_activity(method, params, **kwargs):
                                if method == "chat.history":
                                    self.assertEqual({"sessionKey": key, "limit": 1, "maxChars": 1000}, params)
                                    return {"sessionKey": key, "sessionId": scoped_id,
                                            "sessionInfo": {"hasActiveRun": True, "activeRunIds": [native_run]},
                                            "inFlightRun": {"runId": native_run, "startedAt": started_at}}
                                if method in ("sessions.subscribe", "agent.wait"):
                                    return {}
                                if method == "chat.abort":
                                    self.assertEqual({"sessionKey": key, "runId": native_run}, params)
                                    return {"aborted": True, "runIds": [native_run]}
                                return scoped_titles(method, params, **kwargs)
                            gateway_call.side_effect = native_activity
                            with patch.object(adapter.gateway, "retain_session", new_callable=AsyncMock) as retain:
                                retain.return_value = (object(), (key, "attached"))
                                detail = (await rpc(owner_again, "threads.get", threadId=thread_id))["result"]
                            active = detail["runActivity"]
                            if scoped_id != physical:
                                self.assertEqual([], active["activeRuns"])
                                retain.assert_not_awaited()
                            else:
                                retain.assert_awaited_once_with(key, "attached")
                                self.assertEqual(started_at, active["activeRuns"][0]["startedAt"])
                        external_id = active["activeRunId"]
                        self.assertTrue(external_id.startswith("run_"))
                        external = (await rpc(owner_again, "runs.get", runId=external_id))["result"]
                        self.assertEqual(("openclaw", "running", started_at, thread_id),
                            (external["source"], external["status"], external["createdAt"], external["threadId"]))
                        self.assertNotIn(native_run, json.dumps(external))
                        self.assertTrue((await rpc(owner_again, "events.subscribe", threadId=thread_id))["ok"])
                        tool = {"event": "session.tool", "payload": {"sessionKey": key, "runId": native_run,
                            "stream": "tool", "seq": 1, "data": {"toolCallId": "external-tool", "name": "exec", "phase": "start"}}}
                        await adapter.native_event({**tool, "payload": {**tool["payload"], "sessionKey": "agent:main:recent"}})
                        await adapter.native_event(tool)
                        activity = await owner_again.receive_json(timeout=3)
                        self.assertEqual(("run.activity", external_id, thread_id),
                            (activity["type"], activity["runId"], activity["threadId"]))
                        await adapter.native_event({"event": "chat", "payload": {"sessionKey": key, "runId": native_run,
                            "state": "delta", "seq": 2, "message": {"role": "assistant", "content": "Working"}}})
                        frame = await owner_again.receive_json(timeout=3)
                        self.assertEqual(("message.snapshot", external_id), (frame["type"], frame["runId"]))
                        self.assertEqual("running", (await owner_again.receive_json(timeout=3))["run"]["status"])
                        self.assertEqual("FORBIDDEN", (await rpc(owner_again, "runs.cancel", runtime="two",
                            runId=external_id, clientRequestId="cross-runtime"))["error"]["code"])
                        cancelled = await wait_operation(owner_again, await rpc(owner_again, "runs.cancel",
                            runId=external_id, clientRequestId="cancel-external"))
                        self.assertEqual(("completed", "cancel_requested"),
                            (cancelled["status"], cancelled["result"]["status"]))
                        with patch.object(adapter.gateway, "release_session", new_callable=AsyncMock):
                            await adapter.native_event({"event": "chat", "payload": {"sessionKey": key, "runId": native_run,
                                "state": "aborted", "seq": 3}})
                            await asyncio.gather(*adapter.observer_releases)
                        finished = await owner_again.receive_json(timeout=3)
                        self.assertEqual((external_id, "cancelled"), (finished["run"]["runId"], finished["run"]["status"]))

                    legacy, _ = await open_chat("main", "chat.session")
                    async with legacy:
                        self.assertEqual("FORBIDDEN", (await rpc(legacy, "capabilities.get"))["error"]["code"])
                        self.assertEqual("FORBIDDEN", (await rpc(legacy, "chat.sidebar"))["error"]["code"])
                        self.assertEqual("FORBIDDEN", (await rpc(legacy, "usage.status"))["error"]["code"])
                        self.assertEqual("FORBIDDEN", (await rpc(legacy, "sessions.subscribe"))["error"]["code"])
                        self.assertEqual("FORBIDDEN", (await rpc(legacy, "resources.get", resourceId=resource_id,
                            offset=0, length=1))["error"]["code"])
        finally:
            await runner.cleanup()

    async def test_run_keeps_a_native_session_observer_until_terminal(self):
        registration = self.registration()
        adapter = self.service(registration).adapter("one")
        agent = adapter.resolve_agent("purchase-A")
        sessions = self.root / "one/config/agents/main/sessions"
        sessions.mkdir(parents=True)
        key = "agent:main:dashboard:one"
        (sessions / "sessions.json").write_text(json.dumps({key: {"sessionId": "physical"}}))
        thread = adapter.threads(agent)[0]
        socket = object()
        observer = (socket, (key, "main"))
        adapter.gateway.connect = AsyncMock()
        adapter.gateway.retain_session = AsyncMock(return_value=observer)
        adapter.gateway.release_session = AsyncMock()
        adapter.gateway.call = AsyncMock(return_value={})

        sent = await adapter.native_call("runs.create", {"threadId": thread["threadId"],
            "expectedCurrentSegmentId": thread["currentSegmentId"], "clientRequestId": "send-one", "message": "hello"})
        adapter.gateway.retain_session.assert_awaited_once_with(key, "main")
        native = adapter.gateway.call.await_args.args[1]["idempotencyKey"]
        self.assertIs(adapter.gateway.call.await_args.kwargs["expected_socket"], socket)
        adapter.gateway.release_session.assert_not_awaited()
        await adapter.native_event({"event":"chat", "payload":{"sessionKey":key, "runId":native, "state":"delta", "seq":10}})
        adapter.gateway.release_session.assert_not_awaited()
        await adapter.native_event({"event":"chat", "payload":{"sessionKey":key, "runId":native, "state":"final", "seq":2}})
        await asyncio.sleep(0)
        adapter.gateway.release_session.assert_awaited_once_with(observer)
        self.assertEqual(adapter.store.get(sent["runId"])["data"]["status"], "completed")
        await adapter.native_event({"event":"chat", "payload":{"sessionKey":key, "runId":native, "state":"delta", "seq":11}})
        self.assertEqual(adapter.store.get(sent["runId"])["data"]["status"], "completed")

    async def test_native_error_does_not_publish_or_persist_provider_diagnostic(self):
        adapter = self.service(self.registration()).adapter("one")
        key = "agent:main:fixture"
        agent = adapter.resolve_agent("purchase-A")
        thread_id = adapter.register_thread(agent, key)
        run_id = adapter.store.object("run", "one", "native-error-run",
                                      {"status": "running", "threadId": thread_id})
        adapter.publish = AsyncMock()
        await adapter.native_event({"event": "chat", "payload": {"sessionKey": key, "runId": "native-error-run",
            "state": "error", "errorMessage": "private-provider-diagnostic",
            "message": {"role": "assistant", "content": "private-provider-diagnostic"}}})
        adapter.publish.assert_awaited_once()
        self.assertNotIn("private-provider-diagnostic", str(adapter.publish.await_args))
        saved = adapter.store.get(run_id)["data"]
        self.assertEqual(("failed", "RUN_FAILED", False),
                         (saved["status"], saved["error"]["code"], saved["error"]["retryable"]))
        self.assertNotIn("private-provider-diagnostic", json.dumps(saved))

    async def test_native_session_observation_is_reference_counted_and_pinned_to_socket(self):
        gateway = self.service(self.registration()).adapter("one").gateway
        gateway.ws = type("Socket", (), {"closed": False, "close": AsyncMock()})()
        gateway.connect = AsyncMock()
        gateway.call = AsyncMock(return_value={"subscribed": True, "key": "agent:main:thread"})
        first = await gateway.retain_session("agent:main:thread", "main")
        second = await gateway.retain_session("agent:main:thread", "main")
        self.assertEqual(first, second)
        gateway.call.assert_awaited_once()
        await gateway.release_session(first)
        gateway.call.assert_awaited_once()
        await gateway.release_session(second)
        self.assertEqual(gateway.call.await_count, 2)
        self.assertEqual(gateway.call.await_args.args[0], "sessions.messages.unsubscribe")
        self.assertIs(gateway.call.await_args.kwargs["expected_socket"], gateway.ws)
        self.assertFalse(gateway.session_observers)

    async def test_run_status_falls_back_to_exact_native_completion(self):
        adapter = self.service(self.registration()).adapter("one")
        run_id = adapter.store.object("run", "one", "native-one", {
            "status": "running", "threadId": "thread-one", "nativeRunId": "native-alias",
        })
        row = adapter.store.get(run_id)
        adapter.gateway.call = AsyncMock(side_effect=[
            {"status": "timeout", "runId": "native-alias"},
            {"status": "ok", "runId": "native-unrelated", "endedAt": 123},
            {"status": "ok", "runId": "native-alias", "endedAt": 123},
        ])
        adapter.publish = AsyncMock()
        self.assertEqual((await adapter.refresh_run_outcome(row))["status"], "running")
        self.assertEqual((await adapter.refresh_run_outcome(row))["status"], "running")
        self.assertEqual((await adapter.refresh_run_outcome(row))["status"], "completed")
        adapter.publish.assert_awaited_once()
        self.assertEqual(adapter.gateway.call.await_args.args,
                         ("agent.wait", {"runId": "native-alias", "timeoutMs": 1}))

    async def test_two_runtimes_same_native_path_remain_independent(self):
        first, second = self.registration(), self.registration("two", 18002)
        service = self.service(first, second)
        a, b = service.adapter("one"), service.adapter("two")
        self.assertNotEqual(a.resolve_agent("purchase-A"), b.resolve_agent("purchase-A"))
        self.assertNotEqual(a.gateway.device_path, b.gateway.device_path)
        self.assertEqual(str(a.host_binding.local_path("/home/node/.openclaw/workspace/result.txt")),
                         str(self.root / "one/workspace/result.txt"))
        with self.assertRaises(APIError):
            a.host_binding.local_path("/etc/passwd")
        with self.assertRaises(APIError):
            a.host_binding.local_path("/home/node/.openclaw/workspace/../openclaw.json")

    async def test_generation_changes_device_not_business_ids_and_reinstall_revokes_ids(self):
        reg = self.registration()
        service = self.service(reg)
        adapter = service.adapter("one")
        agent = adapter.resolve_agent("purchase-A")
        sessions = self.root / "one/config/agents/main/sessions"
        sessions.mkdir(parents=True)
        key = "agent:main:dashboard:one"
        (sessions / "sessions.json").write_text(json.dumps({key: {"sessionId": "physical"}}))
        thread = adapter.threads(agent)[0]["threadId"]
        changed = copy.deepcopy(reg)
        changed["hostBinding"]["bindingId"] = "generation-2"
        other = self.service(changed).adapter("one")
        self.assertEqual(agent, other.resolve_agent("purchase-A"))
        self.assertEqual(thread, other.threads(agent)[0]["threadId"])
        self.assertNotEqual(adapter.gateway.device_path, other.gateway.device_path)
        changed["hostBinding"]["agentBindings"][0]["installationId"] = "new-install"
        # Keep the same database: old mappings must not grant the replacement access.
        adapter.registration["hostBinding"] = changed["hostBinding"]
        adapter.host_binding = HostBinding(changed, adapter.context.config_path)
        self.assertNotEqual(agent, adapter.resolve_agent("purchase-A"))
        with self.assertRaises(APIError):
            adapter.resolve_scope("threads.list", {"agentId": agent})
        with self.assertRaises(APIError):
            adapter.resolve_scope("messages.list", {"threadId": thread})
        self.assertNotEqual(thread, adapter.threads(adapter.resolve_agent("purchase-A"))[0]["threadId"])

    async def test_reject_wrong_workspace_duplicate_binding_and_symlink(self):
        reg = self.registration()
        bad = copy.deepcopy(reg)
        bad["hostBinding"]["agentBindings"][0]["workspace"] += "/wrong"
        with self.assertRaises((ValueError, APIError)):
            self.service(bad)
        bad = copy.deepcopy(reg)
        bad["hostBinding"]["agentBindings"][1]["nativeAgentId"] = "main"
        with self.assertRaises((ValueError, APIError)):
            self.service(bad)
        if hasattr(Path, "symlink_to"):
            link = self.root / "linked"
            try:
                link.symlink_to(self.root / "one/config", target_is_directory=True)
            except OSError:
                return
            bad = copy.deepcopy(reg)
            bad["configPath"] = str(link / "openclaw.json")
            with self.assertRaises((ValueError, APIError)):
                self.service(bad)

    async def test_strict_read_rejects_parent_link_even_after_legacy_check(self):
        from agent_manage.segment_reader import safe_file
        self.registration()
        sessions = self.root / "one/config/agents/main/sessions"
        sessions.mkdir(parents=True)
        outside = self.root / "private"
        outside.mkdir()
        (outside / "sessions.json").write_text('{"secret":true}')
        sessions.rmdir()
        sessions.symlink_to(outside, target_is_directory=True)
        path = sessions / "sessions.json"
        # Existing safe_file accepts a linked root alias. Strict Host reads must not.
        self.assertEqual(safe_file(path, [path.parent]), outside / "sessions.json")
        with self.assertRaises(OSError):
            read_without_links(path, 1024)
        with self.assertRaises(ValueError):
            open_without_links(self.root / "one/config/../config/openclaw.json")
        with self.assertRaises(ValueError):
            read_without_links(self.root / "one/config/openclaw.json", 1)

    async def test_reject_shared_endpoint_token_and_local_mounts(self):
        a, b = self.registration(), self.registration("two", 18002)
        b["gatewayUrl"] = a["gatewayUrl"]
        with self.assertRaises(ValueError):
            self.service(a, b)

        b["gatewayUrl"] = "ws://127.0.0.1:18002"
        config = Path(b["configPath"])
        data = json.loads(config.read_text())
        data["gateway"]["auth"]["token"] = "fixture-one"
        config.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.service(a, b)

    async def test_manager_state_cannot_be_mounted_into_tenant(self):
        registration = self.registration()
        with patch("agent_manage.adapters.openclaw.factory.detect_version", return_value="2026.7.1-2"):
            with self.assertRaises(ValueError):
                build_service({"stateDirectory": str(self.root / "one/config/private-manager"),
                               "runtimes": [registration]})

    async def test_native_confirmation_requires_path_and_matching_raw_hash(self):
        reg = self.registration()
        binding = HostBinding(reg, Path(reg["configPath"]))
        raw = Path(reg["configPath"]).read_bytes()
        good = {"path": reg["hostBinding"]["nativeConfigPath"], "hash": hashlib.sha256(raw).hexdigest()}
        binding.confirm(good, raw)
        for update in ({"path": "/wrong/openclaw.json"}, {"hash": "0" * 64}, {"hash": None}):
            with self.assertRaises(APIError) as error:
                binding.confirm({**good, **update}, raw)
            self.assertEqual(error.exception.code, "RUNTIME_MISMATCH")

    async def test_host_mode_disables_management_and_media_even_with_wildcard_scope(self):
        adapter = self.service(self.registration()).adapter("one")
        for method in ("system.info", "capabilities.get", "operations.get", "operations.lookup"):
            self.assertEqual(adapter.capabilities()["methods"][method]["support"], "adapted")
        for method in ("agents.create", "dola.runtime.configure", "runtimes.recover", "resources.get", "models.select"):
            self.assertEqual(adapter.capabilities()["methods"][method]["support"], "unsupported")
            with self.assertRaises(APIError) as error:
                await adapter.invoke(method, {})
            self.assertEqual(error.exception.code, "UNSUPPORTED")

    async def test_private_resolver_scoped_tickets_and_native_container_history(self):
        a, b = self.registration(), self.registration("two", 18002)
        service = self.service(a, b)
        runner = web.AppRunner(create_app(service))
        await runner.setup()
        path = self.root / "manager.sock"
        await web.UnixSite(runner, str(path)).start()
        try:
            async with ClientSession(connector=UnixConnector(path=str(path))) as client:
                async def resolve(runtime, identify="purchase-A", **kwargs):
                    async with client.post("http://manager/v1/agents/resolve",
                            json={"runtimeId": runtime, "agentIdentify": identify}, **kwargs) as response:
                        return await response.json()
                first, second = await resolve("one"), await resolve("two")
                self.assertTrue(first["ok"], first)
                self.assertEqual(first["result"]["bindingId"], "generation-1")
                self.assertEqual(first["result"]["installationId"], "install-A")
                self.assertNotEqual(first["result"]["agentId"], second["result"]["agentId"])
                self.assertFalse((await resolve("one", "unknown"))["ok"])
                self.assertEqual((await resolve("one", headers={"Origin": "https://browser.invalid"}))["error"]["code"], "FORBIDDEN")
                agent = first["result"]["agentId"]
                ticket = service.auth.issue_ticket(issuer="dola", subject="fixture", runtime_ids=["one"],
                                                  agent_ids=[agent], actions=["threads.list", "messages.list"])
                lease = service.auth.redeem(ticket)
                native = "/home/node/.openclaw"
                physical = "11111111-1111-4111-8111-111111111111"
                for registration, marker in ((a, "ONLY-A"), (b, "ONLY-B")):
                    sessions = Path(registration["configPath"]).parent / "agents/main/sessions"
                    sessions.mkdir(parents=True)
                    key = "agent:main:dashboard:identical"
                    (sessions / "sessions.json").write_text(json.dumps({key: {"sessionId": physical,
                        "sessionFile": native + "/agents/main/sessions/" + physical + ".jsonl"}}))
                    (sessions / (physical + ".jsonl")).write_text(json.dumps({"type": "message", "id": "m1",
                        "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": [{"type": "text", "text": marker}]}}) + "\n")
                threads = service.adapter("one").threads(agent)
                thread = threads[0]
                page = await service.adapter("one").invoke("messages.list", {"threadId": thread["threadId"],
                    "segmentId": thread["currentSegmentId"]})
                self.assertIn("ONLY-A", json.dumps(page))
                self.assertNotIn("ONLY-B", json.dumps(page))
                with self.assertRaises(APIError):
                    service.authorize(lease, "threads.list", {"runtimeId": "two", "agentId": second["result"]["agentId"]})
                attached = await resolve("one", "purchase-B")
                with self.assertRaises(APIError):
                    service.authorize(lease, "threads.list", {"runtimeId": "one", "agentId": attached["result"]["agentId"]})
                # A root link appearing after registration must not redirect reads.
                sessions = Path(a["configPath"]).parent / "agents/main/sessions"
                (sessions / "sessions.json").unlink()
                (sessions / "sessions.json").symlink_to(Path(b["configPath"]).parent / "agents/main/sessions/sessions.json")
                with self.assertRaises((APIError, ValueError, PermissionError, ProtocolError)):
                    service.adapter("one").threads(agent)
        finally:
            await runner.cleanup()

    async def test_native_handshake_verifies_device_token_path_hash_and_version(self):
        registration = self.registration()
        override = {}
        observed = []
        async def native(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "event", "event": "connect.challenge", "payload": {"nonce": "native-fixture"}})
            async for message in ws:
                frame = json.loads(message.data)
                if frame["method"] == "connect":
                    params = frame["params"]
                    observed.append(params)
                    device, token = params["device"], params["auth"]["token"]
                    signed = "|".join(["v2", device["id"], "openclaw-control-ui", "webchat", "operator",
                        ",".join(params["scopes"]), str(device["signedAt"]), token, "native-fixture"])
                    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                    Ed25519PublicKey.from_public_bytes(decode(device["publicKey"])).verify(decode(device["signature"]), signed.encode())
                    self.assertEqual(token, "fixture-one")
                    payload = {"protocol": 4, "server": {"version": override.get("version", "2026.7.1-2")}}
                else:
                    payload = {"path": override.get("path", registration["hostBinding"]["nativeConfigPath"]),
                        "hash": override.get("hash", hashlib.sha256(Path(registration["configPath"]).read_bytes()).hexdigest())}
                await ws.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": payload})
            return ws
        app = web.Application()
        app.router.add_get("/", native)
        server = TestServer(app)
        await server.start_server()
        registration["gatewayUrl"] = str(server.make_url("/")).replace("http:", "ws:")
        try:
            for bad in (None, {"path": "/wrong/openclaw.json"}, {"hash": "0" * 64}, {"version": "2026.7.2"}):
                override.clear()
                override.update(bad or {})
                adapter = self.service(registration).adapter("one")
                if bad:
                    with self.assertRaises(APIError) as error:
                        await adapter.gateway.connect()
                    self.assertEqual(error.exception.code, "RUNTIME_MISMATCH")
                    self.assertIsNone(adapter.gateway.hello)
                else:
                    await adapter.gateway.connect()
                    self.assertIsNotNone(adapter.gateway.hello)
                await adapter.gateway.close()
            self.assertEqual(len(observed), 4)
        finally:
            await server.close()
