"""Select the local workload's engine without reading its native configuration."""

import os
import stat
from pathlib import Path

from .binding import read_engine_binding
from .registry import DEFAULT_ENGINE, resolve_engine


def cli_driver(args):
    bootstrap = os.environ.get("UNITAG_AGENT_MANAGER_ENGINE")
    binding_file = os.environ.get("UNITAG_AGENT_MANAGER_BINDING_FILE")
    generation = os.environ.get("UNITAG_AGENT_MANAGER_BINDING_ID")
    if binding_file is None and generation is None:
        return resolve_engine(DEFAULT_ENGINE if bootstrap is None else bootstrap)
    if not binding_file or not generation or os.environ.get("UNITAG_AGENT_MANAGER_RUNTIME") != "container":
        raise ValueError("A bound CLI requires its container binding file and generation")
    path = Path(binding_file)
    if not path.is_absolute() or path.suffix != ".json" or ".." in path.parts:
        raise ValueError("Invalid local engine binding path")
    if any(value is not None for value in (args.config_path, args.openclaw_bin, args.project_dir, args.template_root)):
        raise ValueError("A bound CLI cannot override runtime paths")
    for ancestor in path.parents:
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Local engine binding directory is not trusted")
    profile = read_engine_binding(path, path.stem)
    if profile is None or profile["bindingId"] != generation:
        raise ValueError("Invalid or stale local engine binding")
    engine = profile.get("engine", DEFAULT_ENGINE)
    if bootstrap is not None and bootstrap != engine:
        raise ValueError("Deployment engine conflicts with the current binding")
    return resolve_engine(engine)
