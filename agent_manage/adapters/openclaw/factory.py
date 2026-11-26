"""Detect the installed OpenClaw version and select its protocol implementation."""

import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from ...core.runtime import RuntimeContext
from ..base import RuntimeAdapter, UnsupportedRuntimeAdapter


def create_local(options):
    from .execution import open_local
    return open_local(options)


def detect_version(executable: str) -> str:
    binary = shutil.which(executable)
    if binary:
        try:
            package = json.loads((Path(binary).resolve().parent / "package.json").read_text())
            if isinstance(package, dict) and package.get("name") == "openclaw":
                version = package.get("version")
                if isinstance(version, str) and version:
                    return version
        except (OSError, ValueError):
            pass
    return "unknown"


def create_adapter(registration, store, workers, publish) -> RuntimeAdapter:
    lifecycle = registration.get("lifecycle", "external")
    if lifecycle not in ("external", "openclaw"):
        raise ValueError("Runtime lifecycle owner must be external or openclaw")
    environment = registration.get("environment", {})
    if set(environment) & {"OPENCLAW_PROFILE", "OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "HOME"}:
        raise ValueError("Runtime routing cannot be overridden through environment variables")
    if registration.get("gatewayUrl"):
        url = urlparse(registration["gatewayUrl"])
        if url.username or url.password or url.query or url.fragment or url.scheme not in ("ws", "wss"):
            raise ValueError("Gateway URL must be ws/wss without credentials, query or fragment")
        if url.scheme == "ws" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Non-loopback native Gateways require wss")

    binding = None
    if "hostBinding" in registration:
        from .host_binding import HostBinding, local_path
        config_path = local_path(registration["configPath"])
        binding = HostBinding(registration, config_path)
    else:
        config_path = Path(registration["configPath"]).expanduser().resolve()
    context = RuntimeContext(
        runtime_id=registration["id"], environment_id=registration["environmentId"],
        config_path=config_path, state_dir=config_path.parent,
        executable=registration.get("executable", "openclaw"), lifecycle=lifecycle,
        home=Path(registration.get("home", config_path.parent.parent)).expanduser().resolve(),
        inherit_process_env=False, extra_env=tuple(environment.items()),
    )
    # Remote host-bound Chat has no local OpenClaw executable. The adapter still
    # confirms the pinned version and config hash with the native server.
    version = registration.get("externalNativeVersion") if binding else None
    if not isinstance(version, str):
        version = detect_version(context.executable)
    if re.fullmatch(r"2026\.7\.[1-9][0-9]*(?:-[1-9][0-9]*)?", version):
        from .v2026_7.adapter import OpenClawAdapter

        return OpenClawAdapter(registration, store, workers, publish, context=context, version=version,
                               host_binding=binding)
    return UnsupportedRuntimeAdapter(registration, context, engine="openclaw", version=version)
