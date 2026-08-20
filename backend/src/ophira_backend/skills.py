from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Literal

from .models import SkillVersion


class SkillNotFoundError(KeyError):
    pass


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
