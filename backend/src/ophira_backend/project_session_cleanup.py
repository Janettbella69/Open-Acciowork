"""Review and erase application-owned native SDK sessions, without model calls.

The caller holds the project's exclusive lifecycle lease and has stopped every
worker before erasure. Plans contain hashes and locators, never transcript text.
The pinned SDK owns transcript deletion; bounded nofollow capture covers its
documented omissions and verifies the result of its best-effort directory removal.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .backup_files import parse_json
from .models import RunRecord
from .skill_proposal_files import child_directory, directory_fd, read_at, relative_parts

_MAX_ENTRIES = 50_000
_MAX_BYTES = 512 * 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024
_SID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TRANSCRIPT = re.compile(rf"^({_SID})(?:\.jsonl|\.orphaned-[^/]+\.jsonl|"
                         r"\.jsonl\.superseded-[^/]+)$")
_SESSION_TREES = ("file-history", "tasks", "session-env", "image-cache", "uploads")
_UNATTRIBUTED = (
    "history.jsonl", "plans", "paste-cache", "shell-snapshots", "feedback",
    "dump-prompts", "apiDump", "telemetry",
)


class SessionCleanupConflict(ValueError):
    """The reviewed native-session boundary cannot safely be erased."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionCleanupFile(_StrictModel):
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    kind: Literal["file", "symlink"] = "file"


class SessionCleanupGroup(_StrictModel):
    config_root: str
    session_id: str
    run_ids: list[str] = Field(default_factory=list)


class SessionCleanupPlan(_StrictModel):
    format: Literal["ophira-native-session-cleanup-v1"] = "ophira-native-session-cleanup-v1"
    project_id: str
    data_root_hash: str
    relative_files: list[SessionCleanupFile] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)
    scan_roots: list[str] = Field(default_factory=list)
    session_groups: list[SessionCleanupGroup] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    retained: list[str] = Field(default_factory=lambda: [
        "Other projects' native sessions and shared Provider configuration are retained.",
        "Operator backups, downloaded copies and external session stores are outside this erasure.",
    ])


class SessionCleanupResult(_StrictModel):
    complete: bool
    erased_files: int
    erased_sessions: int


def _component(value: str) -> str:
    if len(relative_parts(value)) != 1 or value.startswith("."):
        raise SessionCleanupConflict("invalid_native_scope")
    return value


def _session_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise SessionCleanupConflict("invalid_native_session_id") from exc
    return value


