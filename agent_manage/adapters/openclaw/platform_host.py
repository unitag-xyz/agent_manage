"""OpenClaw-specific profile and connection handling for platform Chat."""

import asyncio
import json
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .host_binding import native_path
from ...core.errors import APIError
from ...path_io import read_without_links


class OpenClawPlatformHost:
    profile_fields = {"gatewayUrl", "executable", "gatewaySocket", "nativeVersion", "gatewayOrigin", "gatewayOriginDomain"}
    shared_profile_fields = {"gatewayOriginDomain", "nativeVersion"}

    @staticmethod
    def shared_profile(profile, relay_root):
        return {**profile, "gatewaySocket": str(relay_root / profile["bindingId"] / "openclaw.sock")}

    def __init__(self, profile, mount, relay_root):
        self.instance = profile["instanceIdentify"]
        self.generation = profile["bindingId"]
        self.mount = mount
        self.origin_domain = profile.get("gatewayOriginDomain")
        if ("gatewayOrigin" in profile) == ("gatewayOriginDomain" in profile):
            raise ValueError("Configure exactly one native Gateway Origin source")
        if "gatewayOriginDomain" in profile:
            if (not isinstance(self.origin_domain, str) or len(self.origin_domain) > 190
                    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
                                        self.origin_domain)):
                raise ValueError("Invalid native Gateway Origin domain")
            self.origin = f"https://{self.instance}.{self.origin_domain}"
        else:
            self.origin = profile["gatewayOrigin"]
        self.gateway_url = profile.get("gatewayUrl", "ws://127.0.0.1:18789")
        self.executable = profile.get("executable", "/app/openclaw.mjs")
        self.gateway_socket = profile.get("gatewaySocket")
        if (self.gateway_socket is not None and self.gateway_socket != "/native-gateway/openclaw.sock"
                and (relay_root is None or self.gateway_socket != str(relay_root / self.generation / "openclaw.sock"))):
            raise ValueError("Invalid private native gateway socket")
        self.native_version = profile.get("nativeVersion")
        if self.native_version is not None and (self.gateway_socket is None or not isinstance(self.native_version, str)
                or not re.fullmatch(r"2026\.7\.[1-9][0-9]*(?:-[1-9][0-9]*)?", self.native_version)):
            raise ValueError("Invalid native version for private relay")
        gateway = urlsplit(self.gateway_url)
        if (not isinstance(self.origin, str) or gateway.scheme != "ws"
                or gateway.hostname != "127.0.0.1" or not gateway.port):
            raise ValueError("Invalid isolated platform profile")

    @property
    def has_private_relay(self):
        return bool(self.gateway_socket)

    def registration(self, grant=None):
        config_path = self.mount / "config/openclaw.json"
        instance_chat = grant is None or grant.instance_chat
        bindings = []
        if not instance_chat:
            workspace = self.granted_workspace(grant)
            bindings.append({"agentIdentify": grant.agent_identify, "nativeAgentId": grant.native_agent_id,
                             "installationId": grant.installation_id, "workspace": workspace})
        return {"id": self.instance, "environmentId": self.instance,
            "lifecycle": "external", "home": str(self.mount), "configPath": str(config_path),
            "executable": self.executable, "gatewayUrl": self.gateway_url, "gatewayOrigin": self.origin,
            **({"externalNativeVersion": self.native_version} if self.native_version else {}),
            "hostBinding": {"bindingId": self.generation, "nativeConfigPath": "/home/node/.openclaw/openclaw.json",
                "pathMappings": [{"native": "/home/node/.openclaw", "local": str(self.mount / "config")},
                    {"native": "/home/node/.openclaw/workspace", "local": str(self.mount / "workspace")}],
                "instanceChat": instance_chat, "agentBindings": bindings}}

    def granted_workspace(self, grant):
        config = json.loads(read_without_links(self.mount / "config/openclaw.json", 8 * 1024 * 1024))
        agents = config.get("agents", {})
        candidates = [agent for agent in agents.get("list", []) if agent.get("id") == grant.native_agent_id]
        if len(candidates) != 1:
            raise APIError("RUNTIME_MISMATCH", "Authorized native Agent is missing or ambiguous")
        workspace = candidates[0].get("workspace") or agents.get("defaults", {}).get("workspace")
        if not native_path(workspace).is_relative_to(native_path("/home/node/.openclaw")):
            raise APIError("RUNTIME_MISMATCH", "Agent workspace is outside the trusted state mount")
        return workspace

    def verify_native_origin(self):
        if self.origin_domain is not None:
            config = json.loads(read_without_links(self.mount / "config/openclaw.json", 8 * 1024 * 1024))
            allowed = config.get("gateway", {}).get("controlUi", {}).get("allowedOrigins")
            if not isinstance(allowed, list) or self.origin not in allowed:
                raise APIError("ORIGIN_MISMATCH", "The generated Gateway Origin is not configured in OpenClaw")

    def prepare(self, adapter, *, idle):
        if not getattr(adapter, "host_binding", None) or not adapter.gateway:
            raise APIError("UNSUPPORTED", "Native OpenClaw version is not supported")
        adapter.gateway.unix_socket = self.gateway_socket
        adapter.gateway.paused = idle

    async def verify_grant(self, grant):
        await asyncio.to_thread(self.verify_native_origin)
        if grant.instance_chat:
            await asyncio.to_thread(self.granted_workspace, grant)

    async def resolve_grant_agent(self, adapter, workers, grant):
        if grant.instance_chat:
            return await workers(adapter.resolve_native_agent, grant.native_agent_id)
        return await workers(adapter.resolve_agent, grant.agent_identify)

    async def verify_readiness(self):
        await asyncio.to_thread(self.verify_native_origin)

    async def probe_shared_relay(self):
        await self.verify_readiness()
        if not stat.S_ISSOCK(Path(self.gateway_socket).lstat().st_mode):
            raise APIError("NOT_READY", "The native relay is unavailable")

    async def connect(self, adapter):
        await adapter.gateway.connect()

    def resume(self, adapter):
        adapter.gateway.paused = False

    def pause(self, adapter):
        adapter.gateway.paused = True

    async def disconnect(self, adapter):
        await adapter.gateway.disconnect()
