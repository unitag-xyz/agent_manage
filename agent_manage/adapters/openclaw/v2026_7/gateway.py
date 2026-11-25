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
from ....core.store import canonical, now_ms
from ....file_io import atomic_write_bytes


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class Gateway:
    def __init__(self, url, config_path, device_path, on_event, *, origin=None, expected_version=None, host_binding=None):
        self.url, self.config_path, self.device_path = url, Path(config_path), Path(device_path)
        parsed = urlsplit(url)
        self.origin = origin or urlunsplit(("https" if parsed.scheme == "wss" else "http", parsed.netloc, "", "", ""))
        self.on_event = on_event
        self.session = self.ws = self.reader = None
        self.pending = {}
        self.lock = asyncio.Lock()
        self.session_lock = asyncio.Lock()
        self.session_observers = {}
        self.hello = None
        self.closing = False
        self.paused = False
        self.expected_version = expected_version
        self.host_binding = host_binding
        self.unix_socket = None

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
            if self.closing or self.paused:
                raise APIError("NOT_READY", "Native Gateway transport is not active")
            if self.ws is not None and not self.ws.closed and self.hello is not None:
                return
            if self.session is None:
                transport = aiohttp.UnixConnector(path=self.unix_socket) if self.unix_socket else None
                self.session = aiohttp.ClientSession(connector=transport, timeout=aiohttp.ClientTimeout(total=20))
            try:
                self.ws = await self.session.ws_connect(self.url, origin=self.origin,
                    heartbeat=20, max_msg_size=4 * 1024 * 1024)
                challenge = await asyncio.wait_for(self.ws.receive_json(), 10)
                if challenge.get("event") != "connect.challenge" or not isinstance(challenge.get("payload", {}).get("nonce"), str):
                    raise APIError("NATIVE_PROTOCOL_ERROR", "Gateway did not send a supported challenge")
                if self.host_binding:
                    self.host_binding.read_config()
                config_raw = self.host_binding.config_bytes() if self.host_binding else self.config_path.read_bytes()
                config = json.loads(config_raw)
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
                if self.host_binding:
                    if not config_response.get("ok"):
                        raise APIError("RUNTIME_MISMATCH", "Gateway did not confirm its configuration")
                    self.host_binding.confirm(config_response.get("payload") or {}, config_raw)
                    if self.host_binding.config_bytes() != config_raw:
                        raise APIError("RUNTIME_MISMATCH", "Configuration changed during Gateway handshake")
                elif not config_response.get("ok") or not isinstance(native_path, str) or Path(native_path).resolve() != self.config_path.resolve():
                    raise APIError("RUNTIME_MISMATCH", "Gateway does not confirm the registered configuration path")
                self.hello = hello
                self.session_observers.clear()
                from ....core.runtime import CURRENT_WRITE_GUARD
                guard = CURRENT_WRITE_GUARD.set(None)
                try:
                    self.reader = asyncio.create_task(self.read_loop())
                finally:
                    CURRENT_WRITE_GUARD.reset(guard)
            except asyncio.CancelledError:
                if self.ws:
                    try:
                        await asyncio.wait_for(self.ws.close(), 0.25)
                    except asyncio.TimeoutError:
                        pass
                raise
            except APIError:
                if self.ws:
                    await self.ws.close()
                raise
            except Exception as exc:
                if self.ws:
                    await self.ws.close()
                raise APIError("NOT_READY", "Cannot connect to the configured Gateway", retryable=True) from exc

    async def retain_session(self, key, agent_id):
        async with self.session_lock:
            await self.connect()
            socket = self.ws
            target = (key, agent_id)
            count = self.session_observers.get(target, 0)
            if count == 0:
                if target not in self.session_observers and len(self.session_observers) >= 128:
                    raise APIError("BUSY", "Native session observation limit reached", retryable=True)
                self.session_observers[target] = 0
                result = await self.call("sessions.messages.subscribe", {"key": key, "agentId": agent_id},
                                         timeout=5, expected_socket=socket)
                if not isinstance(result, dict) or result.get("subscribed") is not True or result.get("key") != key:
                    raise APIError("NATIVE_PROTOCOL_ERROR", "Gateway did not acknowledge the requested session")
            self.session_observers[target] = count + 1
            return socket, target

    async def release_session(self, observer):
        socket, target = observer
        async with self.session_lock:
            if self.ws is not socket or socket.closed:
                return
            count = self.session_observers.get(target, 0)
            if count > 1:
                self.session_observers[target] = count - 1
            elif count == 1:
                self.session_observers[target] = 0
                key, agent_id = target
                try:
                    await self.call("sessions.messages.unsubscribe", {"key": key, "agentId": agent_id},
                                    timeout=5, expected_socket=socket)
                except APIError:
                    # Unknown unsubscribe results remain counted until retry or reconnect.
                    return
                self.session_observers.pop(target, None)

    async def call(self, method, params, *, timeout=30, expected_socket=None):
        await self.connect()
        socket = self.ws
        if expected_socket is not None and socket is not expected_socket:
            raise APIError("NOT_READY", "Native Gateway connection changed before dispatch", retryable=True)
        methods = self.hello.get("features", {}).get("methods")
        if isinstance(methods, list) and method not in methods:
            raise APIError("UNSUPPORTED", "Native Gateway does not advertise this method")
        if len(self.pending) >= 64:
            raise APIError("BUSY", "Gateway request limit reached", retryable=True)
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            frame = {"type": "req", "id": identifier, "method": method, "params": params}
            if method == "chat.send" and params.get("attachments"):
                data = canonical(frame)
                if len(data.encode()) >= 25 * 1024 * 1024:
                    raise APIError("REQUEST_TOO_LARGE", "Attachment message exceeds the native Gateway limit")
                await socket.send_str(data)
            else:
                await socket.send_json(frame)
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
        await self.disconnect()

    async def disconnect(self):
        async with self.lock:
            if self.ws is not None:
                try:
                    await asyncio.wait_for(self.ws.close(), 1)
                except asyncio.TimeoutError:
                    pass
            if self.session is not None:
                await self.session.close()
            if self.reader is not None:
                await asyncio.gather(self.reader, return_exceptions=True)
            self.ws = self.reader = self.session = self.hello = None
            self.session_observers.clear()