class _Capture:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, SessionCleanupFile] = {}
        self.directories: set[str] = set()
        self.entries = 0
        self.bytes = 0
        self.checkpoint_links: dict[tuple[int, int], tuple[int, set[str]]] = {}

    def names(self, relative: str) -> list[str]:
        with directory_fd(self.root) as root_fd:
            try:
                with child_directory(root_fd, relative_parts(relative)) as fd:
                    names = []
                    with os.scandir(fd) as children:
                        for child in children:
                            self.entries += 1
                            if self.entries > _MAX_ENTRIES:
                                raise SessionCleanupConflict("native_entry_limit")
                            relative_parts(child.name)
                            names.append(child.name)
                    if len({name.casefold() for name in names}) != len(names):
                        raise SessionCleanupConflict("ambiguous_native_paths")
                    return sorted(names)
            except FileNotFoundError:
                return []

    def info(self, relative: str):
        with directory_fd(self.root) as root_fd:
            try:
                with child_directory(root_fd, relative_parts(relative)[:-1]) as fd:
                    return os.stat(relative_parts(relative)[-1], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return None

    def read(self, relative: str) -> bytes:
        with directory_fd(self.root) as root_fd:
            parts = relative_parts(relative)
            with child_directory(root_fd, parts[:-1]) as fd:
                raw = read_at(fd, parts[-1], min(_MAX_FILE_BYTES, _MAX_BYTES - self.bytes))
        self.bytes += len(raw)
        return raw

    def checkpoint(self, relative: str) -> bytes:
        """Native session forks hardlink backups; allow only fully accounted links."""
        parts = relative_parts(relative)
        with directory_fd(self.root) as root_fd, child_directory(root_fd, parts[:-1]) as fd:
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=fd,
            )
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                limit = min(_MAX_FILE_BYTES, _MAX_BYTES - self.bytes)
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise SessionCleanupConflict("unsafe_native_checkpoint")
                raw = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
                if len(raw) > limit or any(getattr(before, f) != getattr(after, f) for f in fields):
                    raise SessionCleanupConflict("native_checkpoint_changed")
                key = (before.st_dev, before.st_ino)
                count, paths = self.checkpoint_links.setdefault(key, (before.st_nlink, set()))
                if count != before.st_nlink:
                    raise SessionCleanupConflict("native_checkpoint_changed")
                paths.add(relative)
        self.bytes += len(raw)
        return raw

    def verify_links(self) -> None:
        if any(count != len(paths) for count, paths in self.checkpoint_links.values()):
            raise SessionCleanupConflict("native_checkpoint_has_unreviewed_links")

    def tree(self, relative: str, depth: int = 0) -> None:
        if depth > 40:
            raise SessionCleanupConflict("native_depth_limit")
        info = self.info(relative)
        if info is None:
            return
        if stat.S_ISDIR(info.st_mode):
            self.directories.add(relative)
            for name in self.names(relative):
                self.tree(f"{relative}/{name}", depth + 1)
            return
        if stat.S_ISLNK(info.st_mode):
            parts = relative_parts(relative)
            with directory_fd(self.root) as root_fd, child_directory(root_fd, parts[:-1]) as fd:
                raw = os.fsencode(os.readlink(parts[-1], dir_fd=fd))
            kind = "symlink"
        else:
            if relative_parts(relative)[2] == "file-history":
                raw = self.checkpoint(relative)
            else:
                raw = self.read(relative)  # Rejects devices, sockets and hardlinks.
            kind = "file"
        self.files[relative] = SessionCleanupFile(
            relative_path=relative, sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw), kind=kind,
        )


