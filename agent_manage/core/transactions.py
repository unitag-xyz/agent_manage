"""One reentrant cross-process writer lock per runtime state directory."""
import fcntl
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import APIError

_guard = threading.Lock()
_locks = {}
_held = threading.local()


@contextmanager
def runtime_write_lock(state_dir, *, timeout=30):
    root = Path(state_dir).resolve() / "agent-manage"
    with _guard:
        lock = _locks.setdefault(str(root), threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise APIError("BUSY", "Runtime lock is busy; retry later", retryable=True)
    fd = None
    depths = getattr(_held, "depths", {})
    _held.depths = depths
    try:
        if not depths.get(str(root)):
            if root.is_symlink():
                raise ValueError("Runtime state directory must not be a symlink")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / "runtime.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Runtime lock must be a private regular file")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise APIError("BUSY", "Runtime lock is busy; retry later", retryable=True)
                    time.sleep(.02)
            if (path.stat().st_dev, path.stat().st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("Runtime lock was replaced")
        depths[str(root)] = depths.get(str(root), 0) + 1
        try:
            yield
        finally:
            depths[str(root)] -= 1
    finally:
        if fd is not None:
            os.close(fd)
        lock.release()
