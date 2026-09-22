"""SDK-native transcript erasure, isolated from host credentials and other projects."""

import json
import os
import re
import stat
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from pydantic import Field, model_validator

from .backup_files import BackupLimits, tree_inventory
from .models import APIModel
from .skill_proposal_files import directory_fd, relative_parts

UUID_PATTERN = r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
NATIVE_LIMITS = BackupLimits(files=10_000, entries=20_000, file_bytes=250_000_000)


class NativeSessionTarget(APIModel):
    session_id: str = Field(pattern=UUID_PATTERN)
    config: str
    workspace: str
    transcript: str

    @model_validator(mode="after")
    def scoped(self):
        config, workspace, transcript = (
            relative_parts(value) for value in (self.config, self.workspace, self.transcript)
        )
        if (
            len(config) != 2
            or config[0] != "runtime-config"
            or len(workspace) != 3
            or workspace[0] != "projects"
            or workspace[2] != "workspace"
            or len(transcript) != 5
            or transcript[:2] != config
            or transcript[2] != "projects"
            or transcript[-1] != self.session_id + ".jsonl"
        ):
            raise ValueError("invalid native session erasure scope")
        return self


def sdk_operation(root, target, operation):
    root = Path(root).resolve()
    with ExitStack() as stack:
        stack.enter_context(directory_fd(root / target.config))
        try:
            stack.enter_context(directory_fd(root / target.workspace))
        except FileNotFoundError:
            # A removed workspace can still have durable SDK transcripts. Its
            # existing project parent remains validated without following links.
            stack.enter_context(directory_fd((root / target.workspace).parent))
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).with_name("sdk_session_delete_worker.py")),
                operation,
                target.session_id,
                str(root / target.workspace),
            ],
            env={"CLAUDE_CONFIG_DIR": str(root / target.config), "PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    if result.returncode or len(result.stdout) > (50_000 if operation == "list" else 100):
        raise ValueError("Native session operation could not be verified.")
    value = json.loads(result.stdout)
    if operation == "list":
        if (not isinstance(value, dict) or set(value) != {"sessions"}
                or not isinstance(value["sessions"], list) or len(value["sessions"]) > 1000
                or any(not isinstance(item, str) or not re.fullmatch(UUID_PATTERN, item)
                       for item in value["sessions"])
                or len(set(value["sessions"])) != len(value["sessions"])):
            raise ValueError("Native project session inventory is invalid.")
        return value["sessions"]
    if not isinstance(value, dict) or set(value) != {"found"} or type(value["found"]) is not bool:
        raise ValueError("Native session response could not be verified.")
    return value["found"]


def find_native_session(inventory, profile_id, project_id, session_id):
    """Discover an exact UUID only inside its recorded Provider configuration.

    The public SDK verifies the working directory; duplicate UUID copies are
    ambiguous and refused rather than relying on filesystem enumeration order.
    """
    NativeSessionTarget(
        session_id=session_id,
        config=f"runtime-config/{profile_id}",
        workspace=f"projects/{project_id}/workspace",
        transcript=f"runtime-config/{profile_id}/projects/placeholder/{session_id}.jsonl",
    )
    config = f"runtime-config/{profile_id}"
    candidates = []
    for name, info in inventory.children(f"{config}/projects"):
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Native session storage could not be verified.")
        relative = f"{config}/projects/{name}/{session_id}.jsonl"
        raw = inventory.read(relative, optional=True)
        if raw is not None:
            candidates.append(relative)
        elif any(
            child == session_id or child.startswith(session_id + ".")
            for child, _ in inventory.children(f"{config}/projects/{name}")
        ):
            raise ValueError("Native session has unattributed orphan data. Recover it first.")
    if len(candidates) > 1:
        raise ValueError("Native session has ambiguous storage copies.")
    if not candidates:
        return None
    target = NativeSessionTarget(
        session_id=session_id,
        config=config,
        workspace=f"projects/{project_id}/workspace",
        transcript=candidates[0],
    )
    if not sdk_operation(inventory.root, target, "inspect"):
        raise ValueError("Recorded native session could not be located by the SDK.")
    return target


def select_native_side_data(selection, profile_id, session_id):
    """Select documented exact-session side stores without touching shared auth."""
    from .backup_files import parse_json
    from .memory_erasure import jsonl

    config = f"runtime-config/{profile_id}"
    for directory in ("file-history", "image-cache", "uploads", "session-env", "tasks", "debug"):
        for suffix in ("", ".json", ".jsonl", ".txt", ".log"):
            selection.remove_entry(f"{config}/{directory}", session_id + suffix)
    for name, _ in selection.children(f"{config}/todos"):
        if re.fullmatch(re.escape(session_id) + r"-agent-[A-Za-z0-9-]+\.json", name):
            selection.change(f"{config}/todos/{name}")
    for folder, info in selection.children(f"{config}/projects"):
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Native project storage is invalid.")
        for name, _ in selection.children(f"{config}/projects/{folder}"):
            if re.fullmatch(
                re.escape(session_id)
                + r"(?:\.orphaned-[A-Za-z0-9_-]+\.jsonl|\.jsonl\.superseded-[0-9-]+)",
                name,
            ):
                selection.change(f"{config}/projects/{folder}/{name}")
    path = f"{config}/history.jsonl"
    raw = selection.read(path, optional=True, record=True)
    if path in selection.changes:
        raw = selection.changes[path][1]
    if raw:
        if not raw.endswith(b"\n"):
            raise ValueError("Native history contains an incomplete tail.")
        rows = [parse_json(line) for line in raw.splitlines() if line.strip()]
        retained = jsonl(
            [
                row
                for row in rows
                if row.get("sessionId") != session_id and row.get("session_id") != session_id
            ]
        )
        if path in selection.changes:
            selection.changes[path] = (selection.changes[path][0], retained)
        else:
            selection.change(path, retained)


def erase_native_sessions(root, targets, files, directories):
    """Recheck reviewed bytes, invoke the SDK, and let the journal finish leftovers.

    The pinned SDK intentionally suppresses errors while removing child transcripts.
    Exact remaining child files stay in the file transaction for verified cleanup.
    """
    from .erasure_files import _matches, _read

    for target in targets:
        child = target.transcript.removesuffix(".jsonl")
        expected_files = {
            item.path: item
            for item in files
            if item.path == target.transcript or item.path.startswith(child + "/")
        }
        expected_dirs = {
            path for path in directories if path == child or path.startswith(child + "/")
        }
        if target.transcript not in expected_files:
            raise ValueError("Native erasure has no reviewed transcript.")
        if any(item.after_sha256 is not None for item in expected_files.values()):
            raise ValueError("Native erasure cannot replace session data.")
        try:
            tree = tree_inventory(Path(root) / child, NATIVE_LIMITS)
            if child not in expected_dirs:
                raise ValueError("Native child root was not reviewed.")
        except FileNotFoundError:
            tree = {"files": [], "directories": []}
        for item in tree["files"]:
            path = f"{child}/{item['path']}"
            before = expected_files.get(path)
            if (
                before is None
                or item["sha256"] != before.before_sha256
                or item["size"] != before.before_size
            ):
                raise ValueError("Native child transcript changed after deletion commit.")
        if any(f"{child}/{item['path']}" not in expected_dirs for item in tree["directories"]):
            raise ValueError("Native child directory changed after deletion commit.")
        original = expected_files[target.transcript]
        raw = _read(root, target.transcript, 250_000_000, missing=True)
        if raw is None:
            continue  # A crash may have removed the main transcript before its children.
        if not _matches(raw, original.before_size, original.before_sha256):
            raise ValueError("Native transcript changed after deletion commit.")
        if not sdk_operation(root, target, "delete"):
            raise ValueError("Native session deletion could not be verified.")
        if _read(root, target.transcript, 250_000_000, missing=True) is not None:
            raise ValueError("Native session transcript remains after SDK deletion.")
        with directory_fd((Path(root) / target.transcript).parent) as parent:
            os.fsync(parent)