class ProjectSessionCleanup:
    def __init__(self, data_root: Path):
        # Do not resolve away a symlink. Every access verifies all ancestors.
        self.root = Path(data_root).absolute()
        self.root_hash = hashlib.sha256(os.fsencode(self.root)).hexdigest()

    def preview(
        self, project_id: str, runs: Iterable[RunRecord], all_runs: Iterable[RunRecord],
    ) -> SessionCleanupPlan:
        _component(project_id)
        plan = SessionCleanupPlan(project_id=project_id, data_root_hash=self.root_hash)
        try:
            self._preview(plan, list(runs), list(all_runs))
        except (OSError, ValueError, RecursionError) as exc:
            # Arbitrary source paths/JSON/errors may contain secrets. Stable codes only.
            code = str(exc) if isinstance(exc, SessionCleanupConflict) else "unsafe_native_storage"
            plan.blockers.append(code)
        plan.blockers = sorted(set(plan.blockers))
        return plan

    def _preview(self, plan: SessionCleanupPlan, runs: list, all_runs: list) -> None:
        cwd = str(self.root / "projects" / plan.project_id / "workspace")
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        refs: dict[tuple[str, str], set[str]] = defaultdict(set)
        foreign: set[tuple[str, str]] = set()
        for run in all_runs:
            if run.project_id != plan.project_id:
                for sid in (run.sdk_session_id, run.resume_session_id):
                    if sid:
                        foreign.add((run.provider_profile_id or "environment", sid))
        for run in runs:
            if run.project_id != plan.project_id:
                raise SessionCleanupConflict("foreign_project_run")
            provider = _component(run.provider_profile_id or "environment")
            for sid in (run.sdk_session_id, run.resume_session_id):
                if sid:
                    refs[provider, _session_id(sid)].add(run.id)
        if any(key in foreign for key in refs):
            raise SessionCleanupConflict("native_session_shared_with_other_project")
        cap = _Capture(self.root)
        providers = sorted(set(cap.names("runtime-config")) | {key[0] for key in refs})
        roots: set[str] = set()
        groups = dict(refs)
        identity_hashes: dict[str, str] = {}
        for provider in providers:
            _component(provider)
            config = f"runtime-config/{provider}"
            info = cap.info(config)
            if info is None:
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise SessionCleanupConflict("unsafe_native_config_root")
            found: dict[str, list[str]] = defaultdict(list)
            owned_dirs = []
            for dirname in cap.names(f"{config}/projects"):
                directory = f"{config}/projects/{dirname}"
                info = cap.info(directory)
                if info is None or not stat.S_ISDIR(info.st_mode):
                    raise SessionCleanupConflict("unsafe_native_project_directory")
                names = cap.names(directory)
                candidate = dirname == encoded or (
                    len(encoded) > 200 and dirname.startswith(encoded[:200] + "-")
                )
                relevant = candidate or any(
                    (match := _TRANSCRIPT.fullmatch(name)) and (provider, match[1]) in refs
                    for name in names
                )
                for name in names:
                    if (match := re.fullmatch(rf"({_SID})\.jsonl", name)):
                        found[match[1]].append(f"{directory}/{name}")
                if not relevant:
                    continue
                identities: set[str] = set()
                for name in names:
                    match = _TRANSCRIPT.fullmatch(name)
                    if not match:
                        continue
                    sid = match[1]
                    info = cap.info(f"{directory}/{name}")
                    if info is not None and stat.S_ISLNK(info.st_mode):
                        if not candidate or (provider, sid) not in refs:
                            raise SessionCleanupConflict("unproven_native_session_owner")
                        # The run proves ownership of this directory entry, not
                        # of its link target. Capture/unlink the leaf only.
                        identities.add(sid)
                        groups.setdefault((provider, sid), set())
                        continue
                    raw = cap.read(f"{directory}/{name}")
                    identity_hashes[f"{directory}/{name}"] = hashlib.sha256(raw).hexdigest()
                    observed_cwds, observed_sessions = set(), set()
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        row = parse_json(line)
                        if not isinstance(row, dict):
                            raise SessionCleanupConflict("invalid_native_transcript")
                        if row.get("cwd") is not None:
                            if not isinstance(row["cwd"], str):
                                raise SessionCleanupConflict("invalid_native_transcript")
                            observed_cwds.add(row["cwd"])
                        if row.get("sessionId") is not None:
                            if not isinstance(row["sessionId"], str):
                                raise SessionCleanupConflict("invalid_native_transcript")
                            observed_sessions.add(row["sessionId"])
                    if observed_cwds - {cwd} or observed_sessions - {sid}:
                        raise SessionCleanupConflict("native_session_ownership_mismatch")
                    if not observed_cwds and (provider, sid) not in refs:
                        raise SessionCleanupConflict("unproven_native_session_owner")
                    if (provider, sid) in foreign:
                        raise SessionCleanupConflict("native_session_shared_with_other_project")
                    identities.add(sid)
                    groups.setdefault((provider, sid), set())
                if not identities and names:
                    raise SessionCleanupConflict("unproven_native_directory_owner")
                if identities:
                    for name in names:
                        if not (_TRANSCRIPT.fullmatch(name) or name in identities
                                or name in {"memory", "sessions-index.json"}):
                            raise SessionCleanupConflict("unknown_native_project_content")
                    if not candidate:
                        raise SessionCleanupConflict("native_cwd_migration_requires_review")
                    owned_dirs.append(directory)
                    roots.add(directory)
            for (group_provider, sid), _run_ids in list(groups.items()):
                if group_provider != provider:
                    continue
                if len(found[sid]) > 1 or any(
                    str(Path(path).parent) not in owned_dirs for path in found[sid]
                ):
                    raise SessionCleanupConflict("ambiguous_native_session_location")
                for name in _SESSION_TREES:
                    roots.add(f"{config}/{name}/{sid}")
                roots.add(f"{config}/debug/{sid}.txt")
                # Legacy native todo files are session-prefixed, never provider-wide.
                for name in cap.names(f"{config}/todos"):
                    if name.startswith(sid + "-agent-") and name.endswith(".json"):
                        roots.add(f"{config}/todos/{name}")
            if any(key[0] == provider for key in groups) or owned_dirs:
                known_sessions = set(found) | {
                    sid for key_provider, sid in (*groups, *foreign) if key_provider == provider
                }
                for name in _SESSION_TREES:
                    for sid in cap.names(f"{config}/{name}"):
                        if sid not in known_sessions:
                            plan.blockers.append("unproven_native_sidecar_owner:" +
                                                 f"{config}/{name}")
                for name in _UNATTRIBUTED:
                    path = f"{config}/{name}"
                    info = cap.info(path)
                    if info is not None and (
                        not stat.S_ISDIR(info.st_mode) or cap.names(path)
                    ):
                        plan.blockers.append("unattributed_native_content:" + path)
        for relative in sorted(roots):
            cap.tree(relative)
        cap.verify_links()
        for relative, digest in identity_hashes.items():
            item = cap.files.get(relative)
            if item is None or item.kind != "file" or item.sha256 != digest:
                raise SessionCleanupConflict("native_cleanup_snapshot_changed")
        plan.scan_roots = sorted(roots)
        plan.relative_files = sorted(cap.files.values(), key=lambda item: item.relative_path)
        plan.directories = sorted(cap.directories)
        plan.session_groups = [SessionCleanupGroup(
            config_root=f"runtime-config/{provider}", session_id=sid, run_ids=sorted(run_ids),
        ) for (provider, sid), run_ids in sorted(groups.items())]

    def erase(self, plan: SessionCleanupPlan) -> SessionCleanupResult:
        """Idempotent under the caller's durable tombstone and exclusive lease."""
        if plan.blockers or plan.data_root_hash != self.root_hash:
            raise SessionCleanupConflict("native_cleanup_plan_blocked")
        _component(plan.project_id)
        allowed_configs = set()
        for group in plan.session_groups:
            parts = relative_parts(group.config_root)
            if len(parts) != 2 or parts[0] != "runtime-config":
                raise SessionCleanupConflict("invalid_native_scope")
            _component(parts[1])
            _session_id(group.session_id)
            allowed_configs.add(group.config_root)
        for relative in [*plan.scan_roots, *plan.directories,
                         *(item.relative_path for item in plan.relative_files)]:
            parts = relative_parts(relative)
            if len(parts) < 4 or "/".join(parts[:2]) not in allowed_configs:
                raise SessionCleanupConflict("invalid_native_scope")
            config = "/".join(parts[:2])
            ids = {g.session_id for g in plan.session_groups if g.config_root == config}
            if parts[2] == "projects":
                # Only a reviewed cwd-specific native project directory can be a root.
                encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(
                    self.root / "projects" / plan.project_id / "workspace",
                ))
                if not (parts[3] == encoded or (
                    len(encoded) > 200 and parts[3].startswith(encoded[:200] + "-")
                )):
                    raise SessionCleanupConflict("native_cwd_migration_requires_review")
            elif parts[2] in _SESSION_TREES:
                if parts[3] not in ids:
                    raise SessionCleanupConflict("invalid_native_scope")
            elif parts[2] == "debug":
                if len(parts) != 4 or parts[3] not in {sid + ".txt" for sid in ids}:
                    raise SessionCleanupConflict("invalid_native_scope")
            elif parts[2] == "todos":
                if len(parts) != 4 or not any(
                    parts[3].startswith(sid + "-agent-") and parts[3].endswith(".json")
                    for sid in ids
                ):
                    raise SessionCleanupConflict("invalid_native_scope")
            else:
                raise SessionCleanupConflict("invalid_native_scope")
        self._verify(plan)
        # These exact directories belong to the reviewed project/session. Native
        # snapshots may be read-only; do not chmod shared config or parent roots.
        for relative in plan.directories:
            with directory_fd(self.root) as root_fd:
                try:
                    with child_directory(root_fd, relative_parts(relative)) as fd:
                        mode = stat.S_IMODE(os.fstat(fd).st_mode)
                        if mode & 0o700 != 0o700:
                            os.fchmod(fd, mode | 0o700)
                            os.fsync(fd)
                except FileNotFoundError:
                    pass
        # Remove leaf symlinks ourselves: the SDK's lookup follows main-file links.
        for item in plan.relative_files:
            if item.kind == "symlink":
                self._unlink(item.relative_path)
        for group in plan.session_groups:
            self._sdk_delete(group)
        # The SDK intentionally ignores recursive deletion errors. Finish the exact
        # reviewed inventory, including checkpoints, then verify no residue/additions.
        for item in plan.relative_files:
            self._unlink(item.relative_path)
        for relative in sorted(plan.directories, key=lambda path: (-path.count("/"), path)):
            self._unlink(relative, directory=True)
        cap = _Capture(self.root)
        for relative in plan.scan_roots:
            cap.tree(relative)
        if cap.files or cap.directories:
            raise SessionCleanupConflict("native_cleanup_residue")
        return SessionCleanupResult(
            complete=True, erased_files=len(plan.relative_files),
            erased_sessions=len(plan.session_groups),
        )

    def _verify(self, plan: SessionCleanupPlan) -> None:
        cap = _Capture(self.root)
        for relative in plan.scan_roots:
            cap.tree(relative)
        cap.verify_links()
        expected = {item.relative_path: item for item in plan.relative_files}
        if len(expected) != len(plan.relative_files):
            raise SessionCleanupConflict("ambiguous_native_paths")
        if any(expected.get(path) != item for path, item in cap.files.items()):
            raise SessionCleanupConflict("native_cleanup_snapshot_changed")
        if cap.directories - set(plan.directories):
            raise SessionCleanupConflict("native_cleanup_snapshot_changed")
        # SDK delete_session searches all native project directories. Reconfirm
        # uniqueness even on retries after the reviewed main file has disappeared.
        for group in plan.session_groups:
            for name in _UNATTRIBUTED:
                path = f"{group.config_root}/{name}"
                info = cap.info(path)
                if info is not None and (not stat.S_ISDIR(info.st_mode) or cap.names(path)):
                    raise SessionCleanupConflict("unattributed_native_content")
            projects = f"{group.config_root}/projects"
            for dirname in cap.names(projects):
                directory = f"{projects}/{dirname}"
                info = cap.info(directory)
                if info is None or not stat.S_ISDIR(info.st_mode):
                    raise SessionCleanupConflict("unsafe_native_project_directory")
                path = f"{directory}/{group.session_id}.jsonl"
                if cap.info(path) is not None and path not in expected:
                    raise SessionCleanupConflict("ambiguous_native_session_location")

    def _unlink(self, relative: str, *, directory: bool = False) -> None:
        parts = relative_parts(relative)
        with directory_fd(self.root) as root_fd:
            try:
                with child_directory(root_fd, parts[:-1]) as fd:
                    if directory:
                        os.rmdir(parts[-1], dir_fd=fd)
                    else:
                        os.unlink(parts[-1], dir_fd=fd)
                    os.fsync(fd)
            except FileNotFoundError:
                pass

    def _sdk_delete(self, group: SessionCleanupGroup) -> None:
        config = self.root / group.config_root
        if _Capture(self.root).info(group.config_root) is None:
            return
        # No shared os.environ mutation, provider credentials, inherited PYTHONPATH,
        # home configuration, git discovery or native model runtime invocation.
        with tempfile.TemporaryDirectory(prefix="ophira-session-cleanup-") as temporary:
            env = {
                "HOME": temporary, "TMPDIR": temporary, "PATH": "/usr/bin:/bin",
                "CLAUDE_CONFIG_DIR": str(config),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
            script = (
                "from importlib.metadata import version\n"
                "from claude_agent_sdk import delete_session\n"
                "import sys\n"
                "assert version('claude-agent-sdk') == '0.2.143'\n"
                "try:\n delete_session(sys.argv[1])\n"
                "except FileNotFoundError:\n pass\n"
            )
            try:
                result = subprocess.run(
                    [sys.executable, "-I", "-c", script, group.session_id],
                    env=env, cwd=temporary, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SessionCleanupConflict("native_sdk_cleanup_failed") from exc
            if result.returncode:
                raise SessionCleanupConflict("native_sdk_cleanup_failed")
