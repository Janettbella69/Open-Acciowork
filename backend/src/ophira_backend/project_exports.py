"""Authorized, bounded business-data archives; never a copy of SDK credentials.

The export inventory is derived from authoritative stores on every request. It
is not a second project ledger or an application-restore format.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass

from .artifact_files import snapshot_relative
from .auth import ProjectPermission
from .backup_files import parse_json
from .constants import TERMINAL_RUN_STATUSES, EventType, MemoryScope
from .models import EventEnvelope, RunRecord, utc_now
from .project_scope import authorize_project_scope, project_actor
from .skill_proposal_files import directory_fd, read_relative, relative_parts
from .storage import LedgerNotFoundError
from .workspace_checkpoints import workspace_lease

MAX_FILES = 20_000
MAX_ENTRIES = 50_000
MAX_FILE_BYTES = 250_000_000
MAX_TOTAL_BYTES = 512_000_000
MAX_SCAN_BYTES = 128_000_000
MAX_RECORD_BYTES = 4_000_000
MAX_RUNS = 1000
MAX_EVENTS = 200_000

EXCLUSIONS = [
    "provider_and_connector_credentials",
    "sdk_internal_transcripts_and_runtime_configuration",
    "browser_login_profiles_and_device_credentials",
    "shared_agent_core_and_user_or_agent_memories",
    "other_projects_and_external_service_data",
]


class ProjectExportError(ValueError):
    """Stable messages, without user content or private paths."""


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


@dataclass
class ExportFile:
    path: str
    source: str | None
    size: int
    sha256: str
    content: bytes | None = None

    def view(self):
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


class ExportInventory:
    def __init__(self, root, authorize):
        self.root, self.authorize = root, authorize
        self.files: dict[str, ExportFile] = {}
        self.paths: dict[str, tuple[str, bool]] = {}
        self.directories: set[str] = set()
        self.total_bytes = 0
        self.scan_bytes = 0
        self.entries = 0
        self.events = 0
        self.warnings: list[dict] = []

    def read(self, relative, *, optional=False, record=False):
        self.authorize()
        try:
            raw = read_relative(
                self.root,
                relative,
                min(
                    MAX_RECORD_BYTES if record else MAX_FILE_BYTES,
                    MAX_SCAN_BYTES - self.scan_bytes if record else MAX_TOTAL_BYTES,
                ),
            )
        except FileNotFoundError:
            if optional:
                return None
            raise
        if record:
            self.scan_bytes += len(raw)
            if self.scan_bytes > MAX_SCAN_BYTES:
                raise ProjectExportError("Export discovery exceeds its size limit.")
        return raw

    def children(self, relative):
        relative_parts(relative)
        self.authorize()
        try:
            with directory_fd(self.root / relative) as fd:
                rows = []
                with os.scandir(fd) as entries:
                    for item in entries:
                        self.entries += 1
                        if self.entries > MAX_ENTRIES:
                            raise ProjectExportError("Export discovery exceeds its entry limit.")
                        relative_parts(f"{relative}/{item.name}")
                        rows.append((item.name, item.stat(follow_symlinks=False)))
                return sorted(rows)
        except FileNotFoundError:
            return []

    def register_path(self, path, *, directory=False):
        parts = relative_parts(path)
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            is_directory = index < len(parts) or directory
            key = unicodedata.normalize("NFC", prefix).casefold()
            entry = (prefix, is_directory)
            if key in self.paths and self.paths[key] != entry:
                raise ProjectExportError("Export contains ambiguous paths.")
            self.paths[key] = entry

    def add(self, path, raw, *, source=None):
        relative_parts(path)
        if path in self.files:
            raise ProjectExportError("Export contains duplicate paths.")
        if len(self.files) >= MAX_FILES or self.total_bytes + len(raw) > MAX_TOTAL_BYTES:
            raise ProjectExportError("Export exceeds its file or size limit.")
        # Check every parent too: a file and a case-folded directory cannot coexist.
        self.register_path(path)
        self.total_bytes += len(raw)
        self.files[path] = ExportFile(
            path,
            source,
            len(raw),
            hashlib.sha256(raw).hexdigest(),
            None if source else raw,
        )

    def file(self, relative, *, optional=False):
        raw = self.read(relative, optional=optional)
        if raw is not None:
            self.add(f"data/{relative}", raw, source=relative)
        return raw

    def tree(self, relative, depth=0):
        if depth > 32:
            raise ProjectExportError("Export directory depth exceeds its limit.")
        for name, info in self.children(relative):
            path = f"{relative}/{name}"
            if stat.S_ISDIR(info.st_mode):
                self.register_path(f"data/{path}", directory=True)
                self.directories.add(f"data/{path}/")
                self.tree(path, depth + 1)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                self.file(path)
            else:
                raise ProjectExportError("Export cannot include linked or special files.")

    def record(self, relative):
        raw = self.read(relative, record=True)
        value = parse_json(raw)
        if not isinstance(value, dict):
            raise ProjectExportError("Export record could not be verified.")
        return value, raw

    def rows(self):
        return [self.files[name].view() for name in sorted(self.files)]


class ProjectExports:
    def __init__(self, ledger, access, settings, runs, memory):
        self.ledger, self.access, self.settings = ledger, access, settings
        self.runs, self.memory = runs, memory

    def authorize(self, project_id, principal):
        project = authorize_project_scope(
            self.ledger,
            self.access,
            self.settings,
            project_id,
            principal,
            ProjectPermission.PROJECT_ACCESS_MANAGE,
        )
        if project.id != project_id:
            raise ProjectExportError("Project identity could not be verified.")
        return project

    def _runs(self, inventory, project_id, seed):
        runs = []
        for name, info in inventory.children("runs"):
            if not stat.S_ISDIR(info.st_mode):
                raise ProjectExportError("Run storage could not be verified.")
            value, _ = inventory.record(f"runs/{name}/run.json")
            run = RunRecord.model_validate(value)
            if run.id != name:
                raise ProjectExportError("Run identity could not be verified.")
            if run.project_id == project_id:
                runs.append(run)
                if len(runs) > MAX_RUNS:
                    raise ProjectExportError("Project has too many runs for one export.")
        if seed is not None:
            selected = next((run for run in runs if run.id == seed), None)
            if selected is None:
                raise LedgerNotFoundError(seed)
            session_id = selected.sdk_session_id or selected.resume_session_id
            runs = [
                run
                for run in runs
                if run.id == seed
                or (
                    session_id is not None
                    and (run.sdk_session_id == session_id or run.resume_session_id == session_id)
                )
            ]
        return sorted(runs, key=lambda item: (item.created_at, item.id))

    def _conversation(self, inventory, runs):
        sections = ["# Conversation export\n"]
        for run in runs:
            inventory.file(f"runs/{run.id}/run.json")
            raw = inventory.file(f"runs/{run.id}/events.jsonl", optional=True) or b""
            if raw and not raw.endswith(b"\n"):
                raise ProjectExportError("Run event log has an incomplete tail.")
            sections.extend(
                [
                    f"## {run.id}\n",
                    f"Created: {run.created_at.isoformat()}\n",
                    f"Status: {run.status.value}\n",
                    "### User\n",
                    run.prompt + "\n",
                ]
            )
            sequence = 0
            messages = []
            deltas = {}
            for line in raw.splitlines():
                if not line.strip():
                    continue
                inventory.events += 1
                if inventory.events > MAX_EVENTS:
                    raise ProjectExportError("Export has too many conversation events.")
                event = EventEnvelope.model_validate_json(line)
                if (
                    event.run_id != run.id
                    or event.project_id != run.project_id
                    or event.sequence != sequence + 1
                ):
                    raise ProjectExportError(
                        "Run event identity or sequence could not be verified."
                    )
                sequence = event.sequence
                stream = (event.actor.type, event.actor.name, event.actor.agent_id, event.parent_id)
                label = "Assistant"
                if event.actor.type == "subagent" or event.parent_id:
                    label = "Branch " + json.dumps(
                        event.actor.agent_id or event.parent_id or event.actor.name,
                        ensure_ascii=False,
                    )
                if event.type == EventType.ASSISTANT_TEXT_DELTA.value:
                    text = event.data.get("text", "")
                    if isinstance(text, str):
                        deltas.setdefault(stream, (label, []))[1].append(text)
                elif event.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value:
                    text = event.data.get("text", "")
                    if isinstance(text, str) and text:
                        messages.append((label, text))
                        deltas.pop(stream, None)
            # Completed messages are authoritative; a failed final partial message
            # remains visibly partial instead of being silently lost or duplicated.
            for label, message in messages:
                sections.extend([f"### {label}\n", message + "\n"])
            for label, chunks in deltas.values():
                sections.extend([f"### {label} (partial)\n", "".join(chunks) + "\n"])
            inventory.file(f"runs/{run.id}/summary.json", optional=True)
            inventory.file(f"runs/{run.id}/task-experience-context.json", optional=True)
        inventory.add("conversation.md", "\n".join(sections).encode())

    def _related(self, inventory, project_id):
        for relative in (
            f"receipts/{project_id}",
            f"commercial-actions/{project_id}",
            f"collaboration/project-templates/{project_id}",
        ):
            inventory.tree(relative)
        for relative in (
            f"collaboration/comments/{project_id}.json",
            f"auth/projects/{project_id}/access.json",
            f"auth/marketplaces/{project_id}.json",
        ):
            inventory.file(relative, optional=True)
        for directory in (
            "notifications",
            "intents",
            "feedback",
            "grants",
            "channel-deliveries",
            "shop-browsers",
            "task-experiences",
        ):
            for name, _info in inventory.children(directory):
                if not name.endswith(".json"):
                    continue
                value, raw = inventory.record(f"{directory}/{name}")
                if value.get("project_id") == project_id:
                    inventory.add(f"data/{directory}/{name}", raw, source=f"{directory}/{name}")
        for directory, record_name in (
            ("schedules", "schedule.json"),
            ("skill-proposals", "record.json"),
        ):
            for name, info in inventory.children(directory):
                if name.startswith("."):
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    raise ProjectExportError("Project-related storage could not be verified.")
                value, _ = inventory.record(f"{directory}/{name}/{record_name}")
                if value.get("project_id") == project_id:
                    inventory.tree(f"{directory}/{name}")

    def inventory(self, project_id, principal, *, run_id=None):
        project = self.authorize(project_id, principal)
        inventory = ExportInventory(self.ledger.root, lambda: self.authorize(project_id, principal))
        runs = self._runs(inventory, project_id, run_id)
        for run in runs:
            if run.status not in TERMINAL_RUN_STATUSES or self.runs.is_executing(run.id):
                raise ProjectExportError("Wait for the selected runs to finish before exporting.")
            if (
                run.identity_binding != project.identity_binding
                or run.tenant_id != project.tenant_id
            ):
                raise ProjectExportError("Run scope could not be verified.")
        if run_id is None:
            inventory.tree(f"projects/{project_id}")
            self._related(inventory, project_id)
        else:
            inventory.add(
                "session.json",
                encode(
                    {
                        "project_id": project_id,
                        "selected_run_id": run_id,
                        "run_ids": [run.id for run in runs],
                        "sdk_session_id": next(
                            (
                                run.sdk_session_id or run.resume_session_id
                                for run in runs
                                if run.id == run_id
                            ),
                            None,
                        ),
                    }
                ),
            )
        self._conversation(inventory, runs)
        selected_ids = {run.id for run in runs}
        artifacts = [
            item for item in project.artifacts if run_id is None or item.run_id in selected_ids
        ]
        artifact_index = []
        for item in artifacts:
            if item.project_id != project_id:
                raise ProjectExportError("Artifact scope could not be verified.")
            source = f"projects/{project_id}/" + (
                snapshot_relative(item.checksum)
                if item.content_storage == "snapshot"
                else f"workspace/{item.path}"
            )
            raw = inventory.read(source, optional=True)
            if (
                raw is None
                or len(raw) != item.size_bytes
                or hashlib.sha256(raw).hexdigest() != item.checksum
            ):
                if item.content_storage == "snapshot":
                    raise ProjectExportError("A saved artifact version could not be verified.")
                inventory.warnings.append({"code": "legacy_artifact_unavailable", "id": item.id})
                artifact_index.append({**item.model_dump(mode="json"), "export_path": None})
                continue
            target = f"data/{source}"
            if target not in inventory.files:
                inventory.add(target, raw, source=source)
            artifact_index.append({**item.model_dump(mode="json"), "export_path": target})
        inventory.add("artifacts.json", encode(artifact_index))
        snapshot = self.memory.export_scope(MemoryScope.PROJECT, project_id)
        if sum(len(values) for values in snapshot.values()) > MAX_FILES:
            raise ProjectExportError("Project has too many memory records for one export.")
        memories = {}
        for key, values in snapshot.items():
            records = [value["item"] if key == "history" else value for value in values]
            if any(
                item["identity_binding"] != project.identity_binding
                or item["tenant_id"] != project.tenant_id
                for item in records
            ):
                raise ProjectExportError("Memory scope could not be verified.")
            memories[key] = [
                value
                for value, item in zip(values, records, strict=True)
                if run_id is None or item.get("source_run_id") in selected_ids
            ]
        inventory.add("project-memory.json", encode(memories))
        descriptor = {
            "schema_version": 1,
            "kind": "project" if run_id is None else "session",
            "project_id": project_id,
            "selected_run_id": run_id,
            "project_revision": project.revision,
            "run_ids": [run.id for run in runs],
            "files": inventory.rows(),
            "directories": sorted(inventory.directories),
            "total_bytes": inventory.total_bytes,
            "warnings": inventory.warnings,
            "exclusions": EXCLUSIONS,
        }
        inventory.descriptor = descriptor
        inventory.fingerprint = hashlib.sha256(encode(descriptor)).hexdigest()
        self.authorize(project_id, principal)
        return inventory

    def preview(self, project_id, principal, *, run_id=None):
        self.authorize(project_id, principal)
        with workspace_lease(self.ledger, project_id):
            inventory = self.inventory(project_id, principal, run_id=run_id)
        return {
            key: value
            for key, value in inventory.descriptor.items()
            if key not in {"files", "directories"}
        } | {
            "fingerprint": inventory.fingerprint,
            "file_count": len(inventory.files),
        }

    def create(self, project_id, principal, fingerprint, *, run_id=None):
        self.authorize(project_id, principal)
        with workspace_lease(self.ledger, project_id):
            inventory = self.inventory(project_id, principal, run_id=run_id)
            if inventory.fingerprint != fingerprint:
                raise ProjectExportError("Project data changed. Refresh the export preview.")
            archive = tempfile.TemporaryFile(mode="w+b")
            try:
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                    for item in inventory.files.values():
                        inventory.authorize()
                        raw = inventory.read(item.source) if item.source else item.content
                        if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.sha256:
                            raise ProjectExportError("Project data changed during export.")
                        zipped.writestr(item.path, raw)
                    for directory in sorted(inventory.directories):
                        zipped.writestr(directory, b"")
                    zipped.writestr(
                        "manifest.json",
                        encode(
                            {
                                **inventory.descriptor,
                                "fingerprint": inventory.fingerprint,
                                "exported_at": utc_now().isoformat(),
                                "exported_by": project_actor(principal),
                            }
                        ),
                    )
                final = self.inventory(project_id, principal, run_id=run_id)
                if final.fingerprint != inventory.fingerprint:
                    raise ProjectExportError("Project data changed during export.")
                self.authorize(project_id, principal)
                size = archive.tell()
                archive.seek(0)
                return archive, size
            except BaseException:
                archive.close()
                raise
