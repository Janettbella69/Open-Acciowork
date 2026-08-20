from __future__ import annotations

import hashlib
import json
import os
import re
import threading
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
    re.compile(r"chmod\s+777"),
)
REFERENCE_PATTERN = re.compile(r"references/[A-Za-z0-9._/-]+")
MAX_SKILL_FILE_BYTES = 200_000


class SkillRegistry:
    """Discovers versioned filesystem Skills from the application-managed Plugin."""

    def __init__(self, plugin_dir: Path, state_dir: Path) -> None:
        self.plugin_dir = plugin_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
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
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            raise ValueError(f"Skill is missing YAML frontmatter: {path}")
        try:
            header = content.split("---\n", 2)[1]
        except IndexError as exc:
            raise ValueError(f"Skill frontmatter is not closed: {path}") from exc
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
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
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

    def list(self) -> list[SkillVersion]:
        namespace = self._namespace()
        overrides = self._enabled_overrides()
        skills: list[SkillVersion] = []
        for skill_file in sorted((self.plugin_dir / "skills").glob("*/SKILL.md")):
            metadata = self._frontmatter(skill_file)
            name = metadata["name"]
            identifier = f"{namespace}:{name}"
            skills.append(
                SkillVersion(
                    id=identifier,
                    namespace=namespace,
                    name=name,
                    description=metadata["description"],
                    version=metadata.get("version", "0.0.0"),
                    content_hash=self._hash_directory(skill_file.parent),
                    enabled=overrides.get(identifier, True),
                    path=str(skill_file),
                )
            )
        return skills

    def get(self, skill_id: str) -> SkillVersion:
        skill = next((item for item in self.list() if item.id == skill_id), None)
        if skill is None:
            raise SkillNotFoundError(skill_id)
        return skill

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillVersion:
        self.get(skill_id)
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
        skill = self.get(skill_id)
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
