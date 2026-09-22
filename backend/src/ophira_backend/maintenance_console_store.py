"""Reviewed, recoverable publication around the existing cold backup functions."""

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .application_backup import DEFAULT_LIMITS, create_backup, implementation, restore_backup
from .backup_files import (
    BackupError,
    parse_json,
    private_json,
    publish_exclusive,
    remove_staging,
    tree_inventory,
)
from .maintenance import data_directory_lease
from .maintenance_console_models import REQUEST_ID, MaintenanceRecord
from .models import utc_now
from .skill_proposal_files import directory_fd, read_relative

MAX_RECORDS = 200


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def identity(path):
    with directory_fd(path) as fd:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino


class MaintenanceConsole:
    def __init__(self, data, plugin, workspace):
        self.data, self.plugin, self.root = (
            Path(item).absolute() for item in (data, plugin, workspace)
        )
        for source in (self.data, self.plugin):
            left, right = self.root.resolve(), source.resolve()
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise BackupError("maintenance_workspace_overlaps_source")
        with directory_fd(self.root.parent):
            self.root.mkdir(mode=0o700, exist_ok=True)
        with directory_fd(self.root) as fd:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise BackupError("maintenance_workspace_must_be_private")
        self.scope = fingerprint({"data": str(self.data), "plugin": str(self.plugin)})
        with data_directory_lease(self.root, exclusive=True):
            for name in ("backups", "restores", "reviews"):
                path = self.root / name
                path.mkdir(mode=0o700, exist_ok=True)
                with directory_fd(path):
                    pass
            try:
                scope = parse_json(read_relative(self.root, "scope.json", 2048))
            except FileNotFoundError:
                private_json(self.root / "scope.json", {"scope": self.scope})
            else:
                if scope != {"scope": self.scope}:
                    raise BackupError("maintenance_workspace_scope_changed")

    def _record(self, request_id):
        if not re.fullmatch(REQUEST_ID, request_id):
            raise BackupError("invalid_maintenance_request")
        raw = read_relative(self.root / "reviews", f"{request_id}.json", 64 * 1024)
        record = MaintenanceRecord.model_validate(parse_json(raw))
        if record.request_id != request_id or record.scope != self.scope:
            raise BackupError("maintenance_record_scope_changed")
        if record.fingerprint and record.fingerprint != self._fingerprint(record):
            raise BackupError("maintenance_review_changed")
        return record

    def _write(self, record):
        MaintenanceRecord.model_validate(record.model_dump())
        parent = self.root / "reviews"
        temporary = parent / f".{record.request_id}.{uuid4().hex}.tmp"
        with directory_fd(parent) as fd:
            try:
                private_json(temporary, record.model_dump(mode="json"))
                os.replace(temporary, parent / f"{record.request_id}.json")
                os.fsync(fd)
            finally:
                temporary.unlink(missing_ok=True)

    def _records(self):
        with directory_fd(self.root / "reviews") as fd:
            names = os.listdir(fd)
        if len(names) > MAX_RECORDS * 2:
            raise BackupError("maintenance_history_limit")
        records = [self._record(name[:-5]) for name in names if name.endswith(".json")]
        return sorted(records, key=lambda row: row.created_at, reverse=True)

    def _backups(self):
        rows = []
        with directory_fd(self.root / "backups") as fd, os.scandir(fd) as children:
            for index, child in enumerate(children):
                if index >= MAX_RECORDS * 2:
                    raise BackupError("maintenance_backup_limit")
                if child.name.startswith(".") or not child.is_dir(follow_symlinks=False):
                    continue
                rows.append({"id": fingerprint(child.name), "name": child.name})
        return sorted(rows, key=lambda row: row["name"])

    def _parent(self, record):
        return self.root / ("backups" if record.kind == "backup" else "restores")

    def _target(self, record):
        return self._parent(record) / f"{record.kind}-{record.request_id}"

    def _stage(self, record):
        if record.stage_name is None:
            return None
        path = self._parent(record) / record.stage_name
        try:
            current = identity(path)
        except FileNotFoundError:
            return None
        if current != record.stage_identity:
            raise BackupError("maintenance_staging_changed")
        return path

    def _fingerprint(self, record):
        return fingerprint(
            {
                "request_id": record.request_id,
                "kind": record.kind,
                "backup_id": record.backup_id,
                "scope": record.scope,
                "reviewed_at": record.reviewed_at.isoformat(),
                "stage": record.stage_name,
                "stage_identity": record.stage_identity,
                "candidate_identity": record.candidate_identity,
                "tree_hash": record.tree_hash,
                "implementation_hash": record.implementation_hash,
                "summary": record.summary,
            }
        )

    def _tree_hash(self, record, path):
        limits = DEFAULT_LIMITS
        if record.kind == "backup":
            limits = replace(
                limits,
                files=limits.files + 1,
                entries=limits.entries + 3,
                total_bytes=limits.total_bytes + limits.manifest_bytes,
            )
        return fingerprint(tree_inventory(path, limits))

    def _view(self, record):
        return {
            "request_id": record.request_id,
            "kind": record.kind,
            "backup_id": record.backup_id,
            "state": record.state,
            "created_at": record.created_at.isoformat(),
            "reviewed_at": record.reviewed_at.isoformat() if record.reviewed_at else None,
            "fingerprint": record.fingerprint,
            "summary": record.summary,
            "destination": str(self._target(record)),
        }

    def status(self):
        with data_directory_lease(self.root, exclusive=True):
            return {
                "data_directory": str(self.data),
                "plugin_directory": str(self.plugin),
                "backup_directory": str(self.root / "backups"),
                "restore_directory": str(self.root / "restores"),
                "backups": self._backups(),
                "operations": [self._view(row) for row in self._records()],
            }

    def prepare(self, request):
        with data_directory_lease(self.root, exclusive=True):
            try:
                record = self._record(request.request_id)
            except FileNotFoundError:
                rows = self._records()
                if len(rows) >= MAX_RECORDS:
                    raise BackupError("maintenance_history_limit") from None
                if any(row.state in {"preparing", "reviewed", "publishing"} for row in rows):
                    raise BackupError("finish_current_maintenance_review") from None
                record = MaintenanceRecord(**request.model_dump(), scope=self.scope)
                self._write(record)
            if record.kind != request.kind or record.backup_id != request.backup_id:
                raise BackupError("maintenance_request_changed")
            if record.state != "preparing":
                return self._view(record)
            source = None
            if record.kind == "restore":
                match = next(
                    (row for row in self._backups() if row["id"] == record.backup_id), None
                )
                if match is None:
                    raise BackupError("backup_not_found")
                source = self.root / "backups" / match["name"]
            previous = self._stage(record)
            if previous is not None:
                remove_staging(previous)
            stage = Path(
                tempfile.mkdtemp(prefix=f".console-{record.request_id}-", dir=self._parent(record))
            )
            record.stage_name, record.stage_identity = stage.name, identity(stage)
            self._write(record)
            candidate = stage / "candidate"
            before_code = fingerprint(implementation())
            result = (
                create_backup(self.data, self.plugin, candidate)
                if record.kind == "backup"
                else restore_backup(source, candidate)
            )
            if fingerprint(implementation()) != before_code:
                raise BackupError("implementation_changed")
            record.summary = {
                key: value
                for key, value in result.items()
                if key
                in {
                    "files",
                    "bytes",
                    "projects",
                    "runs",
                    "proposals",
                    "proposal_states",
                    "canonical_preserved",
                    "provider_invoked",
                }
            }
            record.tree_hash = self._tree_hash(record, candidate)
            record.candidate_identity = identity(candidate)
            record.implementation_hash = before_code
            record.reviewed_at = utc_now()
            record.fingerprint = self._fingerprint(record)
            record.state = "reviewed"
            self._write(record)
            return self._view(record)

    def confirm(self, request_id, confirmation, *, authorize=lambda: None):
        with data_directory_lease(self.root, exclusive=True):
            authorize()
            record = self._record(request_id)
            if confirmation.fingerprint != record.fingerprint:
                raise BackupError("maintenance_review_changed")
            if record.state == "completed":
                return self._view(record)
            if record.state not in {"reviewed", "publishing"}:
                raise BackupError("maintenance_review_required")
            if record.implementation_hash != fingerprint(implementation()):
                raise BackupError("implementation_changed")
            stage, target = self._stage(record), self._target(record)
            candidate = stage / "candidate" if stage else None
            if record.state == "reviewed":
                if utc_now() - record.reviewed_at > timedelta(days=1):
                    raise BackupError("maintenance_review_expired")
                if candidate is None or target.exists() or target.is_symlink():
                    raise BackupError("maintenance_target_conflict")
                self._verify_candidate(record, candidate)
                authorize()
                record.state = "publishing"
                self._write(record)
            # The durable publishing state commits this reviewed copy. A lost
            # response or process exit resumes the same no-replace publication.
            if target.exists() or target.is_symlink():
                if candidate is not None and candidate.exists():
                    raise BackupError("maintenance_target_conflict")
                self._verify_candidate(record, target)
            else:
                if candidate is None:
                    raise BackupError("maintenance_staging_missing")
                self._verify_candidate(record, candidate)
                publish_exclusive(candidate, target)
            with directory_fd(target.parent) as fd:
                os.fsync(fd)
            record.state = "completed"
            self._write(record)
            if stage is not None:
                remove_staging(stage)
            return self._view(record)

    def _verify_candidate(self, record, path):
        if (
            identity(path) != record.candidate_identity
            or self._tree_hash(record, path) != record.tree_hash
        ):
            raise BackupError("maintenance_prepared_copy_changed")

    def discard(self, request_id):
        with data_directory_lease(self.root, exclusive=True):
            record = self._record(request_id)
            if record.state in {"publishing", "completed"}:
                raise BackupError("maintenance_already_committed")
            stage = self._stage(record)
            if stage is not None:
                remove_staging(stage)
            record.state = "discarded"
            self._write(record)
            return self._view(record)
