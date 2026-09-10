"""Verify human Skill imports and their canonical publication receipts in cold copies."""

from .backup_files import BackupError, parse_json
from .skill_import_models import SkillRepositoryScope
from .skill_proposal_files import read_relative
from .skill_repository import SkillRepository
from .skills import SkillIntegrityError
from .storage import LedgerNotFoundError


def inspect_skill_imports(ledger, registry):
    root = ledger.root / "skill-imports"
    if not root.exists() or not any(root.iterdir()):
        return {}
    try:
        scope = SkillRepositoryScope.model_validate(
            parse_json(read_relative(root, "scope.json", 2048))
        )
        store = SkillRepository(ledger, registry, scope=scope)
        if any(path.name.startswith(".import-") for path in root.iterdir()):
            raise BackupError("pending_skill_import_staging")
        records = {}
        for identifier in store.identifiers():
            item = store.load(identifier)
            store.verified_files(item)
            skill, _scan = registry.inspect_candidate(store.path(identifier) / "package")
            if skill.model_dump(exclude={"path"}) != item.skill.model_dump():
                raise ValueError("Private Skill import metadata differs from its package")
            if item.state == "publishing":
                # A normal authorized read reconciles the durable receipt. Cold
                # copy inspection does not invent or retry a review decision.
                raise BackupError("pending_skill_import_publication")
            receipt = registry.publication_receipt(identifier)
            if receipt != item.publication_receipt:
                raise ValueError("Private Skill import receipt is missing or mismatched")
            records[identifier] = item
        return records
    except BackupError:
        raise
    except (OSError, ValueError, SkillIntegrityError, LedgerNotFoundError) as exc:
        raise BackupError("private_skill_import_invalid") from exc
