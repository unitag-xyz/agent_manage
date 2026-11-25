import asyncio
import base64
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_manage.core.errors import APIError
from agent_manage.core.recovery import recover_refreshes
from agent_manage.refresh_management import RefreshManagementMixin


class RefreshTests(unittest.TestCase):
    def test_recovery_understands_refresh_file_mode_including_sticky_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "openclaw.json"
            config.write_text("{}")
            target = root / "hook"
            target.write_bytes(b"new")
            target.chmod(0o1755)
            backup = root / "agent-manage/backups/job"
            backup.mkdir(parents=True)
            (backup / "file-0").write_bytes(b"old")
            def fp(content):
                return {"sha256": hashlib.sha256(content).hexdigest(), "mode": 0o1755}
            (backup / "manifest.json").write_text(json.dumps({"config_path": str(config), "status": "prepared",
                "recovery_entries": [{"path": str(target), "before": fp(b"old"), "after": fp(b"new"),
                                      "backup_file": "file-0"}]}))
            self.assertEqual(recover_refreshes(config), ["job"])
            self.assertEqual((target.read_bytes(), target.stat().st_mode & 0o7777), (b"old", 0o1755))

    def test_interruption_before_complete_manifest_is_not_a_prepared_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "openclaw.json"
            config.write_bytes(b"{}")
            backup = config.parent / "agent-manage/backups/job"
            backup.mkdir(parents=True)
            RefreshManagementMixin._backup_refresh(SimpleNamespace(config_path=config), backup, [], b"{}", 0o600,
                config.parent / "state", {}, None, "main", "job")
            self.assertEqual([], recover_refreshes(config))
            self.assertEqual(config.read_bytes(), b"{}")


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_v1_host_binding as fixtures
        self.f = fixtures.HostBindingTest()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        reg = self.f.registration()
        reg["hostBinding"].update(instanceChat=True, agentBindings=[])
        self.s = self.f.service(reg)
        self.a = self.s.adapter("one")
        self.key = "agent:main:test"
        self.sessions = self.f.root / "one/config/agents/main/sessions"
        self.sessions.mkdir(parents=True)
        self.sid = "11111111-1111-4111-8111-111111111111"
        (self.sessions / "sessions.json").write_text(json.dumps({self.key: {"sessionId": self.sid}}))
        self.agent = self.a.resolve_native_agent("main")
        self.thread = self.a.threads(self.agent)[0]
        self.g = self.a.gateway
        self.g.connect = AsyncMock()
        self.g.retain_session = AsyncMock(return_value=(object(), (self.key, "main")))
        self.g.release_session = AsyncMock()
        self.lease = self.s.auth.redeem(self.s.auth.issue_ticket(issuer="test", subject="owner", runtime_ids=["one"], actions=["*"]))

    async def write(self, method, **params):
        response = await self.s.dispatch(self.lease, {"requestId": "one", "method": method,
            "params": {"runtimeId": "one", "clientRequestId": "request", **params}})
        self.assertTrue(response["ok"], response)
        await asyncio.gather(*list(self.s.tasks))
        return self.s.store.operation(response["result"]["operationId"], self.lease.context, "one")

    async def send(self):
        return await self.write("runs.create", threadId=self.thread["threadId"],
            expectedCurrentSegmentId=self.thread["currentSegmentId"], message="fixture")

    async def test_confirmed_final_survives_missing_ack(self):
        async def send(method, params, **kw):
            await self.a.native_event({"event": "chat", "payload": {"sessionKey": self.key,
                "runId": params["idempotencyKey"], "state": "final"}})
            raise APIError("DISPATCH_UNKNOWN", "ack lost")
        self.g.call = send
        op = await self.send()
        row, = self.s.store.objects("run", "one")
        self.assertEqual(row["data"]["status"], "completed")
        self.assertIn("completedAt", row["data"])
        self.assertIn(op["status"], ("unknown", "completed"))

    async def test_reconciliation_accepts_only_confirmed_failed_run(self):
        self.g.call = AsyncMock(return_value={})
        operation = await self.send()
        row = self.s.store.get(operation["result"]["runId"])
        self.a.publish = AsyncMock()
        for reply in ({"status": "error", "runId": "other", "endedAt": 123},
                      {"status": "timeout", "runId": row["native"]},
                      {"status": "error", "runId": row["native"]}):
            self.g.call = AsyncMock(return_value=reply)
            self.assertEqual((await self.a.refresh_run_outcome(row))["status"], "running")
        self.g.call = AsyncMock(return_value={"status": "error", "runId": row["native"], "endedAt": 123,
                                              "error": "private diagnostic"})
        data = await self.a.refresh_run_outcome(row)
        self.assertEqual((data["status"], data["completedAt"], data["error"]["code"]), ("failed", 123, "RUN_FAILED"))
        self.assertNotIn("private diagnostic", json.dumps(data))
        self.assertEqual(data, self.s.store.get(row["id"])["data"])
        self.assertNotIn(row["native"], self.a.run_observers)
        self.a.publish.assert_awaited_once()

    async def test_subscription_refusals_do_not_consume_capacity_but_unknown_is_reserved(self):
        from agent_manage.adapters.openclaw.v2026_7.gateway import Gateway
        g = Gateway("ws://localhost", "/unused", "/unused", AsyncMock())
        g.connect = AsyncMock()
        g.ws = SimpleNamespace(closed=False)
        g.call = AsyncMock(side_effect=APIError("NATIVE_ERROR", "refused"))
        for i in range(128):
            with self.assertRaises(APIError):
                await g.retain_session(str(i), "main")
        self.assertFalse(g.session_observers)
        g.call = AsyncMock(side_effect=APIError("DISPATCH_UNKNOWN", "ack lost"))
        with self.assertRaises(APIError):
            await g.retain_session("unknown", "main")
        self.assertIn(("unknown", "main"), g.session_observers)
        g.call = AsyncMock(return_value={"subscribed": False, "key": "negative"})
        with self.assertRaises(APIError):
            await g.retain_session("negative", "main")
        self.assertNotIn(("negative", "main"), g.session_observers)
        g.call = AsyncMock(side_effect=APIError("NATIVE_ERROR", "retry refused"))
        with self.assertRaises(APIError):
            await g.retain_session("unknown", "main")
        self.assertIn(("unknown", "main"), g.session_observers)
        g.call = AsyncMock(return_value={"subscribed": True, "key": "fresh"})
        await g.retain_session("fresh", "main")
        self.assertEqual(g.session_observers[("fresh", "main")], 1)

    async def test_history_resource_batch_and_stable_revisions(self):
        path = self.sessions / (self.sid + ".jsonl")
        def save(text):
            blocks = [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(bytes([i])).decode()}
                      for i in range(32)]
            rows = [{"type": "message", "id": k, "message": {"role": "assistant", "content": content}}
                    for k, content in (("images", blocks), ("big", text * 400000))]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        save("x")
        params = {"threadId": self.thread["threadId"], "segmentId": self.thread["currentSegmentId"]}
        calls = []
        loop_thread = threading.get_ident()
        put = self.s.store.put_objects
        def measured(runtime, items):
            if any(kind == "resource" for kind, _, _ in items):
                calls.append((len(items), threading.get_ident() == loop_thread))
            return put(runtime, items)
        async def page(stamp):
            with patch("agent_manage.segment_reader.time.time", return_value=stamp):
                return (await self.a.invoke("messages.list", {**params, "edge": "start", "limit": 2}))["items"]
        with patch.object(self.s.store, "put_objects", side_effect=measured):
            first = await page(2000000000)
        later = await page(2000000001)
        self.assertEqual(first[0]["content"], later[0]["content"])
        self.assertNotEqual(first[1]["fullMessageRef"], later[1]["fullMessageRef"])
        self.assertEqual([m["revision"] for m in first], [m["revision"] for m in later])
        for stub in later:
            ref = stub.get("fullMessageRef", {}).get("params", {})
            result = await self.a.invoke("messages.get", {**params, "messageId": stub["id"], **ref})
            self.assertEqual(result["message"]["revision"], stub["revision"])
        from agent_manage.response import ProtocolError
        with patch("agent_manage.segment_reader.time.time", return_value=2000007200), self.assertRaises(ProtocolError) as expired:
            await self.a.invoke("messages.get", later[1]["fullMessageRef"]["params"])
        self.assertEqual(expired.exception.code, "CURSOR_EXPIRED")
        save("y")
        self.assertNotEqual((await page(2000000002))[1]["revision"], later[1]["revision"])
        self.assertEqual(calls, [(32, False)])

    async def test_external_attachment_changes_revision_but_not_on_ticket_renewal(self):
        path = self.sessions / (self.sid + ".jsonl")
        media = self.f.root / "one/workspace/sample.png"
        path.write_text(json.dumps({"type": "message", "id": "photo", "message": {"role": "assistant",
            "content": "MEDIA:/home/node/.openclaw/workspace/sample.png"}}) + "\n")
        params = {"threadId": self.thread["threadId"], "segmentId": self.thread["currentSegmentId"], "messageId": "photo"}
        async def read(stamp):
            with patch("agent_manage.segment_reader.time.time", return_value=stamp):
                return (await self.a.invoke("messages.get", params))["message"]
        missing = await read(2000000000)
        same = await read(2000000001)
        self.assertEqual((missing["content"], missing["revision"]), (same["content"], same["revision"]))
        media.write_bytes(b"first")
        first = await read(2000000002)
        media.write_bytes(b"second")
        second = await read(2000000003)
        self.assertEqual(len({missing["revision"], first["revision"], second["revision"]}), 3)

    async def test_explicit_refusal_not_an_active_run(self):
        self.g.call = AsyncMock(side_effect=APIError("NATIVE_ERROR", "refused"))
        op = await self.send()
        self.assertEqual(op["status"], "failed")
        row, = self.s.store.objects("run", "one")
        self.assertEqual(row["data"]["status"], "failed")

    async def test_reconnect_reattaches_without_resend(self):
        self.g.call = AsyncMock(return_value={})
        op = await self.send()
        await self.a.native_event({"event": "manager.disconnected"})
        for _ in range(2):
            await self.a.invoke("events.subscribe", {"runtimeId": "one", "threadId": self.thread["threadId"]})
        self.assertEqual(self.g.retain_session.await_count, 2)
        self.assertEqual(sum(c.args[0] == "chat.send" for c in self.g.call.await_args_list), 1)
        row = self.s.store.get(op["result"]["runId"])
        with patch.object(self.s.store, "objects", side_effect=AssertionError("full table materialization")):
            result = await self.a.invoke("runs.lookup", {"threadId": self.thread["threadId"], "clientRequestId": "request"})
        self.assertEqual(result["items"][0]["runId"], row["id"])

    async def test_rename_refusal_preserves_created_thread_and_receipt(self):
        async def rpc(method, params):
            if method == "sessions.create":
                return {"key": self.key}
            record, = self.s.store.operations(self.lease.context, "one", "request", method="threads.create")
            self.assertEqual(record["result"], {"threadId": self.thread["threadId"], "agentId": self.agent,
                                               "titleStatus": "unknown"})
            raise APIError("NATIVE_ERROR", "refused")
        self.g.call = AsyncMock(side_effect=rpc)
        op = await self.write("threads.create", agentId=self.agent, title="my title")
        self.assertEqual(op["result"]["threadId"], self.thread["threadId"])
        self.assertEqual(op["result"]["titleStatus"], "not_applied")
        self.assertEqual(await self.write("threads.create", agentId=self.agent, title="my title"), op)
        self.assertEqual(self.g.call.await_count, 2)

    async def test_instance_receipt_survives_generation_change(self):
        from agent_manage.platform_runtime import PlatformRuntimePool
        from agent_manage.core.store import Store, canonical
        import hashlib
        profile = {"instanceIdentify": "one", "mountRoot": str(self.f.root / "one"),
                   "gatewayOrigin": "https://one.example", "gatewaySocket": "/native-gateway/openclaw.sock",
                   "nativeVersion": "2026.7.1-2"}
        key = hashlib.sha256(canonical(["one", "a" * 64, "instance-chat"]).encode()).hexdigest()
        legacy = Store(self.f.root / "manager/platform-runs" / key)
        receipt, _ = legacy.claim(self.lease.context, "one", "threads.create", "once", {})
        legacy.finish(receipt["operationId"], "completed", result={"threadId": "original"})
        ids = []
        devices = []
        old_lease = None
        for generation in ("a", "b"):
            pool = PlatformRuntimePool(self.f.root / "manager", "host", {**profile, "bindingId": generation * 64})
            try:
                svc = await pool.runtime_service()
                op, fresh = svc.store.claim(self.lease.context, "one", "threads.create", "once", {})
                ids.append(op["operationId"])
                devices.append(svc.adapter("one").gateway.device_path)
                self.assertFalse(fresh)
                self.assertEqual(op["result"], {"threadId": "original"})
                if generation == "a":
                    old_lease = svc.auth.redeem(svc.auth.issue_ticket(issuer="test", subject="owner",
                        runtime_ids=["one"], actions=["*"])).token
                    svc.store.object("run", "one", "prior", {"status": "running", "threadId": "thread"})
                else:
                    with self.assertRaises(APIError):
                        svc.auth.authenticate(old_lease)
                    self.assertTrue(svc.store.lookup("run", "one", "prior")["data"]["executionDetached"])
                    self.assertEqual(svc.store.thread_runs("one", "thread"), [])
            finally:
                await pool.close()
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(devices[0], devices[1])

    async def test_large_message_hot_pages_and_stale_cursor(self):
        from agent_manage.response import ProtocolError
        path = self.sessions / (self.sid + ".jsonl")
        text = "x" * (1024 * 1024)
        def line(i, value):
            return json.dumps({"type": "message", "id": i, "message": {"role": "user",
                              "content": [{"type": "text", "text": value}]}}) + "\n"
        path.write_text(line("big", text) + line("small", "last"))
        params = {"threadId": self.thread["threadId"], "segmentId": self.thread["currentSegmentId"]}
        cold = self.a.history({**params, "edge": "end", "limit": 1})
        self.assertEqual(cold["timings"]["counts"]["recordsScanned"], 2)
        hot = self.a.history({**params, "messageId": "big"}, "message")
        self.assertEqual(hot["message"]["content"][0]["text"], text)
        self.assertEqual(hot["timings"]["counts"]["snapshotIndexHits"], 1)
        self.assertNotIn("recordsScanned", hot["timings"]["counts"])
        self.assertEqual(hot["timings"]["counts"]["snapshotVerifyBytes"], path.stat().st_size)
        with patch.object(self.a, "history", wraps=self.a.history) as read:
            await self.a.invoke("messages.get", {**params, "messageId": "big"})
            self.assertEqual(read.call_count, 1)
        path.write_text(line("big", "z" * len(text)) + line("small", "last"))
        with self.assertRaises(ProtocolError) as stale:
            self.a.history({**params, "cursor": cold["olderCursor"]})
        self.assertEqual(stale.exception.code, "CURSOR_EXPIRED")
        path.write_text(line("big", "z" * (8 * 1024 * 1024)))
        with self.assertRaises(ProtocolError) as large:
            self.a.history({**params, "messageId": "big"}, "message")
        self.assertEqual(large.exception.code, "MESSAGE_TOO_LARGE")

    async def test_list_batches_mappings_and_delta_never_registers_agents(self):
        index = {f"agent:main:{i}": {"sessionId": self.sid} for i in range(300)}
        (self.sessions / "sessions.json").write_text(json.dumps(index))
        with patch.object(self.s.store, "connect", wraps=self.s.store.connect) as connect:
            self.assertEqual(len(self.a.threads(self.agent)), 300)
            self.assertLessEqual(connect.call_count, 4)
        with patch.object(self.s.store, "put_objects", side_effect=AssertionError("unexpected mapping write")):
            routed = self.a.event_route({"sessionKey": "agent:main:0"})
            self.assertEqual(self.a.native_key(routed[1]), "agent:main:0")
            self.assertIsNone(self.a.event_route({"sessionKey": "agent:other:0"}))

    async def test_media_chunks_reuse_only_the_verified_transcript_index(self):
        import base64
        from agent_manage.response import ProtocolError
        path = self.sessions / (self.sid + ".jsonl")
        media = self.f.root / "one/workspace/photo.png"
        block = 128 * 1024
        media.write_bytes(b"x" * (8 * block))
        rows = (json.dumps({"type": "message", "id": str(i), "message": {"role": "assistant", "content":
                [{"type": "text", "text": "MEDIA:/home/node/.openclaw/workspace/photo.png" if i == 3999 else "hi"}]}})
                for i in range(4000))
        path.write_text("\n".join(rows) + "\n")
        params = {"threadId": self.thread["threadId"], "segmentId": self.thread["currentSegmentId"]}
        page = self.a.history({**params, "limit": 1})
        ref = page["messages"][0]["mediaRefs"][0]["params"]["ref"]
        for i in range(8):
            result = self.a.history({**params, "ref": ref, "mediaOffset": i * block, "mediaLength": block}, "media")
            self.assertEqual(base64.b64decode(result["data"]), b"x" * block)
            counts = result["timings"]["counts"]
            self.assertEqual((counts.get("recordsScanned", 0), counts["snapshotIndexHits"]), (0, 1))
            self.assertEqual(counts["snapshotVerifyBytes"], path.stat().st_size)
        media.write_bytes(b"y" * (8 * block))
        with self.assertRaises(ProtocolError) as stale:
            self.a.history({**params, "ref": ref, "mediaOffset": 0, "mediaLength": block}, "media")
        self.assertEqual(stale.exception.code, "CURSOR_EXPIRED")
