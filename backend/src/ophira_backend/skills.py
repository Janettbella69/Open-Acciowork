from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from .models import SkillCheck, SkillTestResult, SkillVersion


class SkillNotFoundError(KeyError):
    pass


class SkillIdentifierError(ValueError):
    """A caller supplied an invalid public Skill identifier."""


class SkillConflictError(ValueError):
    """A valid Skill request conflicts with immutable registry state."""


class SkillIntegrityError(RuntimeError):
    """Persisted Skill history or registry state failed verification."""


SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*\S{6,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
)
DANGEROUS_PATTERNS = (
    re.compile(r"rm\s+-rf\s+[/~]"),
    re.compile(r"(?i)(curl|wget)[^\n]*\|\s*(ba|z)?sh"),
    re.compile(r"(?i)\b(curl|wget)\b[^\n]*(--data|-d\b|--upload-file|-T\b)"),
    re.compile(r"(?i)\b(requests|httpx)\.(post|put|patch)\s*\("),
    re.compile(r"(?i)\b(nc|netcat)\s+[^\n]+"),
    re.compile(r"chmod\s+777"),
)
REFERENCE_PATTERN = re.compile(r"references/[A-Za-z0-9._/-]+")
MAX_SKILL_FILE_BYTES = 200_000
MAX_SKILL_ARCHIVE_BYTES = 2_000_000
MAX_SKILL_ARCHIVE_ENTRIES = 128
MAX_SKILL_UNPACKED_BYTES = 5_000_000
# Immutable history is never pruned because a persisted Run may still pin any
# version. New tenant publications fail closed at these supply-chain quotas.
MAX_TENANT_SKILL_VERSIONS_PER_SKILL = 32
MAX_TENANT_SKILL_HISTORY_BYTES = 256 * 1024 * 1024
# Runtime views are reproducible caches backed by immutable history, so inactive
# entries may be reclaimed. Active SDK sessions hold cross-process shared leases.
MAX_RUNTIME_SKILL_CACHE_ENTRIES = 32
MAX_RUNTIME_SKILL_CACHE_BYTES = 512 * 1024 * 1024
TENANT_PLUGIN_NAMESPACE = "ophira-tenant"
SKILL_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PLUGIN_NAMESPACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
CONTENT_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SkillRuntimeView:
    """The exact immutable Plugin directories and Skill IDs for one run."""

    plugin_paths: list[dict[str, str]]
    skill_ids: list[str]
    _lease_handle: IO[bytes] | None = field(default=None, repr=False, compare=False)

    def close(self) -> None:
        """Release an optional cross-process cache lease."""
        if self._lease_handle is not None and not self._lease_handle.closed:
            fcntl.flock(self._lease_handle.fileno(), fcntl.LOCK_UN)
            self._lease_handle.close()

    def __enter__(self) -> SkillRuntimeView:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class SkillRegistry:
    """Review, version, and materialize filesystem Skills for SDK sessions.

    Tenant installs are content-addressed and an atomic pointer selects the current
    version. Run snapshots are materialized as read-only Plugin roots, so a queued
    or retried run never observes a later global Skill upgrade.
    """

    def __init__(self, plugin_dir: Path, state_dir: Path) -> None:
        self.plugin_dir = plugin_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tenant_plugin_dir = self._managed_directory("tenant-plugin")
        self.tenant_dir = self._managed_directory("tenant-plugin", "skills")
        self.versions_dir = self._managed_directory("versions")
        self.runtime_dir = self._managed_directory("runtime")
        self.runtime_lease_dir = self._managed_directory("runtime-leases")
        self.staging_dir = self._managed_directory("staging")
        # ``enabled.json`` and ``current.json`` are legacy inputs only. All new
        # mutations use one atomic document so a crash cannot switch a version
        # without its matching enable state (or vice versa).
        self.state_path = self.state_dir / "enabled.json"
        self.current_path = self.state_dir / "current.json"
        self.registry_state_path = self.state_dir / "registry-state.json"
        self.lock_path = self.state_dir / ".registry.lock"
        self._lock = threading.RLock()

        with self._exclusive():
            manifest_dir = self.tenant_plugin_dir / ".claude-plugin"
            if manifest_dir.is_symlink():
                raise ValueError("Tenant Plugin manifest directory must not be a symlink")
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest = manifest_dir / "plugin.json"
            if manifest.is_symlink():
                raise ValueError("Tenant Plugin manifest must not be a symlink")
            if not manifest.exists():
                self._atomic_json_write(
                    manifest,
                    {
                        "name": TENANT_PLUGIN_NAMESPACE,
                        "version": "1.0.0",
                        "description": "Tenant-reviewed OPHIRA Skills",
                    },
                )
            self._migrate_legacy_tenant_skills_unlocked()

    def _managed_directory(self, *parts: str) -> Path:
        path = self.state_dir.joinpath(*parts)
        if path.is_symlink():
            raise ValueError("Managed Skill directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve()
        try:
            resolved.relative_to(self.state_dir)
        except ValueError as exc:
            raise ValueError("Managed Skill directory escapes state root") from exc
        return resolved

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialize state transitions across threads and backend processes."""
        with self._lock:
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.lock_path, flags, 0o600)
            try:
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError("Skill registry lock must be a regular file")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
        if path.is_symlink():
            raise SkillIntegrityError("Skill registry state must not be a symlink")
        descriptor = -1
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except (OSError, TypeError, ValueError) as exc:
            raise SkillIntegrityError("Skill registry state could not be written") from exc
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _read_mapping(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise SkillIntegrityError("Skill registry state must not be a symlink")
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SkillIntegrityError("Skill registry state is unreadable") from exc
        if not isinstance(value, dict):
            raise SkillIntegrityError("Skill registry state must be an object")
        return value

    @staticmethod
    def _validate_enabled_state(values: dict[str, Any]) -> dict[str, bool]:
        if any(
            not isinstance(key, str) or not isinstance(value, bool)
            for key, value in values.items()
        ):
            raise SkillIntegrityError(
                "Skill enabled state must map string IDs to booleans"
            )
        return dict(values)

    def _validate_current_state(self, values: dict[str, Any]) -> dict[str, str]:
        current: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SkillIntegrityError(
                    "Skill current-version state must map string IDs to hashes"
                )
            try:
                namespace, name = self._parse_skill_id(key)
            except SkillIdentifierError as exc:
                raise SkillIntegrityError(
                    "Skill current-version state contains an invalid Skill ID"
                ) from exc
            if namespace != TENANT_PLUGIN_NAMESPACE:
                raise SkillIntegrityError(
                    "Only tenant Skills may have mutable current-version pointers"
                )
            if not CONTENT_HASH_PATTERN.fullmatch(value):
                raise SkillIntegrityError(
                    "Skill current-version state contains an invalid content hash"
                )
            current[f"{namespace}:{name}"] = value
        return current

    def _write_registry_state_unlocked(
        self, enabled: dict[str, bool], current: dict[str, str]
    ) -> None:
        self._atomic_json_write(
            self.registry_state_path,
            {
                "schema_version": 1,
                "enabled": enabled,
                "current": current,
            },
        )

    def _registry_state_unlocked(self) -> tuple[dict[str, bool], dict[str, str]]:
        if self.registry_state_path.exists() or self.registry_state_path.is_symlink():
            state = self._read_mapping(self.registry_state_path)
            if set(state) != {"schema_version", "enabled", "current"}:
                raise SkillIntegrityError("Skill registry state has an invalid schema")
            if type(state["schema_version"]) is not int or state["schema_version"] != 1:
                raise SkillIntegrityError(
                    "Skill registry state has an unsupported schema version"
                )
            enabled_value = state["enabled"]
            current_value = state["current"]
            if not isinstance(enabled_value, dict) or not isinstance(current_value, dict):
                raise SkillIntegrityError(
                    "Skill registry state selections must be objects"
                )
            return (
                self._validate_enabled_state(enabled_value),
                self._validate_current_state(current_value),
            )

        # One-time migration from the former two-file state. Publishing the
        # combined document is atomic; legacy files remain untouched as a
        # recovery aid and are ignored after this point.
        enabled = self._validate_enabled_state(self._read_mapping(self.state_path))
        current = self._validate_current_state(self._read_mapping(self.current_path))
        self._write_registry_state_unlocked(enabled, current)
        return enabled, current

    def _namespace(self) -> str:
        manifest = self.plugin_dir / ".claude-plugin" / "plugin.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            value = str(data["name"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SkillIntegrityError("OPHIRA Plugin manifest is invalid") from exc
        if not PLUGIN_NAMESPACE_PATTERN.fullmatch(value):
            raise SkillIntegrityError(
                "OPHIRA Plugin manifest contains an invalid namespace"
            )
        return value

    @staticmethod
    def _frontmatter(path: Path) -> dict[str, str]:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("Skill must be readable UTF-8 text") from exc
        if not content.startswith("---\n"):
            raise ValueError("Skill is missing YAML frontmatter")
        sections = content.split("---\n", 2)
        if len(sections) != 3:
            raise ValueError("Skill frontmatter is not closed")
        header = sections[1]
        values: dict[str, str] = {}
        for line in header.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"name", "description", "version"}:
                values[key.strip()] = value.strip().strip('"\'')
        if not values.get("name") or not values.get("description"):
            raise ValueError("Skill requires name and description")
        return values

    @staticmethod
    def _hash_directory(directory: Path) -> str:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Skill package is not a real directory")
        digest = hashlib.sha256()
        try:
            paths = sorted(directory.rglob("*"))
            for path in paths:
                if path.is_symlink():
                    raise ValueError("Skill package contains a symbolic link")
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ValueError("Skill package contains a special file")
                digest.update(path.relative_to(directory).as_posix().encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
        except OSError as exc:
            raise ValueError("Skill package could not be hashed") from exc
        return digest.hexdigest()

    def _enabled_overrides_unlocked(self) -> dict[str, bool]:
        enabled, _current = self._registry_state_unlocked()
        return enabled

    def _current_versions_unlocked(self) -> dict[str, str]:
        _enabled, current = self._registry_state_unlocked()
        return current

    def _collect_application_unlocked(self, overrides: dict[str, bool]) -> list[SkillVersion]:
        namespace = self._namespace()
        skills: list[SkillVersion] = []
        skills_root = self.plugin_dir / "skills"
        if not skills_root.exists():
            return skills
        for skill_file in sorted(skills_root.glob("*/SKILL.md")):
            try:
                metadata = self._frontmatter(skill_file)
                name = metadata["name"]
                if not SKILL_NAME_PATTERN.fullmatch(name):
                    raise ValueError("invalid application Skill name")
                content_hash = self._hash_directory(skill_file.parent)
            except ValueError as exc:
                raise SkillIntegrityError(
                    "Application Skill package failed integrity validation"
                ) from exc
            identifier = f"{namespace}:{name}"
            skills.append(
                SkillVersion(
                    id=identifier,
                    namespace=namespace,
                    name=name,
                    description=metadata["description"],
                    version=metadata.get("version", "0.0.0"),
                    content_hash=content_hash,
                    source="application",
                    enabled=overrides.get(identifier, True),
                    path=str(skill_file),
                )
            )
        return skills

    @staticmethod
    def _parse_skill_id(skill_id: str) -> tuple[str, str]:
        parts = skill_id.split(":")
        if len(parts) != 2:
            raise SkillIdentifierError(
                "Skill ID must be '<plugin-namespace>:<skill-name>'"
            )
        namespace, name = parts
        if not PLUGIN_NAMESPACE_PATTERN.fullmatch(namespace):
            raise SkillIdentifierError(f"Invalid Skill namespace: {namespace}")
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise SkillIdentifierError(f"Invalid Skill name: {name}")
        return namespace, name

    def _history_path(self, namespace: str, name: str, content_hash: str) -> Path:
        if namespace not in {self._namespace(), TENANT_PLUGIN_NAMESPACE}:
            raise ValueError(f"Unknown managed Skill namespace: {namespace}")
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid Skill name: {name}")
        if not CONTENT_HASH_PATTERN.fullmatch(content_hash):
            raise ValueError("Skill content hash must be a lowercase SHA-256 digest")
        path = self.versions_dir / namespace / name / content_hash
        current = self.versions_dir
        for component in (namespace, name, content_hash):
            current = current / component
            if current.is_symlink():
                raise SkillIntegrityError(
                    f"Skill history path contains a symbolic link: {current}"
                )
        try:
            path.resolve(strict=False).relative_to(self.versions_dir)
        except (OSError, ValueError) as exc:
            raise SkillIntegrityError(
                "Skill history path escapes the managed store"
            ) from exc
        return path

    def _skill_from_history_unlocked(
        self,
        namespace: str,
        name: str,
        content_hash: str,
        *,
        enabled: bool,
    ) -> SkillVersion:
        directory = self._history_path(namespace, name, content_hash)
        if not directory.is_dir() or directory.is_symlink():
            raise SkillNotFoundError(f"{namespace}:{name}@{content_hash}")
        try:
            actual_hash = self._hash_directory(directory)
        except ValueError as exc:
            raise SkillIntegrityError(
                f"Skill history package is invalid for {namespace}:{name}"
            ) from exc
        if actual_hash != content_hash:
            raise SkillIntegrityError(
                f"Skill history integrity check failed for {namespace}:{name}: "
                f"expected {content_hash}, got {actual_hash}"
            )
        try:
            metadata = self._frontmatter(directory / "SKILL.md")
        except ValueError as exc:
            raise SkillIntegrityError(
                f"Skill history metadata is invalid for {namespace}:{name}"
            ) from exc
        if metadata["name"] != name:
            raise SkillIntegrityError(
                f"Skill history name mismatch for {namespace}:{name}: {metadata['name']}"
            )
        return SkillVersion(
            id=f"{namespace}:{name}",
            namespace=namespace,
            name=name,
            description=metadata["description"],
            version=metadata.get("version", "0.0.0"),
            content_hash=content_hash,
            source="tenant" if namespace == TENANT_PLUGIN_NAMESPACE else "application",
            enabled=enabled,
            path=str(directory / "SKILL.md"),
        )

    def _collect_tenant_unlocked(self, overrides: dict[str, bool]) -> list[SkillVersion]:
        current = self._current_versions_unlocked()
        skills: list[SkillVersion] = []
        for identifier, content_hash in sorted(current.items()):
            try:
                skill = self._skill_from_history_unlocked(
                    TENANT_PLUGIN_NAMESPACE,
                    self._parse_skill_id(identifier)[1],
                    content_hash,
                    enabled=overrides.get(identifier, False),
                )
            except SkillNotFoundError as exc:
                raise SkillIntegrityError(
                    "Skill current-version state references missing history"
                ) from exc
            skills.append(skill)
        return skills

    def _list_unlocked(self) -> list[SkillVersion]:
        overrides = self._enabled_overrides_unlocked()
        return [
            *self._collect_application_unlocked(overrides),
            *self._collect_tenant_unlocked(overrides),
        ]

    def list(self) -> list[SkillVersion]:
        with self._exclusive():
            return self._list_unlocked()

    def _get_unlocked(self, skill_id: str) -> SkillVersion:
        skill = next((item for item in self._list_unlocked() if item.id == skill_id), None)
        if skill is None:
            raise SkillNotFoundError(skill_id)
        return skill

    def get(self, skill_id: str) -> SkillVersion:
        self._parse_skill_id(skill_id)
        with self._exclusive():
            return self._get_unlocked(skill_id)

    @staticmethod
    def _make_read_only(directory: Path) -> None:
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_symlink():
                raise ValueError("Cannot seal a symbolic link")
            path.chmod(0o555 if path.is_dir() else 0o444)
        directory.chmod(0o555)

    @staticmethod
    def _directory_size(directory: Path) -> int:
        """Return logical bytes while rejecting links and special files."""
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Skill package is not a real directory")
        total = 0
        try:
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise ValueError("Skill store contains a symbolic link")
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ValueError("Skill store contains a special file")
                total += path.stat().st_size
        except OSError as exc:
            raise ValueError("Skill store could not be measured") from exc
        return total

    @staticmethod
    def _allocated_directory_size(directory: Path) -> int:
        """Return allocated filesystem bytes, including directory blocks."""

        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Skill package is not a real directory")
        total = 0
        try:
            for path in (directory, *directory.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Skill store contains a symbolic link")
                metadata = path.stat()
                if not path.is_dir() and not path.is_file():
                    raise ValueError("Skill store contains a special file")
                blocks = getattr(metadata, "st_blocks", 0)
                total += blocks * 512 if blocks else metadata.st_size
        except OSError as exc:
            raise ValueError("Skill store could not be measured") from exc
        return total

    def _enforce_tenant_history_quota_unlocked(self, skill: SkillVersion) -> None:
        """Reserve no space; deterministically reject a publication over quota.

        This is called under the cross-process registry lock immediately before
        the atomic history publish. Existing history remains readable even if an
        operator later lowers a configured limit.
        """
        skill_root = self.versions_dir / TENANT_PLUGIN_NAMESPACE / skill.name
        if skill_root.is_symlink():
            raise SkillIntegrityError("Tenant Skill history path is a symbolic link")
        existing_versions = 0
        if skill_root.exists():
            try:
                entries = list(skill_root.iterdir())
            except OSError as exc:
                raise SkillIntegrityError(
                    "Tenant Skill history could not be inspected"
                ) from exc
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir():
                    raise SkillIntegrityError(
                        "Tenant Skill history contains an invalid entry"
                    )
                if not CONTENT_HASH_PATTERN.fullmatch(entry.name):
                    raise SkillIntegrityError(
                        "Tenant Skill history contains an invalid hash path"
                    )
                existing_versions += 1
        if existing_versions >= MAX_TENANT_SKILL_VERSIONS_PER_SKILL:
            raise SkillConflictError(
                "tenant Skill version limit exceeded "
                f"({MAX_TENANT_SKILL_VERSIONS_PER_SKILL} per Skill)"
            )

        tenant_root = self.versions_dir / TENANT_PLUGIN_NAMESPACE
        if tenant_root.is_symlink():
            raise SkillIntegrityError("Tenant Skill history root is a symbolic link")
        try:
            current_bytes = (
                self._directory_size(tenant_root) if tenant_root.exists() else 0
            )
        except ValueError as exc:
            raise SkillIntegrityError(
                "Tenant Skill history could not be measured"
            ) from exc
        candidate_bytes = self._directory_size(Path(skill.path).parent)
        if current_bytes + candidate_bytes > MAX_TENANT_SKILL_HISTORY_BYTES:
            raise SkillConflictError(
                "tenant Skill global history byte limit exceeded "
                f"({MAX_TENANT_SKILL_HISTORY_BYTES} bytes)"
            )

    def _publish_version_unlocked(self, skill: SkillVersion) -> SkillVersion:
        source = Path(skill.path).parent
        if self._hash_directory(source) != skill.content_hash:
            raise SkillIntegrityError("Skill changed while being archived")
        target = self._history_path(skill.namespace, skill.name, skill.content_hash)
        if target.exists():
            archived = self._skill_from_history_unlocked(
                skill.namespace,
                skill.name,
                skill.content_hash,
                enabled=skill.enabled,
            )
            if archived.version != skill.version:
                raise SkillIntegrityError("Skill history version metadata mismatch")
            return archived

        if skill.namespace == TENANT_PLUGIN_NAMESPACE:
            self._enforce_tenant_history_quota_unlocked(skill)

        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".archive-", dir=self.staging_dir) as temporary:
            candidate = Path(temporary) / "package"
            shutil.copytree(source, candidate, symlinks=False)
            copied_hash = self._hash_directory(candidate)
            if copied_hash != skill.content_hash:
                raise SkillIntegrityError("Skill changed while being archived")
            metadata = self._frontmatter(candidate / "SKILL.md")
            if metadata["name"] != skill.name:
                raise SkillIntegrityError("Skill package directory/name mismatch")
            os.replace(candidate, target)
            self._make_read_only(target)
        return self._skill_from_history_unlocked(
            skill.namespace,
            skill.name,
            skill.content_hash,
            enabled=skill.enabled,
        )

    def _migrate_legacy_tenant_skills_unlocked(self) -> None:
        """One-way import of the former mutable tenant-plugin/skills layout."""
        current = self._current_versions_unlocked()
        overrides = self._enabled_overrides_unlocked()
        current_changed = False
        enabled_changed = False
        for skill_file in sorted(self.tenant_dir.glob("*/SKILL.md")):
            if skill_file.parent.name.startswith("."):
                continue
            metadata = self._frontmatter(skill_file)
            name = metadata["name"]
            if not SKILL_NAME_PATTERN.fullmatch(name) or skill_file.parent.name != name:
                raise ValueError(f"Invalid legacy tenant Skill package: {skill_file.parent}")
            identifier = f"{TENANT_PLUGIN_NAMESPACE}:{name}"
            if identifier in current:
                # Once migrated, the immutable pointer is authoritative. A stale
                # legacy directory must never overwrite a later reviewed upgrade.
                continue
            skill = SkillVersion(
                id=identifier,
                namespace=TENANT_PLUGIN_NAMESPACE,
                name=name,
                description=metadata["description"],
                version=metadata.get("version", "0.0.0"),
                content_hash=self._hash_directory(skill_file.parent),
                source="tenant",
                enabled=overrides.get(identifier, False),
                path=str(skill_file),
            )
            review = self._test_skill(skill)
            if not review.ok:
                failures = "; ".join(
                    check.detail for check in review.checks if check.status == "failed"
                )
                raise ValueError(f"legacy skill review failed: {failures}")
            archived = self._publish_version_unlocked(skill)
            current[identifier] = archived.content_hash
            current_changed = True
            if identifier not in overrides:
                overrides[identifier] = False
                enabled_changed = True
        if enabled_changed or current_changed:
            self._write_registry_state_unlocked(overrides, current)

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillVersion:
        self._parse_skill_id(skill_id)
        with self._exclusive():
            skill = self._get_unlocked(skill_id)
            if enabled:
                result = self._test_skill(skill)
                if not result.ok:
                    failures = "; ".join(
                        check.detail for check in result.checks if check.status == "failed"
                    )
                    raise ValueError(f"skill review failed: {failures}")
                self._publish_version_unlocked(skill)
            values, current = self._registry_state_unlocked()
            values[skill_id] = enabled
            self._write_registry_state_unlocked(values, current)
            return self._get_unlocked(skill_id)

    def test(self, skill_id: str) -> SkillTestResult:
        """Run deterministic review checks without any model call."""
        return self._test_skill(self.get(skill_id))

    def _test_skill(self, skill: SkillVersion) -> SkillTestResult:
        skill_dir = Path(skill.path).parent
        skill_md = Path(skill.path).read_text(encoding="utf-8")
        header = skill_md.split("---\n", 2)[1] if skill_md.startswith("---\n") else ""
        checks: list[SkillCheck] = []

        try:
            actual_hash = self._hash_directory(skill_dir)
            integrity_detail = (
                "content hash verified"
                if actual_hash == skill.content_hash
                else f"expected {skill.content_hash}, got {actual_hash}"
            )
            integrity_status: Literal["passed", "failed"] = (
                "passed" if actual_hash == skill.content_hash else "failed"
            )
        except ValueError as exc:
            integrity_status = "failed"
            integrity_detail = str(exc)
        checks.append(
            SkillCheck(
                name="package_integrity",
                status=integrity_status,
                detail=integrity_detail,
            )
        )
        checks.append(
            SkillCheck(
                name="frontmatter",
                status="passed" if skill.version != "0.0.0" else "warning",
                detail=(
                    f"name/description present; version {skill.version}"
                    + ("" if skill.version != "0.0.0" else " (version 缺省)")
                ),
            )
        )
        if integrity_status == "failed":
            # Do not traverse an untrusted package after discovering a symlink or
            # hash mismatch. The integrity failure alone blocks enable/rollback.
            return SkillTestResult(skill_id=skill.id, ok=False, checks=checks)

        referenced = sorted(set(REFERENCE_PATTERN.findall(skill_md)))
        invalid_refs: list[str] = []
        missing_refs: list[str] = []
        for reference in referenced:
            relative = Path(reference)
            if relative.is_absolute() or ".." in relative.parts:
                invalid_refs.append(reference)
                continue
            candidate = skill_dir / relative
            try:
                candidate.resolve(strict=False).relative_to(skill_dir.resolve())
            except (OSError, ValueError):
                invalid_refs.append(reference)
                continue
            if not candidate.exists():
                missing_refs.append(reference)
        checks.append(
            SkillCheck(
                name="references",
                status="failed" if invalid_refs or missing_refs else "passed",
                detail=(
                    f"invalid: {invalid_refs}; missing: {missing_refs}"
                    if invalid_refs or missing_refs
                    else f"{len(referenced)} referenced file(s) all present"
                ),
            )
        )

        secret_hits: list[str] = []
        danger_hits: list[str] = []
        oversized: list[str] = []
        for file_path in sorted(item for item in skill_dir.rglob("*") if item.is_file()):
            relative = file_path.relative_to(skill_dir).as_posix()
            if file_path.stat().st_size > MAX_SKILL_FILE_BYTES:
                oversized.append(relative)
            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(pattern.search(content) for pattern in SECRET_PATTERNS):
                secret_hits.append(relative)
            if any(pattern.search(content) for pattern in DANGEROUS_PATTERNS):
                danger_hits.append(relative)
        checks.append(
            SkillCheck(
                name="secret_scan",
                status="failed" if secret_hits else "passed",
                detail=f"credential-like content in: {secret_hits}" if secret_hits else "clean",
            )
        )
        checks.append(
            SkillCheck(
                name="dangerous_commands",
                status="failed" if danger_hits else "passed",
                detail=f"dangerous patterns in: {danger_hits}" if danger_hits else "clean",
            )
        )
        if oversized:
            checks.append(
                SkillCheck(
                    name="file_size",
                    status="warning",
                    detail=f"files over {MAX_SKILL_FILE_BYTES} bytes: {oversized}",
                )
            )

        if re.search(r"(?im)^allowed-tools\s*:", header):
            checks.append(
                SkillCheck(
                    name="permission_frontmatter",
                    status="warning",
                    detail=(
                        "allowed-tools frontmatter 在 SDK 场景不强制执行；"
                        "权限一律以运行时 options/回调为准（PRD §4.7）"
                    ),
                )
            )

        return SkillTestResult(
            skill_id=skill.id,
            ok=all(check.status != "failed" for check in checks),
            checks=checks,
        )

    def runtime_selection(self) -> Literal["all"] | list[str]:
        skills = self.list()
        if skills and all(item.enabled for item in skills):
            return "all"
        return [item.id for item in skills if item.enabled]

    def snapshot(self) -> list[dict[str, object]]:
        """Capture enabled packages in immutable history before returning the pins."""
        with self._exclusive():
            archived = [
                self._publish_version_unlocked(item)
                for item in self._list_unlocked()
                if item.enabled
            ]
            return [
                {
                    "id": item.id,
                    "version": item.version,
                    "content_hash": item.content_hash,
                    "enabled": True,
                }
                for item in archived
            ]

    def _normalize_snapshot_unlocked(
        self, snapshot: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        normalized: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in snapshot:
            if not isinstance(raw, dict):
                raise ValueError("Skill snapshot entries must be objects")
            identifier = raw.get("id")
            version = raw.get("version")
            content_hash = raw.get("content_hash")
            if not isinstance(identifier, str) or not isinstance(version, str):
                raise ValueError("Skill snapshot requires string id and version")
            if (
                not isinstance(content_hash, str)
                or not CONTENT_HASH_PATTERN.fullmatch(content_hash)
            ):
                raise ValueError("Skill snapshot requires a lowercase SHA-256 content_hash")
            if raw.get("enabled") is not True:
                raise ValueError("Skill snapshot may contain only explicitly enabled Skills")
            namespace, name = self._parse_skill_id(identifier)
            if namespace not in {self._namespace(), TENANT_PLUGIN_NAMESPACE}:
                raise ValueError(f"Unknown managed Skill namespace: {namespace}")
            if identifier in seen:
                raise ValueError(f"Duplicate Skill snapshot entry: {identifier}")
            seen.add(identifier)
            archived = self._skill_from_history_unlocked(
                namespace,
                name,
                content_hash,
                enabled=True,
            )
            if archived.version != version:
                raise ValueError(
                    f"Skill snapshot version mismatch for {identifier}: "
                    f"expected {version}, got {archived.version}"
                )
            normalized.append(
                {
                    "id": identifier,
                    "version": version,
                    "content_hash": content_hash,
                    "enabled": True,
                }
            )
        return sorted(normalized, key=lambda item: str(item["id"]))

    def _validate_runtime_unlocked(
        self,
        root: Path,
        normalized: list[dict[str, object]],
    ) -> list[dict[str, str]]:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Materialized Skill runtime is not a real directory")
        by_namespace: dict[str, list[dict[str, object]]] = {}
        for item in normalized:
            namespace, _name = self._parse_skill_id(str(item["id"]))
            by_namespace.setdefault(namespace, []).append(item)
        actual_namespaces = {item.name for item in root.iterdir()}
        if actual_namespaces != set(by_namespace):
            raise ValueError("Materialized Skill runtime contains unexpected Plugin roots")
        paths: list[dict[str, str]] = []
        for namespace, entries in sorted(by_namespace.items()):
            plugin = root / namespace
            if plugin.is_symlink() or not plugin.is_dir():
                raise ValueError(f"Materialized Plugin is invalid: {namespace}")
            try:
                manifest = json.loads(
                    (plugin / ".claude-plugin" / "plugin.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"Materialized Plugin manifest is invalid: {namespace}"
                ) from exc
            if not isinstance(manifest, dict) or manifest.get("name") != namespace:
                raise ValueError(f"Materialized Plugin namespace mismatch: {namespace}")
            expected_names = {
                self._parse_skill_id(str(item["id"]))[1] for item in entries
            }
            skills_root = plugin / "skills"
            if skills_root.is_symlink() or not skills_root.is_dir():
                raise ValueError(
                    f"Materialized Plugin has no safe skills directory: {namespace}"
                )
            actual_names = {item.name for item in skills_root.iterdir()}
            if actual_names != expected_names:
                raise ValueError(
                    f"Materialized Plugin contains unexpected Skills: {namespace}"
                )
            for item in entries:
                name = self._parse_skill_id(str(item["id"]))[1]
                actual_hash = self._hash_directory(skills_root / name)
                if actual_hash != item["content_hash"]:
                    raise ValueError(
                        f"Materialized Skill hash mismatch: {namespace}:{name}"
                    )
            paths.append({"type": "local", "path": str(plugin)})
        return paths

    def _runtime_cache_entries_unlocked(self) -> list[Path]:
        entries: list[Path] = []
        for entry in self.runtime_dir.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                raise ValueError("Skill runtime cache contains an invalid entry")
            if not CONTENT_HASH_PATTERN.fullmatch(entry.name):
                raise ValueError("Skill runtime cache contains an invalid digest path")
            entries.append(entry)
        return entries

    def _runtime_lease_path(self, directory: Path) -> Path:
        return self.runtime_lease_dir / f"{directory.name}.lock"

    def _ensure_runtime_lease_unlocked(self, directory: Path) -> Path:
        lease_path = self._runtime_lease_path(directory)
        if lease_path.is_symlink():
            raise ValueError("Skill runtime lease must not be a symbolic link")
        if not lease_path.exists():
            lease_path.touch(mode=0o600, exist_ok=False)
        if not lease_path.is_file():
            raise ValueError("Skill runtime lease must be a regular file")
        return lease_path

    def _acquire_runtime_lease_unlocked(self, directory: Path) -> IO[bytes]:
        lease_path = self._ensure_runtime_lease_unlocked(directory)
        handle = lease_path.open("rb")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        except BaseException:
            handle.close()
            raise
        return handle

    def _remove_runtime_cache_unlocked(self, directory: Path) -> bool:
        """Remove only a verified direct cache child; immutable history is untouched."""
        if directory.parent != self.runtime_dir or not CONTENT_HASH_PATTERN.fullmatch(
            directory.name
        ):
            raise ValueError("Refusing to remove a path outside the Skill runtime cache")
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Refusing to remove an invalid Skill runtime cache entry")
        lease_path = self._ensure_runtime_lease_unlocked(directory)
        with lease_path.open("rb") as lease:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            paths = list(directory.rglob("*"))
            for path in paths:
                if path.is_symlink():
                    raise ValueError("Refusing to remove a symlinked Skill runtime cache")
                if not path.is_dir() and not path.is_file():
                    raise ValueError("Refusing to remove a special cache file")
            # Cache trees are sealed read-only. Restore owner write permission only
            # after fully validating the removal boundary, then remove under flock.
            directory.chmod(0o700)
            for path in paths:
                path.chmod(0o700 if path.is_dir() else 0o600)
            shutil.rmtree(directory)
            lease_path.unlink()
            return True

    def _prune_runtime_cache_unlocked(
        self,
        target_count: int,
        *,
        target_bytes: int,
        protected: set[Path],
    ) -> None:
        if target_count < 1:
            raise ValueError("Skill runtime cache limit must leave at least one entry")
        if target_bytes < 1:
            raise ValueError("Skill runtime cache byte limit must be positive")
        entries = self._runtime_cache_entries_unlocked()
        sizes = {entry: self._allocated_directory_size(entry) for entry in entries}
        candidates = sorted(
            (entry for entry in entries if entry not in protected),
            key=lambda entry: (entry.stat().st_mtime_ns, entry.name),
        )
        remaining = len(entries)
        remaining_bytes = sum(sizes.values())
        for candidate in candidates:
            if remaining <= target_count and remaining_bytes <= target_bytes:
                break
            if self._remove_runtime_cache_unlocked(candidate):
                remaining -= 1
                remaining_bytes -= sizes[candidate]
        if remaining > target_count or remaining_bytes > target_bytes:
            raise ValueError(
                "Skill runtime cache cannot satisfy count/byte limits without "
                "evicting active targets"
            )

    def materialize_snapshot(
        self,
        snapshot: list[dict[str, object]],
        *,
        acquire_lease: bool = False,
    ) -> SkillRuntimeView:
        """Build read-only Plugin roots containing only the pinned snapshot content.

        SDK sessions set ``acquire_lease`` and close the returned view when their
        session ends. The shared flock then prevents another worker's cache GC
        from deleting a Plugin root while the SDK may still lazily read it.
        """
        if not snapshot:
            return SkillRuntimeView(plugin_paths=[], skill_ids=[])
        with self._exclusive():
            if MAX_RUNTIME_SKILL_CACHE_ENTRIES < 2:
                raise ValueError("Skill runtime cache must allow at least two entries")
            normalized = self._normalize_snapshot_unlocked(snapshot)
            canonical = json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            snapshot_hash = hashlib.sha256(canonical).hexdigest()
            target = self.runtime_dir / snapshot_hash
            if target.is_symlink():
                raise ValueError("Materialized Skill runtime path is a symbolic link")
            if target.exists():
                plugin_paths = self._validate_runtime_unlocked(target, normalized)
                os.utime(target)
                self._prune_runtime_cache_unlocked(
                    MAX_RUNTIME_SKILL_CACHE_ENTRIES,
                    target_bytes=MAX_RUNTIME_SKILL_CACHE_BYTES,
                    protected={target},
                )
                return SkillRuntimeView(
                    plugin_paths=plugin_paths,
                    skill_ids=[str(item["id"]) for item in normalized],
                    _lease_handle=(
                        self._acquire_runtime_lease_unlocked(target)
                        if acquire_lease
                        else None
                    ),
                )

            self._prune_runtime_cache_unlocked(
                MAX_RUNTIME_SKILL_CACHE_ENTRIES - 1,
                target_bytes=MAX_RUNTIME_SKILL_CACHE_BYTES,
                protected=set(),
            )
            by_namespace: dict[str, list[dict[str, object]]] = {}
            for item in normalized:
                namespace, _name = self._parse_skill_id(str(item["id"]))
                by_namespace.setdefault(namespace, []).append(item)
            with tempfile.TemporaryDirectory(
                prefix=".runtime-", dir=self.staging_dir
            ) as temporary:
                candidate = Path(temporary) / "plugins"
                candidate.mkdir()
                for namespace, entries in by_namespace.items():
                    plugin = candidate / namespace
                    manifest_dir = plugin / ".claude-plugin"
                    skills_root = plugin / "skills"
                    manifest_dir.mkdir(parents=True)
                    skills_root.mkdir()
                    (manifest_dir / "plugin.json").write_text(
                        json.dumps(
                            {
                                "name": namespace,
                                "version": "1.0.0",
                                "description": "Immutable OPHIRA run Skill snapshot",
                            },
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    for item in entries:
                        _namespace, name = self._parse_skill_id(str(item["id"]))
                        source = self._history_path(
                            namespace, name, str(item["content_hash"])
                        )
                        shutil.copytree(source, skills_root / name, symlinks=False)
                self._validate_runtime_unlocked(candidate, normalized)
                self._make_read_only(candidate)
                # macOS requires write permission on a moved directory because its
                # parent entry changes. Keep children sealed, rename atomically,
                # then seal the published root immediately under the registry lock.
                candidate.chmod(0o755)
                os.replace(candidate, target)
                target.chmod(0o555)
            try:
                self._prune_runtime_cache_unlocked(
                    MAX_RUNTIME_SKILL_CACHE_ENTRIES,
                    target_bytes=MAX_RUNTIME_SKILL_CACHE_BYTES,
                    protected={target},
                )
            except ValueError:
                # This target was published under the exclusive registry lock
                # and has not been leased yet, so it is safe to roll back the
                # reproducible cache entry when it cannot fit the hard budget.
                self._remove_runtime_cache_unlocked(target)
                raise
            plugin_paths = self._validate_runtime_unlocked(target, normalized)
            return SkillRuntimeView(
                plugin_paths=plugin_paths,
                skill_ids=[str(item["id"]) for item in normalized],
                _lease_handle=(
                    self._acquire_runtime_lease_unlocked(target)
                    if acquire_lease
                    else None
                ),
            )

    def runtime_snapshot_lease(
        self, snapshot: list[dict[str, object]]
    ) -> SkillRuntimeView:
        """Return a context-managed materialization protected for an SDK session."""
        return self.materialize_snapshot(snapshot, acquire_lease=True)

    def plugin_paths(self) -> list[dict[str, str]]:
        """Materialize the current enabled set for SDK preflight compatibility."""
        return self.materialize_snapshot(self.snapshot()).plugin_paths

    def versions(self, skill_id: str) -> list[SkillVersion]:
        namespace, name = self._parse_skill_id(skill_id)
        with self._exclusive():
            if namespace not in {self._namespace(), TENANT_PLUGIN_NAMESPACE}:
                raise SkillNotFoundError(skill_id)
            root = self.versions_dir / namespace / name
            overrides = self._enabled_overrides_unlocked()
            if namespace == TENANT_PLUGIN_NAMESPACE:
                current = self._current_versions_unlocked().get(skill_id)
                current_enabled = overrides.get(skill_id, False)
            else:
                live = next(
                    (
                        skill
                        for skill in self._collect_application_unlocked(overrides)
                        if skill.id == skill_id
                    ),
                    None,
                )
                current = live.content_hash if live is not None else None
                current_enabled = live.enabled if live is not None else False
            if root.is_symlink():
                raise SkillIntegrityError("Skill version history path is a symbolic link")
            if not root.exists():
                raise SkillNotFoundError(skill_id)
            try:
                history_paths = sorted(root.iterdir())
            except OSError as exc:
                raise SkillIntegrityError("Skill version history cannot be read") from exc
            versions: list[SkillVersion] = []
            for path in history_paths:
                if (
                    path.is_symlink()
                    or not path.is_dir()
                    or not CONTENT_HASH_PATTERN.fullmatch(path.name)
                ):
                    raise SkillIntegrityError(
                        "Skill version history contains an invalid entry"
                    )
                versions.append(
                    self._skill_from_history_unlocked(
                        namespace,
                        name,
                        path.name,
                        enabled=(
                            path.name == current and current_enabled
                        ),
                    )
                )
            if not versions:
                raise SkillNotFoundError(skill_id)
            return sorted(versions, key=lambda item: (item.version, item.content_hash))

    def rollback(
        self,
        skill_id: str,
        *,
        version: str | None = None,
        content_hash: str | None = None,
    ) -> SkillVersion:
        """Atomically select a reviewed immutable tenant version as current."""
        if (version is None) == (content_hash is None):
            raise SkillConflictError("Specify exactly one of version or content_hash")
        namespace, name = self._parse_skill_id(skill_id)
        if namespace != TENANT_PLUGIN_NAMESPACE:
            raise SkillConflictError("Only tenant Skills can be rolled back")
        with self._exclusive():
            root = self.versions_dir / namespace / name
            try:
                root_is_symlink = root.is_symlink()
                root_is_directory = root.is_dir()
            except OSError as exc:
                raise SkillIntegrityError("Skill version history cannot be read") from exc
            if root_is_symlink:
                raise SkillIntegrityError("Skill version history path is a symbolic link")
            if not root_is_directory:
                raise SkillNotFoundError(skill_id)
            if content_hash is not None:
                candidates = [
                    self._skill_from_history_unlocked(
                        namespace, name, content_hash, enabled=False
                    )
                ]
            else:
                candidates = []
                try:
                    history_paths = sorted(root.iterdir())
                except OSError as exc:
                    raise SkillIntegrityError(
                        "Skill version history cannot be read"
                    ) from exc
                for path in history_paths:
                    if (
                        path.is_symlink()
                        or not path.is_dir()
                        or not CONTENT_HASH_PATTERN.fullmatch(path.name)
                    ):
                        raise SkillIntegrityError(
                            "Skill version history contains an invalid entry"
                        )
                    candidate = self._skill_from_history_unlocked(
                        namespace, name, path.name, enabled=False
                    )
                    if candidate.version == version:
                        candidates.append(candidate)
                if not candidates:
                    raise SkillNotFoundError(f"{skill_id}@{version}")
                if len(candidates) > 1:
                    raise SkillConflictError(
                        f"Multiple immutable packages use version {version}; "
                        "select content_hash"
                    )
            selected = candidates[0]
            review = self._test_skill(selected)
            if not review.ok:
                failures = "; ".join(
                    check.detail for check in review.checks if check.status == "failed"
                )
                raise SkillConflictError(f"skill review failed: {failures}")
            enabled, current = self._registry_state_unlocked()
            current[skill_id] = selected.content_hash
            self._write_registry_state_unlocked(enabled, current)
            return self._get_unlocked(skill_id)

    def install_zip(self, payload: bytes) -> SkillVersion:
        if not payload:
            raise ValueError("skill archive is empty")
        if len(payload) > MAX_SKILL_ARCHIVE_BYTES:
            raise ValueError(f"skill archive exceeds {MAX_SKILL_ARCHIVE_BYTES} bytes")
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise ValueError("skill archive is not a valid ZIP file") from exc

        with archive:
            entries = archive.infolist()
            if len(entries) > MAX_SKILL_ARCHIVE_ENTRIES:
                raise ValueError(
                    f"skill archive exceeds {MAX_SKILL_ARCHIVE_ENTRIES} entries"
                )
            if sum(info.file_size for info in entries) > MAX_SKILL_UNPACKED_BYTES:
                raise ValueError(
                    f"skill archive expands beyond {MAX_SKILL_UNPACKED_BYTES} bytes"
                )

            paths: dict[str, Path] = {}
            normalized_names: set[str] = set()
            entry_is_directory: dict[str, bool] = {}
            for info in entries:
                unix_mode = info.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if info.flag_bits & 0x1:
                    raise ValueError("encrypted skill archives are not supported")
                if stat.S_ISLNK(unix_mode):
                    raise ValueError("skill archive contains symbolic links")
                if file_type and not (
                    stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)
                ):
                    raise ValueError("skill archive contains special files")
                if "\\" in info.filename or "\x00" in info.filename:
                    raise ValueError("skill archive contains unsafe paths")
                path = Path(info.filename)
                if (
                    not info.filename
                    or path.is_absolute()
                    or ".." in path.parts
                    or (path.parts and path.parts[0].endswith(":"))
                    or any(part in {"", "."} for part in path.parts)
                ):
                    raise ValueError("skill archive contains unsafe paths")
                normalized = path.as_posix().rstrip("/").casefold()
                if normalized in normalized_names:
                    raise ValueError("skill archive contains duplicate paths")
                normalized_names.add(normalized)
                entry_is_directory[normalized] = info.is_dir()
                paths[info.filename] = path
            for normalized in normalized_names:
                parts = normalized.split("/")
                for index in range(1, len(parts)):
                    parent = "/".join(parts[:index])
                    if parent in entry_is_directory and not entry_is_directory[parent]:
                        raise ValueError("skill archive contains conflicting file paths")

            skill_entries = [
                info
                for info in entries
                if not info.is_dir() and paths[info.filename].name == "SKILL.md"
            ]
            if len(skill_entries) != 1:
                raise ValueError("skill archive must contain exactly one SKILL.md")
            archive_root = paths[skill_entries[0].filename].parent

            with self._exclusive(), tempfile.TemporaryDirectory(
                prefix=".install-", dir=self.staging_dir
            ) as temporary_dir:
                candidate = Path(temporary_dir) / "candidate"
                candidate.mkdir()
                unpacked_bytes = 0
                for info in entries:
                    if info.is_dir():
                        continue
                    path = paths[info.filename]
                    try:
                        relative = path.relative_to(archive_root)
                    except ValueError as exc:
                        raise ValueError(
                            "all skill files must share the SKILL.md package root"
                        ) from exc
                    destination = candidate / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("xb") as output:
                        while chunk := source.read(64 * 1024):
                            unpacked_bytes += len(chunk)
                            if unpacked_bytes > MAX_SKILL_UNPACKED_BYTES:
                                raise ValueError(
                                    "skill archive exceeded its declared unpacked size"
                                )
                            output.write(chunk)

                skill_path = candidate / "SKILL.md"
                if not skill_path.exists():
                    raise ValueError("extracted archive is missing SKILL.md")
                metadata = self._frontmatter(skill_path)
                name = metadata["name"]
                if not SKILL_NAME_PATTERN.fullmatch(name):
                    raise ValueError(
                        "skill name must contain only lowercase letters, digits, '-' or '_'"
                    )
                identifier = f"{TENANT_PLUGIN_NAMESPACE}:{name}"
                staged = SkillVersion(
                    id=identifier,
                    namespace=TENANT_PLUGIN_NAMESPACE,
                    name=name,
                    description=metadata["description"],
                    version=metadata.get("version", "0.0.0"),
                    content_hash=self._hash_directory(candidate),
                    source="tenant",
                    enabled=False,
                    path=str(skill_path),
                )
                review = self._test_skill(staged)
                if not review.ok:
                    failures = "; ".join(
                        check.detail for check in review.checks if check.status == "failed"
                    )
                    raise ValueError(f"skill review failed: {failures}")

                archived = self._publish_version_unlocked(staged)
                # Every new supply-chain revision requires an explicit enable action.
                enabled, current = self._registry_state_unlocked()
                enabled[identifier] = False
                current[identifier] = archived.content_hash
                self._write_registry_state_unlocked(enabled, current)
                return self._get_unlocked(identifier)
