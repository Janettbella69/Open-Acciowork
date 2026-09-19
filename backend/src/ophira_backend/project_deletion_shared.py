"""Stable cross-process locks for shared files touched by project erasure."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_GUARD = threading.Lock()
_HELD = threading.local()


@contextmanager
def shared_file_lease(root: Path, relative: str):
    """Writers and erasure use the same permanent inode; never await while held."""
    identity = (str(root.absolute()), relative)
    with _GUARD:
        lock = _LOCKS.setdefault(identity, threading.RLock())
    with lock:
        held = getattr(_HELD, "keys", set())
        if identity in held:
            yield
            return
        directory = root / ".project-shared-locks"
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("shared deletion lock directory is linked")
        path = directory / (hashlib.sha256(relative.encode()).hexdigest() + ".lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("shared deletion lock is not private")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _HELD.keys = held | {identity}
            yield
        finally:
            _HELD.keys = held
            os.close(descriptor)
