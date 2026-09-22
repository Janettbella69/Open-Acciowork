"""Project-owned native SDK sessions, including sessions not yet indexed as Runs."""

import re
import stat

from .backup_files import parse_json
from .erasure_files import encode
from .memory_erasure import jsonl
from .sdk_session_erasure import (
    UUID_PATTERN,
    NativeSessionTarget,
    find_native_session,
    sdk_operation,
    select_native_side_data,
)


def select_project_native(selection, project_id, runs, foreign_sessions):
    workspace = f"projects/{project_id}/workspace"
    absolute = str(selection.root / workspace)
    known = {
        value for run in runs for value in (run.sdk_session_id, run.resume_session_id) if value
    }
    for profile, info in selection.children("runtime-config"):
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Native configuration storage is invalid.")
        config = f"runtime-config/{profile}"
        # Bound discovery and reject linked configuration directories before the
        # public SDK lists sessions. It never traverses other git worktrees.
        for folder, info in selection.children(f"{config}/projects"):
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Native project storage is invalid.")
            selection.children(f"{config}/projects/{folder}")
        probe = NativeSessionTarget(
            session_id="00000000-0000-0000-0000-000000000000",
            config=config,
            workspace=workspace,
            transcript=f"{config}/projects/placeholder/00000000-0000-0000-0000-000000000000.jsonl",
        )
        discovered = sdk_operation(selection.root, probe, "list")
        if set(discovered) & foreign_sessions:
            raise ValueError("Native project session belongs to another project.")
        sessions = known | set(discovered)
        folders = set()
        targets = []
        for session in sorted(sessions):
            target = find_native_session(selection, profile, project_id, session)
            if target is not None:
                targets.append(target)
                folders.add(target.transcript.rsplit("/", 1)[0])
            select_native_side_data(selection, profile, session)
        selected_main = {target.transcript for target in targets}
        for folder in folders:
            for name, _ in selection.children(folder):
                if (
                    name.endswith(".jsonl")
                    and re.fullmatch(UUID_PATTERN, name[:-6])
                    and f"{folder}/{name}" not in selected_main
                ):
                    raise ValueError("Native project contains an unverifiable transcript.")
            selection.remove_tree(folder)
            selection.removed_directories.add(folder)
        selection.native_sessions.extend(targets)
        path = f"{config}/history.jsonl"
        raw = selection.read(path, optional=True, record=True)
        if path in selection.changes:
            raw = selection.changes[path][1]
        if raw:
            if not raw.endswith(b"\n"):
                raise ValueError("Native history has an incomplete tail.")
            rows = [parse_json(line) for line in raw.splitlines() if line.strip()]
            retained = jsonl(
                [
                    row
                    for row in rows
                    if row.get("project") != absolute and row.get("cwd") != absolute
                ]
            )
            if path in selection.changes:
                selection.changes[path] = (selection.changes[path][0], retained)
            else:
                selection.change(path, retained)
        path = f"{config}/.claude.json"
        raw = selection.read(path, optional=True, record=True)
        if raw:
            value = parse_json(raw)
            projects = value.get("projects", {})
            if not isinstance(projects, dict):
                raise ValueError("Native project configuration is invalid.")
            if absolute in projects:
                del projects[absolute]
                selection.change(path, encode(value))
