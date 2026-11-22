"""Opt-in real July Gateway + a local model fixture. No paid model or real auth."""
import asyncio
import json
import os
import shutil
import socket
import tempfile
import unittest
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
    async def test_real_protocol_handshake_send_stream_history_and_abort(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            events, calls = [], []
            async def model(request):
                body = await request.json()
                calls.append(body)
                payload = {"id": "fixture-completion", "object": "chat.completion", "created": 1794480000,
                    "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture reply"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
                if not body.get("stream"):
                    return web.json_response(payload)
                chunk = {**payload, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "fixture reply"}, "finish_reason": None}]}
                last = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                return web.Response(text="data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(last) + "\n\ndata: [DONE]\n\n", content_type="text/event-stream")
            app = web.Application()
            app.router.add_post("/v1/chat/completions", model)
            model_server = TestServer(app)
            await model_server.start_server()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            config_path = root / "openclaw.json"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("Reply briefly. Do not invoke tools for this fixture.\n")
            config_path.write_text(json.dumps({"gateway": {"mode": "local", "port": port, "bind": "loopback",
                "auth": {"mode": "token", "token": "isolated-fixture-token"},
                "controlUi": {"allowedOrigins": [f"http://127.0.0.1:{port}"], "dangerouslyDisableDeviceAuth": True}},
                "logging": {"file": str(root / "native.log")},
                "agents": {"defaults": {"workspace": str(workspace), "model": {"primary": "fixture/fixture"}},
                           "list": [{"id": "main", "workspace": str(workspace)}]},
                "models": {"providers": {"fixture": {"baseUrl": str(model_server.make_url("/v1")), "api": "openai-completions",
                    "apiKey": "isolated-model-fixture", "models": [{"id": "fixture", "name": "Fixture", "contextWindow": 16000, "maxTokens": 1024}]}}}}))
            env = {"PATH": os.environ["PATH"], "HOME": str(root), "OPENCLAW_STATE_DIR": str(root), "OPENCLAW_CONFIG_PATH": str(config_path)}
            logfile = (root / "gateway.log").open("wb")
            process = await asyncio.create_subprocess_exec(shutil.which("node"), os.environ["AGENT_MANAGE_TEST_OPENCLAW_BIN"],
                "gateway", "run", "--allow-unconfigured", "--port", str(port), env=env,
                stdout=logfile, stderr=logfile, start_new_session=True)
            async def event(frame):
                events.append(frame)
            gateway = Gateway(f"ws://127.0.0.1:{port}", config_path, root / "manager/device.key", event, expected_version="2026.7.1-2")
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
                created = await gateway.call("sessions.create", {"agentId": "main"})
                key = created["key"]
                sent = await gateway.call("chat.send", {"sessionKey": key, "message": "Say fixture reply", "deliver": False,
                                                        "idempotencyKey": "fixture-v1-native-run"})
                self.assertTrue(sent.get("runId"))
                for _ in range(300):
                    if any(frame.get("event") == "chat" and frame.get("payload", {}).get("state") in ("final", "error") for frame in events):
                        break
                    await asyncio.sleep(.1)
                terminal = [frame for frame in events if frame.get("event") == "chat" and frame.get("payload", {}).get("state") in ("final", "error")]
                self.assertTrue(terminal, "No terminal event from the native Gateway")
                self.assertEqual(terminal[-1]["payload"]["state"], "final", terminal[-1])
                history = await gateway.call("chat.history", {"sessionKey": key, "limit": 30})
                self.assertIn("fixture reply", json.dumps(history))
                self.assertTrue(calls, "The native Gateway did not call the isolated model endpoint")
                aborted = await gateway.call("chat.abort", {"sessionKey": key, "runId": sent["runId"]})
                self.assertIsInstance(aborted, dict)
            finally:
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
