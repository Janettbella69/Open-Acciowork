"""Project operation leases and durable deletion intents shared by API workers.

Leases never wait on another worker. A deletion must acquire every relevant
exclusive lease before committing its intent; ordinary work cannot enter after
that point. Lock inodes live outside the content trees and are never removed.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .skill_proposal_files import child_directory, directory_fd, read_at

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_HELD: ContextVar[dict[tuple[str, str], tuple[tuple[int, int | None], bool]] | None] = ContextVar(
    "project_lifecycle_leases", default=None,
)
MAX_DELETION_STATE_BYTES = 64 * 1024 * 1024


class ProjectUnavailableError(KeyError):
    """The project has an irreversible deletion intent."""


class ProjectLifecycleBusyError(RuntimeError):
    """A cooperating operation still owns this project."""


def _owner() -> tuple[int, int | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task) if task is not None else None


def _safe_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not _IDENTIFIER.fullmatch(project_id):
        raise ValueError("invalid project lifecycle identifier")
    return project_id


class ProjectLifecycle:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @contextmanager
    def _directory(self, name: str, *, create: bool = False):
        with directory_fd(self.root) as root_fd:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=root_fd)
                    os.fsync(root_fd)
                except FileExistsError:
                    pass
            with child_directory(root_fd, (name,)) as descriptor:
                yield descriptor

    def _held_exclusive(self, project_id: str) -> bool:
        return (_HELD.get() or {}).get((str(self.root), project_id)) == (_owner(), True)

    def has_deletion(self, project_id: str) -> bool:
        project_id = _safe_id(project_id)
        try:
            with self._directory("project-deletions") as parent:
                with child_directory(parent, (project_id,)) as directory:
                    try:
                        os.stat("state.json", dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        return False
                    # A malformed/linked intent remains unavailable until repaired.
                    return True
        except FileNotFoundError:
            return False

    def assert_available(self, project_id: str) -> None:
        project_id = _safe_id(project_id)
        if self._held_exclusive(project_id):
            return  # Internal capture/cleanup in the exclusive operation only.
        if self.has_deletion(project_id):
            raise ProjectUnavailableError(project_id)
        from .conversation_deletion_state import ConversationDeletionStates

        states = ConversationDeletionStates(self).list(project_id)
        if any(state["status"] != "deleted" for state in states):
            raise ProjectUnavailableError(project_id)

    def assert_run_available(self, project_id: str, run_id: str | None = None, *,
                             session_id: str | None = None, provider_id: str | None = None) -> None:
        from .conversation_deletion_state import ConversationDeletionStates

        for state in ConversationDeletionStates(self).list(project_id):
            if run_id is not None and run_id in state["run_ids"]:
                raise ProjectUnavailableError(run_id)
            if session_id is not None and any(
                group["session_id"] == session_id
                and group["config_root"] == f"runtime-config/{provider_id or 'environment'}"
                for group in state["plan"]["sessions"]["session_groups"]
            ):
                raise ProjectUnavailableError(run_id or project_id)

    @contextmanager
    def lease(
        self, project_id: str, *, exclusive: bool = False, allow_deleted: bool = False,
    ) -> Iterator[None]:
        project_id = _safe_id(project_id)
        identity = (str(self.root), project_id)
        owner = _owner()
        held = _HELD.get() or {}
        existing = held.get(identity)
        if existing is not None and existing[0] == owner:
            if exclusive and not existing[1]:
                raise ProjectLifecycleBusyError("project lease cannot be upgraded")
            if not allow_deleted:
                self.assert_available(project_id)
            yield
            return
        with self._directory(".project-lifecycle-locks", create=True) as directory:
            descriptor = os.open(
                project_id + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=directory,
            )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("project lifecycle lock must be a private regular file")
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                            | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ProjectLifecycleBusyError("project has active operations") from exc
            # Check before registering the exclusive owner: a fresh exclusive
            # capture is not allowed to bypass an existing deletion intent.
            if not allow_deleted:
                self.assert_available(project_id)
            token = _HELD.set({**held, identity: (owner, exclusive)})
            try:
                yield
            finally:
                _HELD.reset(token)
        finally:
            os.close(descriptor)

    @contextmanager
    def exclusive_many(
        self, project_ids: list[str], *, allow_deleted: bool = False,
    ) -> Iterator[None]:
        with ExitStack() as stack:
            for project_id in sorted(set(project_ids)):
                stack.enter_context(self.lease(
                    project_id, exclusive=True, allow_deleted=allow_deleted,
                ))
            yield

    def read_state(self, project_id: str) -> dict[str, Any] | None:
        project_id = _safe_id(project_id)
        try:
            with self._directory("project-deletions") as parent:
                with child_directory(parent, (project_id,)) as directory:
                    raw = read_at(directory, "state.json", MAX_DELETION_STATE_BYTES)
        except FileNotFoundError:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("project_id") != project_id
                or value.get("status") not in {"deleting", "blocked", "deleted"}):
            raise ValueError("invalid project deletion intent")
        return value

    def write_state(self, project_id: str, state: dict[str, Any]) -> None:
        project_id = _safe_id(project_id)
        if not self._held_exclusive(project_id):
            raise RuntimeError("deletion intent requires an exclusive project lease")
        if (state.get("project_id") != project_id or state.get("schema_version") != 1
                or state.get("status") not in {"deleting", "blocked", "deleted"}):
            raise ValueError("invalid project deletion intent")
        previous = self.read_state(project_id)
        if previous is not None:
            if previous.get("operation_id") != state.get("operation_id"):
                raise ValueError("project deletion operation cannot be replaced")
            if previous["status"] == "deleted" and state != previous:
                raise ValueError("completed project deletion is immutable")
        payload = (json.dumps(state, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False) + "\n").encode()
        if len(payload) > MAX_DELETION_STATE_BYTES:
            raise ValueError("project deletion intent exceeds its size limit")
        with self._directory("project-deletions", create=True) as parent:
            try:
                os.mkdir(project_id, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            with child_directory(parent, (project_id,)) as directory:
                temporary = ".state-" + uuid.uuid4().hex + ".tmp"
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, "state.json", src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass

    def pending_ids(self) -> list[str]:
        try:
            with self._directory("project-deletions") as directory:
                names = os.listdir(directory)
        except FileNotFoundError:
            return []
        if len(names) > 10_000:
            raise ValueError("too many project deletion operations")
        result = []
        for project_id in sorted(names):
            state = self.read_state(_safe_id(project_id))
            if state is not None and state["status"] != "deleted":
                result.append(project_id)
        return result
