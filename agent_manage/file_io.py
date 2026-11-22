"""Atomic file primitives; callers own backup, locking and rollback policy."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def stage_bytes(path: Path, content: bytes, mode: int = 0o600, *,
                prefix: str = ".agent-manage-", suffix: str = ".tmp") -> Path:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=prefix,
                                         suffix=suffix, delete=False) as handle:
            temporary = Path(handle.name)
            temporary.chmod(mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = stage_bytes(path, content, mode)
    try:
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def encode_json(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sync_directory(path: Path) -> None:
    """Persist rename metadata as well as file data (Linux/macOS local filesystems)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_json(path: Path, payload: object) -> None:
    atomic_write_bytes(path, encode_json(payload))
