"""Detect the installed OpenClaw version and select its protocol implementation."""

import os
from pathlib import Path
from urllib.parse import urlparse

from ...core.runtime import RuntimeContext
from ..base import RuntimeAdapter, UnsupportedRuntimeAdapter
from .local import executable_search_path
from .version import detect_version, implementation, load_implementation


def create_local(options):
    from .execution import open_local
    return open_local(options)


def validate_cli(args, parser):
    if args.command == "add-agents" and args.workspace_root is None:
        args.workspace_root = "~/data"
    if args.command in {"create-instance", "configure-instance", "activate-instance"}:
        if args.model_key is None and not args.model_key_stdin:
            required = "--model-key-stdin" if args.command == "activate-instance" else "--model-key --model-key-stdin"
            parser.error(f"one of the arguments {required} is required")


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
        path, _ = executable_search_path(context.home, environment.get("PATH", os.environ.get("PATH")))
        version = detect_version(context.executable, path=path)
    if family := implementation(version, "runtime"):
        module = load_implementation(family, "adapter")
        return module.OpenClawAdapter(registration, store, workers, publish, context=context, version=version,
                                      host_binding=binding)
    return UnsupportedRuntimeAdapter(registration, context, engine="openclaw", version=version)
