"""Local process leases excluding cold backups from application writes."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .skill_proposal_files import directory_fd

MAINTENANCE_LOCK = ".application-maintenance.lock"


class DataDirectoryBusyError(RuntimeError):
    """Another cooperating process is using the application data directory."""


@contextmanager
def data_directory_lease(root: Path, *, exclusive: bool = False):
    # The application creates its root before entering; the backup requires an
    # existing root. A descriptor prevents following a substituted lock symlink.
    with directory_fd(root) as parent:
        descriptor = os.open(
            MAINTENANCE_LOCK,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("application maintenance lock must be a regular private file")
            try:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DataDirectoryBusyError("application data directory is busy") from exc
            yield
        finally:
            os.close(descriptor)
