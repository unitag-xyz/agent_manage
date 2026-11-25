"""Atomic file primitives; callers own backup, locking and rollback policy."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


def fingerprint(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"Expected a regular file, not a link: {path}") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Expected a regular file: {path}")
        content = hashlib.sha256()
        while block := os.read(fd, 128 * 1024):
            content.update(block)
        return {"sha256": content.hexdigest(), "mode": stat.S_IMODE(info.st_mode)}
    finally:
        os.close(fd)


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
