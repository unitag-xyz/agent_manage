"""OpenClaw protocol 4 client. No generic native RPC escape hatch is exposed."""
import asyncio
import base64
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from ....core.errors import APIError
from ....core.store import now_ms
from ....file_io import atomic_write_bytes


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class Gateway:
    def __init__(self, url, config_path, device_path, on_event, *, origin=None, expected_version=None):
        self.url, self.config_path, self.device_path = url, Path(config_path), Path(device_path)
        parsed = urlsplit(url)
        self.origin = origin or urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc, "", "", ""))
        self.on_event = on_event
        self.session = self.ws = self.reader = None
        self.pending = {}
        self.lock = asyncio.Lock()
        self.hello = None
        self.closing = False
        self.expected_version = expected_version

    def device(self, token, nonce):
        path = self.device_path
        if path.is_symlink():
            raise ValueError("Device identity must not be a link")
        if not path.exists():
            key = Ed25519PrivateKey.generate()
            atomic_write_bytes(path, key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                       serialization.NoEncryption()))
        if path.stat().st_mode & 0o077:
            raise ValueError("Device identity must be private")
        key = Ed25519PrivateKey.from_private_bytes(path.read_bytes())
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        identifier, stamp = hashlib.sha256(public).hexdigest(), now_ms()
        scopes = ["operator.read", "operator.write", "operator.admin"]
        payload = "|".join(["v2", identifier, "openclaw-control-ui", "webchat", "operator",
                            ",".join(scopes), str(stamp), token, nonce])
        return scopes, {"id": identifier, "publicKey": b64(public), "signature": b64(key.sign(payload.encode())),
                        "signedAt": stamp, "nonce": nonce}

    async def connect(self):
        async with self.lock:
            if self.ws is not None and not self.ws.closed and self.hello is not None:
                return
            if self.closing:
                raise APIError("NOT_READY", "Adapter is shutting down")
            if self.session is None:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
            try:
                self.ws = await self.session.ws_connect(self.url, origin=self.origin,
                    heartbeat=20, max_msg_size=4 * 1024 * 1024)
                challenge = await asyncio.wait_for(self.ws.receive_json(), 10)
                if challenge.get("event") != "connect.challenge" or not isinstance(challenge.get("payload", {}).get("nonce"), str):
                    raise APIError("NATIVE_PROTOCOL_ERROR", "Gateway did not send a supported challenge")
                config = json.loads(self.config_path.read_text())
                token = config.get("gateway", {}).get("auth", {}).get("token", "")
                if not isinstance(token, str):
                    raise APIError("NATIVE_AUTH_REQUIRED", "Gateway SecretRef requires an explicit credential resolver")
                scopes, device = self.device(token, challenge["payload"]["nonce"])
                identifier = uuid.uuid4().hex
                await self.ws.send_json({"type": "req", "id": identifier, "method": "connect", "params": {
                    "minProtocol": 4, "maxProtocol": 4,
                    "client": {"id": "openclaw-control-ui", "version": "control-ui", "platform": sys.platform, "mode": "webchat"},
                    "role": "operator", "scopes": scopes, "device": device, "caps": ["tool-events"],
                    "auth": {"token": token}, "locale": "en-US"}})
                async def handshake():
                    while True:
                        response = await self.ws.receive_json()
                        if response.get("type") == "res" and response.get("id") == identifier:
                            return response
                response = await asyncio.wait_for(handshake(), 10)
                if not response.get("ok"):
                    error = response.get("error") or {}
                    raise APIError("NATIVE_AUTH_REQUIRED", "Gateway refused the adapter identity; pair or authorize it in OpenClaw",
                        details={"nativeCode": str(error.get("code", "UNKNOWN"))[:80],
                                 "reason": str((error.get("details") or {}).get("code", "UNKNOWN"))[:80]})
                hello = response.get("payload") or {}
                if hello.get("protocol") != 4:
                    raise APIError("UNSUPPORTED_VERSION", "Gateway did not negotiate protocol 4")
                native_version = hello.get("server", {}).get("version", "")
                if self.expected_version and native_version != self.expected_version:
                    raise APIError("RUNTIME_MISMATCH", "Gateway version differs from the registered local package")
                identifier = uuid.uuid4().hex
                await self.ws.send_json({"type": "req", "id": identifier, "method": "config.get", "params": {}})
                config_response = await asyncio.wait_for(handshake(), 10)
                native_path = (config_response.get("payload") or {}).get("path")
                if not config_response.get("ok") or not isinstance(native_path, str) or Path(native_path).resolve() != self.config_path.resolve():
                    raise APIError("RUNTIME_MISMATCH", "Gateway does not confirm the registered configuration path")
                self.hello = hello
                from ....core.runtime import CURRENT_WRITE_GUARD
                guard = CURRENT_WRITE_GUARD.set(None)
                try:
                    self.reader = asyncio.create_task(self.read_loop())
                finally:
                    CURRENT_WRITE_GUARD.reset(guard)
            except APIError:
                if self.ws:
                    await self.ws.close()
                raise
            except Exception as exc:
                if self.ws:
                    await self.ws.close()
                raise APIError("NOT_READY", "Cannot connect to the configured Gateway", retryable=True) from exc

    async def call(self, method, params, *, timeout=30):
        await self.connect()
        methods = self.hello.get("features", {}).get("methods")
        if isinstance(methods, list) and method not in methods:
            raise APIError("UNSUPPORTED", "Native Gateway does not advertise this method")
        if len(self.pending) >= 64:
            raise APIError("BUSY", "Gateway request limit reached", retryable=True)
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.ws.send_json({"type": "req", "id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, ConnectionError, aiohttp.ClientError) as exc:
            raise APIError("DISPATCH_UNKNOWN", "Native request outcome is unknown; do not resend a write automatically") from exc
        finally:
            self.pending.pop(identifier, None)

    async def read_loop(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                frame = json.loads(message.data)
                if frame.get("type") == "res":
                    future = self.pending.get(frame.get("id"))
                    if future is not None and not future.done():
                        if frame.get("ok"):
                            future.set_result(frame.get("payload"))
                        else:
                            future.set_exception(APIError("NATIVE_ERROR", "Native request was rejected",
                                details={"nativeCode": str((frame.get("error") or {}).get("code", "UNKNOWN"))[:80]}))
                elif frame.get("type") == "event":
                    await self.on_event(frame)
        except (ValueError, aiohttp.ClientError):
            pass
        finally:
            self.hello = None
            await self.ws.close()
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(APIError("DISPATCH_UNKNOWN", "Gateway disconnected; outcome is unknown"))
            await self.on_event({"type": "event", "event": "manager.disconnected", "payload": {}})

    async def close(self):
        self.closing = True
        if self.ws is not None:
            await self.ws.close()
        if self.reader is not None:
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.session is not None:
            await self.session.close()
