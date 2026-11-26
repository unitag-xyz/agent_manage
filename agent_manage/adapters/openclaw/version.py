"""OpenClaw installation identity and compatibility by capability."""

import json
import re
import shutil
from pathlib import Path

from ..versions import VersionFamily, select_family


_JULY = re.compile(r"2026\.7\.[1-9][0-9]*(?:-[1-9][0-9]*)?")
FAMILIES = (
    VersionFamily("v2026_7", lambda version: _JULY.fullmatch(version) is not None,
                  frozenset({"runtime", "history", "codex_auth"})),
    VersionFamily("v2026_7", lambda version: version == "2026.7.1", frozenset({"host_reload"})),
)


def implementation(version: str, capability: str) -> str | None:
    return select_family(FAMILIES, version, capability)


def load_implementation(family: str, module: str):
    from importlib import import_module

    return import_module(f".{family}.{module}", __package__)


def package_directory(executable: str, *, path: str | None = None) -> Path | None:
    binary = shutil.which(executable, path=path)
    return Path(binary).resolve().parent if binary else None


def package_metadata(package: Path) -> dict:
    metadata = json.loads((package / "package.json").read_text())
    return metadata if isinstance(metadata, dict) else {}


def package_version(package: Path, *, metadata: dict | None = None) -> str:
    if metadata is None:
        metadata = package_metadata(package)
    version = metadata.get("version") if metadata.get("name") == "openclaw" else None
    return version if isinstance(version, str) and version else "unknown"


def detect_version(executable: str, *, path: str | None = None) -> str:
    try:
        package = package_directory(executable, path=path)
        return package_version(package) if package else "unknown"
    except (OSError, ValueError):
        return "unknown"
