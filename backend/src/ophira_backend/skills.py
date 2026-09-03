from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Literal

from .models import SkillCheck, SkillTestResult, SkillVersion


class SkillNotFoundError(KeyError):
    pass


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
TENANT_PLUGIN_NAMESPACE = "ophira-tenant"
SKILL_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class SkillRegistry:
    """Discovers versioned filesystem Skills from the application-managed Plugin."""

    def __init__(self, plugin_dir: Path, state_dir: Path) -> None:
        self.plugin_dir = plugin_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tenant_plugin_dir = self.state_dir / "tenant-plugin"
        self.tenant_dir = self.tenant_plugin_dir / "skills"
        self.tenant_dir.mkdir(parents=True, exist_ok=True)
        manifest_dir = self.tenant_plugin_dir / ".claude-plugin"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = manifest_dir / "plugin.json"
        if not manifest.exists():
            manifest.write_text(
                json.dumps(
                    {
                        "name": TENANT_PLUGIN_NAMESPACE,
                        "version": "1.0.0",
                        "description": "Tenant-reviewed OPHIRA Skills",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        self.state_path = self.state_dir / "enabled.json"
        self._lock = threading.RLock()

    def _namespace(self) -> str:
        manifest = self.plugin_dir / ".claude-plugin" / "plugin.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            value = str(data["name"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid OPHIRA Plugin manifest: {manifest}") from exc
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
            raise ValueError("Plugin name must be a lowercase Claude Code namespace")
        return value

    @staticmethod
    def _frontmatter(path: Path) -> dict[str, str]:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Skill must be readable UTF-8 text: {path}") from exc
        if not content.startswith("---\n"):
            raise ValueError(f"Skill is missing YAML frontmatter: {path}")
        sections = content.split("---\n", 2)
        if len(sections) != 3:
            raise ValueError(f"Skill frontmatter is not closed: {path}")
        header = sections[1]
        values: dict[str, str] = {}
        for line in header.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"name", "description", "version"}:
                values[key.strip()] = value.strip().strip('"\'')
        if not values.get("name") or not values.get("description"):
            raise ValueError(f"Skill requires name and description: {path}")
        return values

    @staticmethod
    def _hash_directory(directory: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Skill package contains a symbolic link: {path}")
            if not path.is_file():
                continue
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _enabled_overrides(self) -> dict[str, bool]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(key): bool(value) for key, value in data.items()}

    def _collect(
        self,
        skills_root: Path,
        namespace: str,
        source: Literal["application", "tenant"],
        overrides: dict[str, bool],
        *,
        default_enabled: bool,
    ) -> list[SkillVersion]:
        skills: list[SkillVersion] = []
        if not skills_root.exists():
            return skills
        for skill_file in sorted(skills_root.glob("*/SKILL.md")):
            metadata = self._frontmatter(skill_file)
            name = metadata["name"]
            if not SKILL_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"Invalid Skill name in frontmatter: {name}")
            identifier = f"{namespace}:{name}"
            skills.append(
                SkillVersion(
                    id=identifier,
                    namespace=namespace,
                    name=name,
                    description=metadata["description"],
                    version=metadata.get("version", "0.0.0"),
                    content_hash=self._hash_directory(skill_file.parent),
                    source=source,
                    enabled=overrides.get(identifier, default_enabled),
                    path=str(skill_file),
                )
            )
        return skills

    def list(self) -> list[SkillVersion]:
        namespace = self._namespace()
        overrides = self._enabled_overrides()
        skills = self._collect(
            self.plugin_dir / "skills",
            namespace,
            "application",
            overrides,
            default_enabled=True,
        )
        skills.extend(
            self._collect(
                self.tenant_dir,
                TENANT_PLUGIN_NAMESPACE,
                "tenant",
                overrides,
                default_enabled=False,
            )
        )
        return skills

    def plugin_paths(self) -> list[dict[str, str]]:
        """Return every explicitly managed Plugin root for SDK isolation mode."""
        return [
            {"type": "local", "path": str(self.plugin_dir)},
            {"type": "local", "path": str(self.tenant_plugin_dir)},
        ]

    def get(self, skill_id: str) -> SkillVersion:
        skill = next((item for item in self.list() if item.id == skill_id), None)
        if skill is None:
            raise SkillNotFoundError(skill_id)
        return skill

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillVersion:
        skill = self.get(skill_id)
        if enabled:
            result = self._test_skill(skill)
            if not result.ok:
                failures = "; ".join(
                    check.detail for check in result.checks if check.status == "failed"
                )
                raise ValueError(f"skill review failed: {failures}")
        with self._lock:
            values = self._enabled_overrides()
            values[skill_id] = enabled
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(values, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        return self.get(skill_id)

    def test(self, skill_id: str) -> SkillTestResult:
        """Deterministic package test (§3.6 'test' entry / §4.7 Review-step scan):
        frontmatter shape, referenced files, secret and dangerous-command scan,
        ineffective permission frontmatter, oversized files. No model calls."""
        return self._test_skill(self.get(skill_id))

    def _test_skill(self, skill: SkillVersion) -> SkillTestResult:
        skill_dir = Path(skill.path).parent
        skill_md = Path(skill.path).read_text(encoding="utf-8")
        header = skill_md.split("---\n", 2)[1] if skill_md.startswith("---\n") else ""
        checks: list[SkillCheck] = []

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

        referenced = sorted(set(REFERENCE_PATTERN.findall(skill_md)))
        missing_refs = [ref for ref in referenced if not (skill_dir / ref).exists()]
        checks.append(
            SkillCheck(
                name="references",
                status="failed" if missing_refs else "passed",
                detail=(
                    f"missing: {missing_refs}"
                    if missing_refs
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
        return [
            {
                "id": item.id,
                "version": item.version,
                "content_hash": item.content_hash,
                "enabled": item.enabled,
            }
            for item in self.list()
            if item.enabled
        ]

    def install_zip(self, payload: bytes) -> SkillVersion:
        if not payload:
            raise ValueError("skill archive is empty")
        if len(payload) > MAX_SKILL_ARCHIVE_BYTES:
            raise ValueError(
                f"skill archive exceeds {MAX_SKILL_ARCHIVE_BYTES} bytes"
            )
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
            for info in entries:
                if info.flag_bits & 0x1:
                    raise ValueError("encrypted skill archives are not supported")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("skill archive contains symbolic links")
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
                paths[info.filename] = path

            skill_entries = [
                info
                for info in entries
                if not info.is_dir() and paths[info.filename].name == "SKILL.md"
            ]
            if len(skill_entries) != 1:
                raise ValueError("skill archive must contain exactly one SKILL.md")
            archive_root = paths[skill_entries[0].filename].parent

            with self._lock, tempfile.TemporaryDirectory(
                prefix=".install-", dir=self.tenant_dir
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
                    with archive.open(info) as source, destination.open("wb") as output:
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
                        check.detail
                        for check in review.checks
                        if check.status == "failed"
                    )
                    raise ValueError(f"skill review failed: {failures}")

                target = self.tenant_dir / name
                if target.exists():
                    raise ValueError(f"skill already exists: {identifier}")
                os.replace(candidate, target)

        return self.get(identifier)
