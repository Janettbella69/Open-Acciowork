"""Bounded, non-following reads and lossless-text redaction for project exports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .backup_files import BackupLimits, parse_json
from .events import SECRET_KEY_FRAGMENTS, _redact_text
from .project_export_models import ExportEntry, ProjectExportConflict
from .skill_proposal_files import directory_fd, read_relative, relative_parts

EXPORT_LIMITS = BackupLimits(files=10_000, entries=30_000, total_bytes=128 * 1024 * 1024)
JSON_BYTES = 16 * 1024 * 1024
TOKEN_COUNTER_KEYS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens", "context_window_tokens", "max_tokens", "max_output_tokens",
    "inputtokens", "outputtokens", "cachecreationinputtokens", "cachereadinputtokens",
})


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n").encode()


def _parse_record(raw: bytes):
    try:
        return parse_json(raw)
    except RecursionError as exc:
        raise ProjectExportConflict("Project records are too deeply nested to export.") from exc


def export_redact(value, *, key="", depth=0):
    """Use the event secret policy, but never truncate an exported record silently."""
    if depth > 64:
        raise ProjectExportConflict("Project records are too deeply nested to export.")
    normalized = key.lower().replace("-", "_")
    private_session = normalized == "session_id" or normalized.endswith("_session_id")
    sensitive = any(fragment in normalized for fragment in SECRET_KEY_FRAGMENTS)
    counter = (
        normalized in TOKEN_COUNTER_KEYS
        and isinstance(value, int | float)
        and not isinstance(value, bool)
    )
    if private_session or (sensitive and not counter):
        return "[REDACTED]"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Invalid export JSON number")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("Invalid export JSON key")
        return {k: export_redact(v, key=k, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [export_redact(v, depth=depth + 1) for v in value]
    raise ValueError("Invalid export JSON value")


@dataclass
class ExportCapture:
    root: Path
    limits: BackupLimits = EXPORT_LIMITS
    files: dict[str, bytes] = field(default_factory=dict)
    entries: list[ExportEntry] = field(default_factory=list)
    directories: set[str] = field(default_factory=set)
    observed: dict[str, str] = field(default_factory=dict)
    read_bytes: int = 0
    total_bytes: int = 0
    scanned_entries: int = 0
    _reads: dict[str, tuple[bool, int]] = field(default_factory=dict)
    _listings: set[str] = field(default_factory=set)
    _selectors: set[tuple[str, str, str | None, bool]] = field(default_factory=set)
    _paths: dict[str, tuple[str, str]] = field(default_factory=dict)

    def _observe(self, name: str, digest: str):
        previous = self.observed.get(name)
        if previous is not None and previous != digest:
            raise ProjectExportConflict("Project data changed. Review a fresh export.")
        self.observed[name] = digest

    def _read(self, relative: str, *, optional=False, limit=JSON_BYTES):
        relative_parts(relative)
        limit = min(limit, self.limits.file_bytes)
        # Charge all source IO, including final verification and unrelated rows
        # scanned to find records belonging to this project.
        remaining = self.limits.total_bytes * 4 - self.read_bytes
        try:
            raw = read_relative(self.root, relative, min(limit, remaining))
        except FileNotFoundError:
            if not optional:
                raise
            raw = None
        if raw is not None:
            self.read_bytes += len(raw)
        return raw

    def read(self, relative: str, *, optional=False, limit=JSON_BYTES):
        raw = self._read(relative, optional=optional, limit=limit)
        self._observe(
            "file:" + relative,
            "missing" if raw is None else hashlib.sha256(raw).hexdigest(),
        )
        self._reads[relative] = (optional, limit)
        return raw

    def json(self, relative: str, *, optional=False):
        raw = self.read(relative, optional=optional)
        return None if raw is None else _parse_record(raw)

    def lines(self, relative: str):
        raw = self.read(relative, optional=True)
        if raw is None:
            return []
        return [_parse_record(line) for line in raw.splitlines() if line.strip()]

    def _scan_names(self, relative: str):
        relative_parts(relative)
        try:
            with directory_fd(self.root / relative) as fd, os.scandir(fd) as children:
                names = []
                for child in children:
                    self.scanned_entries += 1
                    if self.scanned_entries > self.limits.entries:
                        raise ProjectExportConflict("Project export scan exceeds its entry limit.")
                    relative_parts(child.name)
                    names.append(child.name)
        except FileNotFoundError:
            names = None
        if names is not None:
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("Ambiguous export source paths")
            names.sort()
        return names

    def _names(self, relative: str):
        names = self._scan_names(relative)
        self._observe("names:" + relative, hashlib.sha256(json_bytes(names)).hexdigest())
        self._listings.add(relative)
        return names

    def names(self, relative: str):
        return self._names(relative) or []

    def select_json(self, root: str, project_id: str, *, child_file=None, skip_non_json=False):
        """Bind only project members when scanning a shared durable store.

        Unrelated project writes must not stale this preview. Verification still
        scans every row to detect new members or rows reassigned to this project.
        """
        if child_file is not None:
            relative_parts(child_file)
        selected = []
        for name in self._scan_names(root) or []:
            if name.startswith("."):
                continue
            if child_file is None and not name.endswith(".json"):
                if skip_non_json:
                    continue
                raise ValueError("Unexpected export source record path")
            source = root + "/" + name + ("/" + child_file if child_file else "")
            raw = self._read(source)
            value = _parse_record(raw)
            if not isinstance(value, dict):
                raise ValueError("Invalid export source record")
            if value.get("project_id") != project_id:
                continue
            self._observe("file:" + source, hashlib.sha256(raw).hexdigest())
            selected.append((name, value))
        selector = (root, project_id, child_file, skip_non_json)
        key = "selector:" + json.dumps(selector, ensure_ascii=False)
        digest = hashlib.sha256(json_bytes([name for name, _value in selected])).hexdigest()
        self._observe(key, digest)
        self._selectors.add(selector)
        return selected

    def _claim_path(self, relative: str, kind: str):
        parts = relative_parts(relative)
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            entry_kind = kind if index == len(parts) else "directory"
            normalized = path.casefold()
            previous = self._paths.get(normalized)
            if previous is not None:
                if previous != (path, "directory") or entry_kind != "directory":
                    raise ValueError("Duplicate or conflicting export path")
                continue
            if len(self._paths) >= self.limits.entries:
                raise ProjectExportConflict("Project export exceeds its entry limit.")
            self._paths[normalized] = (path, entry_kind)
            if entry_kind == "directory":
                self.directories.add(path)

    def add(self, relative: str, raw: bytes, category: str):
        if len(self.files) >= self.limits.files or len(raw) > self.limits.file_bytes:
            raise ProjectExportConflict("Project export exceeds its file limit.")
        if self.total_bytes + len(raw) > self.limits.total_bytes:
            raise ProjectExportConflict("Project export exceeds its total size limit.")
        self._claim_path(relative, "file")
        self.total_bytes += len(raw)
        self.files[relative] = raw
        self.entries.append(ExportEntry(
            path=relative, category=category, size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        ))

    def add_json(self, relative: str, value, category: str):
        self.add(relative, self._encode_rows([value], indent=2), category)

    def add_lines(self, relative: str, rows, category: str):
        self.add(relative, self._encode_rows(rows), category)

    def _encode_rows(self, rows, *, indent=None):
        # Pretty-printing deeply nested records can expand a small source many
        # times. Enforce the output budget while encoding, before allocating it.
        limit = min(self.limits.file_bytes, self.limits.total_bytes - self.total_bytes)
        output = bytearray()
        encoder = json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, allow_nan=False, indent=indent,
        )
        for row in rows:
            for chunk in encoder.iterencode(export_redact(row)):
                encoded = chunk.encode()
                if len(output) + len(encoded) > limit:
                    raise ProjectExportConflict("Project export exceeds its record size limit.")
                output.extend(encoded)
            if len(output) >= limit:
                raise ProjectExportConflict("Project export exceeds its record size limit.")
            output.extend(b"\n")
        return bytes(output)

    def tree(self, source: str, target: str, category: str):
        relative_parts(source)
        relative_parts(target)

        def walk(relative_source: str, relative_target: str, depth: int):
            if depth > 32:
                raise ProjectExportConflict("Project export paths exceed the depth limit.")
            names = self._names(relative_source)
            if names is None:
                if depth:
                    raise ProjectExportConflict("Project data changed. Review a fresh export.")
                return
            self._claim_path(relative_target, "directory")
            # Each file is captured once. Final verification rechecks all
            # observed files and listings, including empty directories.
            with directory_fd(self.root / relative_source) as fd:
                for name in names:
                    child_source = relative_source + "/" + name
                    child_target = relative_target + "/" + name
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        walk(child_source, child_target, depth + 1)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        raw = self.read(child_source, limit=self.limits.file_bytes)
                        self.add(child_target, raw, category)
                    else:
                        raise ValueError("Export source contains a linked or special file")

        walk(source, target, 0)

    def verify(self):
        """Reject source changes before handing out any captured archive bytes."""
        try:
            for relative, (optional, limit) in list(self._reads.items()):
                self.read(relative, optional=optional, limit=limit)
            for relative in sorted(self._listings):
                self._names(relative)
            for root, project_id, child_file, skip_non_json in list(self._selectors):
                self.select_json(
                    root, project_id, child_file=child_file, skip_non_json=skip_non_json,
                )
        except (OSError, ValueError) as exc:
            raise ProjectExportConflict("Project data changed. Review a fresh export.") from exc
