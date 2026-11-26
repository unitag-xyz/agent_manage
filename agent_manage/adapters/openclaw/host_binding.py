"""Trusted Host registration. Native container paths never resolve in the Host namespace."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from ...core.errors import APIError
from ...core.store import canonical
from ...path_io import local_path, read_without_links


def native_path(value):
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or "\\" in value or "\x00" in value or ".." in value.split("/")):
        raise ValueError("Host bindings require absolute, traversal-free native POSIX paths")
    return PurePosixPath(value)


class HostBinding:
    def __init__(self, registration, config_path):
        value = registration["hostBinding"]
        if not isinstance(value, dict):
            raise ValueError("hostBinding must be an object")
        self.binding_id = value.get("bindingId")
        if not isinstance(self.binding_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", self.binding_id):
            raise ValueError("hostBinding requires a trusted generation bindingId")
        self.native_config = native_path(value.get("nativeConfigPath"))
        self.mappings = []
        for item in value.get("pathMappings", []):
            native, local = native_path(item.get("native")), local_path(item.get("local", ""))
            if native == PurePosixPath("/") or local == Path(local.anchor):
                raise ValueError("Filesystem-root mappings are forbidden")
            if any(native == previous[0] or local == previous[1] for previous in self.mappings):
                raise ValueError("Duplicate or ambiguous path mapping")
            self.mappings.append((native, local))
        self.mappings.sort(key=lambda pair: len(pair[0].parts), reverse=True)
        if not self.mappings or self.local_path(str(self.native_config)) != local_path(registration["configPath"]):
            raise ValueError("Local configPath must map exactly to nativeConfigPath")
        if Path(config_path) != self.local_path(str(self.native_config)):
            raise ValueError("Resolved configuration does not match trusted mapping")
        self.agents = value.get("agentBindings")
        self.instance_chat = value.get("instanceChat") is True
        if not isinstance(self.agents, list) or bool(self.agents) == self.instance_chat:
            raise ValueError("Explicit installed agentBindings are required")
        identities, natives = set(), set()
        for item in self.agents:
            if not isinstance(item, dict):
                raise ValueError("Invalid agent binding")
            for key in ("agentIdentify", "nativeAgentId", "installationId"):
                if not isinstance(item.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", item[key]):
                    raise ValueError("Agent bindings require explicit stable identifiers")
            if item["agentIdentify"] in identities or item["nativeAgentId"] in natives:
                raise ValueError("Ambiguous installed Agent mapping")
            identities.add(item["agentIdentify"])
            natives.add(item["nativeAgentId"])
            self.local_path(str(native_path(item.get("workspace"))))
        if registration.get("lifecycle", "external") != "external" or not registration.get("gatewayUrl"):
            raise ValueError("Host-bound runtimes require external lifecycle and an explicit Gateway")
        self.config_path = Path(config_path)
        config = self.read_config()
        self.token_digest = self.check_token(config)
        self.validate_agents(config)
        self.device_scope = hashlib.sha256(canonical([registration["id"], self.binding_id,
            registration["gatewayUrl"], registration.get("gatewayOrigin"), str(self.native_config),
            str(config_path), [(str(a), str(b)) for a, b in self.mappings]]).encode()).hexdigest()

    def local_path(self, value):
        try:
            path = native_path(value)
            for native, local in self.mappings:
                if path.is_relative_to(native):
                    return local_path(str(local / path.relative_to(native)))
        except (ValueError, TypeError) as exc:
            raise APIError("RUNTIME_MISMATCH", "Unsafe registered native path") from exc
        raise APIError("RUNTIME_MISMATCH", "Native path has no trusted local mount")

    def read_config(self):
        try:
            config = json.loads(self.config_bytes())
            if hasattr(self, "token_digest") and self.check_token(config) != self.token_digest:
                raise APIError("RUNTIME_MISMATCH", "Gateway token changed; reload trusted registration")
            return config
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise APIError("RUNTIME_MISMATCH", "Cannot validate the trusted mounted configuration") from exc

    def config_bytes(self):
        local_path(str(self.config_path))
        return read_without_links(self.config_path, 8 * 1024 * 1024)

    @staticmethod
    def check_token(config):
        auth = config.get("gateway", {}).get("auth", {})
        token = auth.get("token")
        if auth.get("mode", "token") != "token" or not isinstance(token, str) or not token.strip():
            raise ValueError("Host-bound runtimes require an explicit nonempty native Gateway token")
        return hashlib.sha256(token.encode()).hexdigest()

    def validate_agents(self, config):
        agents = config.get("agents", {})
        entries = agents.get("list", [])
        # Do not infer a business installation from the implicit main Agent.
        for binding in self.agents:
            matches = [entry for entry in entries if entry.get("id") == binding["nativeAgentId"]]
            if len(matches) != 1:
                raise APIError("RUNTIME_MISMATCH", "Installed Agent is missing or ambiguous in native configuration")
            workspace = matches[0].get("workspace") or agents.get("defaults", {}).get("workspace")
            if native_path(workspace) != native_path(binding["workspace"]):
                raise APIError("RUNTIME_MISMATCH", "Installed Agent workspace differs from trusted registration")
        return self.agents

    def confirm(self, payload, raw):
        try:
            path = native_path(payload.get("path"))
        except (ValueError, TypeError) as exc:
            raise APIError("RUNTIME_MISMATCH", "Gateway did not confirm its native configuration path") from exc
        if path != self.native_config or payload.get("hash") != hashlib.sha256(raw).hexdigest():
            raise APIError("RUNTIME_MISMATCH", "Gateway configuration snapshot differs from the trusted local mount")
        if self.check_token(json.loads(raw)) != self.token_digest:
            raise APIError("RUNTIME_MISMATCH", "Gateway token changed; update trusted registration before reconnecting")

    def agent_native_identity(self, binding):
        return canonical([binding["nativeAgentId"], binding["agentIdentify"], binding["installationId"]])

    def mappings_json(self):
        return [{"native": str(native), "local": str(local)} for native, local in self.mappings]
