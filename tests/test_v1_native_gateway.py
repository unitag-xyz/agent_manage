"""Opt-in real July Gateway + a local model fixture. No paid model or real auth."""
import asyncio
import base64
import json
import os
import shutil
import socket
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

try:
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from agent_manage.adapters.openclaw.v2026_7.gateway import Gateway
    SERVER_AVAILABLE = True
except ModuleNotFoundError:
    SERVER_AVAILABLE = False


@unittest.skipUnless(SERVER_AVAILABLE and os.environ.get("AGENT_MANAGE_TEST_OPENCLAW_BIN"),
                     "Set July OpenClaw and install server extra for native Gateway integration")
class NativeGatewayTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def fixture_png(width=512, height=512):
        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        rows = b"".join(b"\0" + os.urandom(width * 3) for _ in range(height))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

    async def test_real_protocol_handshake_send_stream_history_and_abort(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            events, calls = [], []
            model_started, release_model, model_replied = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def model(request):
                body = await request.json()
                calls.append(body)
                model_started.set()
                await release_model.wait()
                model_replied.set()
                payload = {"id": "fixture-completion", "object": "chat.completion", "created": 1794480000,
                    "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture reply"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
                if not body.get("stream"):
                    return web.json_response(payload)
                chunk = {"id": payload["id"], "created": payload["created"], "model": payload["model"],
                         "object": "chat.completion.chunk", "choices": [
                             {"index": 0, "delta": {"role": "assistant", "content": "fixture reply"}, "finish_reason": None}]}
                last = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                usage = {**chunk, "choices": [], "usage": payload["usage"]}
                return web.Response(text="".join("data: " + json.dumps(item) + "\n\n" for item in (chunk, last, usage))
                                    + "data: [DONE]\n\n", content_type="text/event-stream")
            app = web.Application(client_max_size=4 * 1024 * 1024)
            app.router.add_post("/v1/chat/completions", model)
            model_server = TestServer(app)
            await model_server.start_server()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            config_path = root / "openclaw.json"
            old_key = "agent:main:old-title-fixture"
            session_id = "4d38639c-3ccf-443c-8431-dd7e91ffd295"
            sessions_dir = root / "agents/main/sessions"
            sessions_dir.mkdir(parents=True)
            old_log = sessions_dir / f"{session_id}.jsonl"
            (sessions_dir / "sessions.json").write_text(json.dumps({old_key: {
                "sessionId": session_id, "sessionFile": str(old_log), "updatedAt": 10}}))
            old_log.write_text(json.dumps({"type": "message", "id": "fixture-message", "message": {
                "role": "user", "content": [{"type": "text", "text": "hihi"}]}}) + "\n")
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("Reply briefly. Do not invoke tools for this fixture.\n")
            config_path.write_text(json.dumps({"gateway": {"mode": "local", "port": port, "bind": "loopback",
                "auth": {"mode": "token", "token": "isolated-fixture-token"},
                "controlUi": {"allowedOrigins": [f"http://127.0.0.1:{port}"], "dangerouslyDisableDeviceAuth": True}},
                "logging": {"file": str(root / "native.log")},
                "agents": {"defaults": {"workspace": str(workspace), "model": {"primary": "fixture/fixture"},
                                         "models": {"fixture/fixture": {}, "fixture/alternate": {}}},
                           "list": [{"id": "main", "workspace": str(workspace)}]},
                "models": {"providers": {"fixture": {"baseUrl": str(model_server.make_url("/v1")), "api": "openai-completions",
                    "apiKey": "isolated-model-fixture", "models": [
                        {"id": "fixture", "name": "Fixture", "contextWindow": 131072, "maxTokens": 4096, "input": ["text", "image"]},
                        {"id": "alternate", "name": "Alternate", "contextWindow": 131072, "maxTokens": 4096}]}}}}))
            env = {"PATH": os.environ["PATH"], "HOME": str(root), "OPENCLAW_STATE_DIR": str(root), "OPENCLAW_CONFIG_PATH": str(config_path)}
            logfile = (root / "gateway.log").open("wb")
            process = await asyncio.create_subprocess_exec(shutil.which("node"), os.environ["AGENT_MANAGE_TEST_OPENCLAW_BIN"],
                "gateway", "run", "--allow-unconfigured", "--port", str(port), env=env,
                stdout=logfile, stderr=logfile, start_new_session=True)
            async def event(frame):
                events.append(frame)
            gateway = Gateway(f"ws://127.0.0.1:{port}", config_path, root / "manager/device.key", event,
                expected_version=os.environ.get("AGENT_MANAGE_TEST_OPENCLAW_VERSION", "2026.7.1-2"))
            try:
                for _ in range(300):
                    if process.returncode is not None:
                        self.fail("Isolated Gateway exited: " + (root / "gateway.log").read_text()[-5000:])
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(.1)
                try:
                    await gateway.connect()
                except Exception as error:
                    self.fail(f"Native handshake failed: {getattr(error, 'details', None)}\n" + (root / "gateway.log").read_text()[-6000:])
                self.assertIsNotNone(gateway.hello)
                from agent_manage.adapters.openclaw.v2026_7.thread_titles import enrich_titles
                row = {"title": None}
                targets = {old_key: (session_id, row, False)}
                for expected in ("hihi", "Renamed fixture"):
                    if expected != "hihi":
                        await gateway.call("sessions.patch", {"key": old_key, "label": expected})
                    native = await gateway.call("sessions.describe", {"key": old_key, "includeDerivedTitles": True})
                    self.assertEqual(expected, native["session"]["derivedTitle"])
                    for detail in (False, True):
                        row.clear()
                        await enrich_titles(gateway, targets, detail=detail)
                        self.assertEqual({"title": expected, "derivedTitle": expected}, row)
                created = await gateway.call("sessions.create", {"agentId": "main"})
                key = created["key"]
                other_key = (await gateway.call("sessions.create", {"agentId": "main"}))["key"]
                catalog = await gateway.call("models.list", {"view": "configured"})
                self.assertIn(("fixture", "alternate"), {(m["provider"], m["id"]) for m in catalog["models"]})
                listed = await gateway.call("sessions.list", {"agentId": "main", "search": key, "limit": 16})
                self.assertIs(next(item for item in listed["sessions"] if item["key"] == key)["hasActiveRun"], False)
                config_before = config_path.read_bytes()
                patch = await gateway.call("sessions.patch", {"key": key, "model": "fixture/alternate"})
                self.assertEqual(("fixture", "alternate"), (patch["resolved"]["modelProvider"], patch["resolved"]["model"]))
                self.assertTrue(patch["entry"]["modelOverride"])
                self.assertEqual("alternate", (await gateway.call("sessions.describe", {"key": key}))["session"]["model"])
                self.assertEqual("fixture", (await gateway.call("sessions.describe", {"key": other_key}))["session"]["model"])
                patch = await gateway.call("sessions.patch", {"key": key, "model": None})
                self.assertEqual("fixture", patch["resolved"]["model"])
                self.assertFalse(patch["entry"].get("modelOverride"))
                self.assertEqual(config_before, config_path.read_bytes())
                png = self.fixture_png()
                self.assertGreater(len(png), 256 * 1024)
                sent = await gateway.call("chat.send", {"sessionKey": key, "message": "Say fixture reply", "deliver": False,
                    "idempotencyKey": "fixture-v1-native-run", "attachments": [
                        {"type": "image", "mimeType": "image/png", "fileName": "fixture.png",
                         "content": base64.b64encode(png).decode()},
                        {"type": "image", "mimeType": "image/png", "fileName": "tiny.png",
                         "content": base64.b64encode(self.fixture_png(1, 1)).decode()}]})
                self.assertTrue(sent.get("runId"))
                await asyncio.wait_for(model_started.wait(), 10)
                listed = await gateway.call("sessions.list", {"agentId": "main", "search": key, "limit": 16})
                self.assertIs(next(item for item in listed["sessions"] if item["key"] == key)["hasActiveRun"], True)
                release_model.set()
                for _ in range(300):
                    if any(frame.get("event") == "chat" and frame.get("payload", {}).get("state") in ("final", "error") for frame in events):
                        break
                    await asyncio.sleep(.1)
                terminal = [frame for frame in events if frame.get("event") == "chat" and frame.get("payload", {}).get("state") in ("final", "error")]
                self.assertTrue(terminal, "No terminal event from the native Gateway")
                self.assertEqual(terminal[-1]["payload"]["state"], "final",
                                 (terminal[-1], (root / "gateway.log").read_text()[-6000:]))
                history = await gateway.call("chat.history", {"sessionKey": key, "limit": 30})
                self.assertIn("fixture reply", json.dumps(history))
                self.assertTrue(calls, "The native Gateway did not call the isolated model endpoint")
                model_payload = json.dumps(calls)
                self.assertGreaterEqual(model_payload.count('"type": "image_url"'), 2,
                                        "Native model request did not receive both inline images")
                aborted = await gateway.call("chat.abort", {"sessionKey": key, "runId": sent["runId"]})
                self.assertIsInstance(aborted, dict)

                # Hold a real native run across a transport disconnect, then recover without resending.
                model_started.clear()
                release_model.clear()
                model_replied.clear()
                disconnected_run = await gateway.call("chat.send", {"sessionKey": other_key,
                    "message": "Continue without the WebSocket", "deliver": False,
                    "idempotencyKey": "fixture-disconnected-native-run"})
                await asyncio.wait_for(model_started.wait(), 15)
                await gateway.disconnect()
                self.assertIsNone(gateway.ws)
                release_model.set()
                await asyncio.wait_for(model_replied.wait(), 15)
                outcome = await gateway.call("agent.wait", {"runId": disconnected_run["runId"], "timeoutMs": 15000})
                self.assertEqual(outcome["status"], "ok", outcome)
                history = await gateway.call("chat.history", {"sessionKey": other_key, "limit": 30})
                self.assertIn("fixture reply", json.dumps(history))
                matching = [message for message in history["messages"] if message["role"] == "user"
                            and "Continue without the WebSocket" in json.dumps(message["content"])]
                self.assertEqual(len(matching), 1, "Reconnect must not create a second user turn")

                flags = {"includeDerivedTitles": True, "includeLastMessage": True}
                session = (await gateway.call("sessions.describe", {"key": other_key, **flags}))["session"]
                self.assertTrue(session["derivedTitle"])
                row = {"title": None}
                targets = {other_key: (session["sessionId"], row, False)}
                await enrich_titles(gateway, targets, detail=True)
                self.assertEqual(session["derivedTitle"], row["derivedTitle"])

                # Reconnect before completion too: history alone cannot prove
                # that the replacement socket receives live native events.
                model_started.clear()
                release_model.clear()
                old_observer = await gateway.retain_session(other_key, "main")
                resumed = await gateway.call("chat.send", {"sessionKey": other_key,
                    "message": "Resume live observation", "deliver": False, "idempotencyKey": "fixture-reattach"},
                    expected_socket=old_observer[0])
                await asyncio.wait_for(model_started.wait(), 15)
                await gateway.disconnect()
                observer = await gateway.retain_session(other_key, "main")
                self.assertIsNot(observer[0], old_observer[0])
                boundary = len(events)
                release_model.set()
                async def wait_for_final():
                    while not any(e.get("event") == "chat" and e.get("payload", {}).get("runId") == resumed["runId"]
                                  and e["payload"].get("state") == "final" for e in events[boundary:]):
                        await asyncio.sleep(.05)
                await asyncio.wait_for(wait_for_final(), 30)
                await gateway.release_session(observer)
            finally:
                release_model.set()
                await gateway.close()
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                logfile.close()
                await model_server.close()
