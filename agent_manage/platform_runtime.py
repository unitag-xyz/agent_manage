"""Bounded runtime pool for one OS-isolated Chat pilot container."""

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .adapters.openclaw.host_binding import local_path, native_path
from .adapters.registry import create_runtime_adapter
from .core.errors import APIError
from .core.service import Service
from .core.store import canonical
from .path_io import read_without_links


class PlatformRuntimePool:
    def __init__(self, root, audience, profile):
        expected = {"instanceIdentify", "bindingId", "mountRoot", "gatewayOrigin"}
        optional = {"gatewayUrl", "executable", "gatewaySocket", "nativeVersion"}
        if not isinstance(profile, dict) or not expected <= profile.keys() or profile.keys() - expected - optional:
            raise ValueError("An isolated platform network and mount profile is required")
        self.instance = profile["instanceIdentify"]
        self.generation = profile["bindingId"]
        self.mount = local_path(profile["mountRoot"])
        self.origin = profile["gatewayOrigin"]
        self.gateway_url = profile.get("gatewayUrl", "ws://127.0.0.1:18789")
        self.executable = profile.get("executable", "/app/openclaw.mjs")
        self.gateway_socket = profile.get("gatewaySocket")
        if self.gateway_socket is not None and self.gateway_socket != "/native-gateway/openclaw.sock":
            raise ValueError("Invalid private native gateway socket")
        self.native_version = profile.get("nativeVersion")
        if self.native_version is not None and (self.gateway_socket is None or not isinstance(self.native_version, str)
                or not re.fullmatch(r"2026\.7\.[1-9][0-9]*(?:-[1-9][0-9]*)?", self.native_version)):
            raise ValueError("Invalid native version for private relay")
        gateway = urlsplit(self.gateway_url)
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,46}[a-z0-9]", self.instance)
                or not re.fullmatch(r"[a-f0-9]{64}", self.generation)
                or self.mount == Path(self.mount.anchor) or not isinstance(self.origin, str)
                or gateway.scheme != "ws" or gateway.hostname != "127.0.0.1" or not gateway.port):
            raise ValueError("Invalid isolated platform profile")
        self.root = root / "platform-runs"
        self.audience = audience
        self.services = {}
        self.lock = asyncio.Lock()

    def admits(self, runtime, generation):
        return runtime == self.instance and generation == self.generation

    def registration(self, grant):
        config_path = self.mount / "config/openclaw.json"
        config = json.loads(read_without_links(config_path, 8 * 1024 * 1024))
        agents = config.get("agents", {})
        candidates = [agent for agent in agents.get("list", []) if agent.get("id") == grant.native_agent_id]
        if len(candidates) != 1:
            raise APIError("RUNTIME_MISMATCH", "Authorized native Agent is missing or ambiguous")
        workspace = candidates[0].get("workspace") or agents.get("defaults", {}).get("workspace")
        if not native_path(workspace).is_relative_to(native_path("/home/node/.openclaw")):
            raise APIError("RUNTIME_MISMATCH", "Agent workspace is outside the trusted state mount")
        return {"id": grant.runtime_id, "environmentId": grant.runtime_id, "engine": "openclaw",
            "lifecycle": "external", "home": str(self.mount), "configPath": str(config_path),
            "executable": self.executable, "gatewayUrl": self.gateway_url, "gatewayOrigin": self.origin,
            **({"externalNativeVersion": self.native_version} if self.native_version else {}),
            "hostBinding": {"bindingId": grant.binding_id, "nativeConfigPath": "/home/node/.openclaw/openclaw.json",
                "pathMappings": [{"native": "/home/node/.openclaw", "local": str(self.mount / "config")},
                    {"native": "/home/node/.openclaw/workspace", "local": str(self.mount / "workspace")}],
                **({"instanceChat": True, "agentBindings": []} if grant.instance_chat else
                    {"agentBindings": [{"agentIdentify": grant.agent_identify, "nativeAgentId": grant.native_agent_id,
                        "installationId": grant.installation_id, "workspace": workspace}]})}}

    async def open(self, grant):
        if not self.admits(grant.runtime_id, grant.binding_id):
            raise APIError("RUNTIME_MISMATCH", "Host offer does not match this Manager sandbox")
        identity = ([grant.runtime_id, grant.binding_id, "instance-chat"] if grant.instance_chat else
                    [grant.runtime_id, grant.binding_id, grant.installation_id, grant.agent_identify, grant.native_agent_id])
        key = hashlib.sha256(canonical(identity).encode()).hexdigest()
        async with self.lock:
            current = self.services.get(key)
            if current is None:
                if len(self.services) >= 8:
                    raise APIError("BUSY", "Platform pilot capacity reached", retryable=True)
                registration = self.registration(grant)
                current = Service(self.root / key, audience=self.audience, workers=2)
                try:
                    adapter = create_runtime_adapter(registration, current.store, current.read_workers, current.publish)
                    if not getattr(adapter, "host_binding", None) or not adapter.gateway:
                        raise APIError("UNSUPPORTED", "Native OpenClaw version is not supported")
                    adapter.gateway.unix_socket = self.gateway_socket
                    current.register(adapter, persist_registration=False)
                    await current.start()
                except BaseException:
                    await current.close()
                    raise
                self.services[key] = current
        adapter = current.adapter(grant.runtime_id)
        agent = (await current.read_workers(adapter.resolve_native_agent, grant.native_agent_id) if grant.instance_chat
                 else await current.read_workers(adapter.resolve_agent, grant.agent_identify))
        await adapter.gateway.connect()
        remaining = (grant.expires_at - int(time.time() * 1000)) // 1000
        if remaining <= 0:
            raise APIError("UNAUTHENTICATED", "Platform authorization expired")
        ticket = current.auth.issue_delegated_ticket({"issuer": grant.issuer, "subject": grant.subject,
            "tenantId": grant.tenant_id, "audience": self.audience, "runtimeIds": [grant.runtime_id],
            "agentIds": ["*"] if grant.instance_chat else [agent], "actions": list(grant.actions), "authzVersion": 1,
            "ttlSeconds": 1, "leaseSeconds": min(remaining, 300)})
        return current, current.auth.redeem(ticket), agent

    async def close(self):
        await asyncio.gather(*(service.close() for service in self.services.values()))
