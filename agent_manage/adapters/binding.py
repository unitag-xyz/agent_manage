"""Read a bounded deployment-owned binding from a trusted directory."""

import json
import os
import re

from ..path_io import open_without_links
from .registry import DEFAULT_ENGINE, platform_host_factory


def read_engine_binding(path, runtime):
    if not isinstance(runtime, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,46}[a-z0-9]", runtime):
        return None
    try:
        with os.fdopen(open_without_links(path), "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != 0 or info.st_mode & 0o022:
                return None
            raw = stream.read(16385)
            value = json.loads(raw) if len(raw) <= 16384 else None
    except (ValueError, OSError, RecursionError):
        return None
    if (not isinstance(value, dict) or value.get("instanceIdentify") != runtime
            or not isinstance(value.get("bindingId"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["bindingId"])):
        return None
    try:
        factory = platform_host_factory(value.get("engine", DEFAULT_ENGINE))
    except ValueError:
        return None
    required = {"instanceIdentify", "bindingId", "mountRoot"} | factory.shared_profile_fields
    if value.keys() - {"engine"} != required:
        return None
    return value
