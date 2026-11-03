"""Conservative extra-restart decisions for the standard Host AM installation.

This is a static policy check, not an acknowledgment that async reload completed.
Rules are pinned to the tested Host OpenClaw 2026.7.1 implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path


HOST_INSTALL_ROOT = Path("/opt/unitag-agent-manager")
RELOAD_SOURCE_HASHES = {
    "config-reload-plan-e0nw5b4_.js": "51d28ccdcd69417b2d07f93028467bd474a1c4c847f58499ebd5d5cb5ec95c8f",
    "config-reload-settings-BUK2a-7x.js": "1ee163745067965762c9aaae39d4e001d2490be2b74fdff3e0a7a77cce101a11",
    "server-reload-handlers-CS73F_Pk.js": "f8b3b2ccb97f7ac777588e5acd0167e9960c8c46da409014d6bc29d5b5212ff7",
}
_MISSING = object()


def standard_host_reload_available(binary: str, *, container_runtime: bool) -> bool:
    if not container_runtime or os.environ.get("UNITAG_AGENT_MANAGER_ACTIVATION_MODE"):
        return False
    # Host uses a separate release mount. Fly's /opt/unitag/agent-manager must not opt in.
    if not Path(__file__).resolve().is_relative_to(HOST_INSTALL_ROOT):
        return False
    try:
        executable = shutil.which(binary)
        if executable is None:
            return False
        package = Path(executable).resolve().parent
        if json.loads((package / "package.json").read_text())["version"] != "2026.7.1":
            return False
        return all(hashlib.sha256((package / "dist" / name).read_bytes()).hexdigest() == digest
                   for name, digest in RELOAD_SOURCE_HASHES.items())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _changed_fields(before, after, path=()):
    if isinstance(before, dict) and isinstance(after, dict):
        return [field for key in before.keys() | after.keys()
                for field in _changed_fields(before.get(key, _MISSING), after.get(key, _MISSING), (*path, key))]
    if before is _MISSING and isinstance(after, dict) and after:
        return [field for key, value in after.items() for field in _changed_fields(_MISSING, value, (*path, key))]
    if after is _MISSING and isinstance(before, dict) and before:
        return [field for key, value in before.items() for field in _changed_fields(value, _MISSING, (*path, key))]
    if before is not _MISSING and after is not _MISSING and type(before) is type(after):
        # JSON encoding distinguishes false/0 and true/1 inside lists as well.
        if json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True):
            return []
    return [path]


def _field_kind(path):
    hot_prefixes = (
        ("agents", "list"), ("agents", "defaults", "model"), ("agents", "defaults", "models"),
        ("agents", "defaults", "imageGenerationModel"), ("agents", "defaults", "mediaModels"),
    )
    if any(path[:len(prefix)] == prefix for prefix in hot_prefixes):
        return "hot"
    if path == ("models", "mode"):
        return "hot"
    if len(path) >= 4 and path[:2] == ("models", "providers") and path[3] in {"baseUrl", "api", "apiKey", "models"}:
        return "hot"
    if path[:2] == ("models", "pricing"):
        return "restart"
    if path in {
        ("gateway", "auth", "mode"), ("gateway", "auth", "token"),
        ("gateway", "controlUi", "allowedOrigins"),
        ("gateway", "controlUi", "dangerouslyDisableDeviceAuth"),
    }:
        return "restart"
    if path in {
        ("tools", "profile"), ("tools", "exec", "security"),
        ("tools", "web", "search", "enabled"), ("tools", "web", "fetch", "enabled"),
        ("tools", "web", "fetch", "useTrustedEnvProxy"),
        ("tools", "agentToAgent", "enabled"), ("tools", "agentToAgent", "allow"),
        ("tools", "sessions", "visibility"), ("meta", "lastTouchedAt"), ("meta", "lastTouchedVersion"),
    }:
        return "none"
    return None


def host_restart_required(before: dict, after: dict) -> bool:
    try:
        reload = before.get("gateway", {}).get("reload", {})
        mode = reload.get("mode", "hybrid")
        debounce = reload.get("debounceMs", 300)
        if (mode not in ("hybrid", "hot", "restart", "off") or
                set(reload) - {"mode", "debounceMs"} or type(debounce) is not int or not 0 <= debounce <= 300):
            return True
        if _changed_fields(reload, after.get("gateway", {}).get("reload", {})):
            return True
        changes = _changed_fields(before, after)
        if not changes:
            return False
        kinds = [_field_kind(path) for path in changes]
        if None in kinds or mode == "off":
            return True
        # Custom plugin registrations may override the built-in reload plan.
        plugins = before.get("plugins", {})
        if (plugins.get("load") or plugins.get("installs") or
                set(plugins.get("entries", {})) - {"openclaw-weixin", "qqbot", "feishu"}):
            return True
        return mode == "hot" and "restart" in kinds
    except (AttributeError, TypeError, ValueError):
        return True
