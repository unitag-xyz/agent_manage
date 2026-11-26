"""Strict read-only POSIX opens for files in tenant-mutable Host mounts."""
import os
import stat
from pathlib import Path


def local_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Host bindings require absolute local paths")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("Host binding paths must not contain links")
    return path.resolve()


def open_without_links(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute traversal-free path")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("Host-bound file access requires POSIX no-follow directory opens")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(file).st_mode):
            os.close(file)
            raise ValueError("Expected a regular file")
        return file
    finally:
        os.close(directory)


def read_without_links(path, limit):
    with os.fdopen(open_without_links(path), "rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Mounted file exceeds the read budget")
    return value
