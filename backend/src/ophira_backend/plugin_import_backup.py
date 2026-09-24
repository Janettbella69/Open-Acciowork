"""Validate plugin import reports against frozen Skill imports in cold copies."""

from __future__ import annotations

import hashlib
import os
import re

from .backup_files import BackupError, parse_json
from .plugin_imports import MAX_PLUGIN_IMPORTS, PLUGIN_IMPORT_ID, PluginImportRecord
from .skill_import_models import SkillRepositoryScope
from .skill_proposal_files import directory_fd, read_relative
from .skill_repository import digest


def inspect_plugin_imports(ledger, skill_imports: dict) -> dict:
    root = ledger.root / "plugin-imports"
    if not root.exists():
        return {}
    try:
        with directory_fd(root) as descriptor:
            names = os.listdir(descriptor)
        if any(name.startswith(".plugin-inspect-") for name in names):
            raise BackupError("pending_plugin_import_staging")
        if len(names) > MAX_PLUGIN_IMPORTS:
            raise ValueError("Plugin import capacity exceeded")
        scope_file = ledger.root / "skill-imports" / "scope.json"
        scope = (
            SkillRepositoryScope.model_validate(
                parse_json(read_relative(scope_file.parent, scope_file.name, 2048))
            )
            if scope_file.exists()
            else None
        )
        records = {}
        referenced = set()
        for name in sorted(names):
            identifier = name.removesuffix(".json")
            if not name.endswith(".json") or not re.fullmatch(PLUGIN_IMPORT_ID, identifier):
                raise ValueError("Unexpected plugin import entry")
            raw = read_relative(root, name, 64 * 1024)
            parse_json(raw)
            record = PluginImportRecord.model_validate_json(raw)
            expected_id = "plugin_import_" + digest([
                record.scope.model_dump(), record.uploaded_by.kind,
                record.uploaded_by.id, record.request_id,
            ])
            if record.id != identifier or record.id != expected_id:
                raise ValueError("Plugin import identity mismatch")
            if scope is None:
                scope = record.scope
            elif record.scope != scope:
                raise ValueError("Plugin import scope mismatch")
            for skill in record.skills:
                if skill.import_id in referenced:
                    raise ValueError("Plugin Skill import referenced more than once")
                referenced.add(skill.import_id)
                child = skill_imports[skill.import_id]
                child_request = hashlib.sha256(
                    f"{record.id}:{skill.path}".encode()
                ).hexdigest()
                if (
                    child.request_id != child_request
                    or child.scope != record.scope
                    or child.uploaded_by != record.uploaded_by
                    or child.skill.id != skill.skill_id
                    or child.skill.content_hash != skill.content_hash
                    or child.scan.ok != skill.scan_ok
                ):
                    raise ValueError("Plugin Skill import binding mismatch")
            records[record.id] = record
        return records
    except BackupError:
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise BackupError("plugin_import_invalid") from exc
