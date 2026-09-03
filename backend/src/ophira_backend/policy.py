"""Organization policy (FR-22): permission mode, provider/skill allowlists."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .models import OrgPolicy, utc_now


class PolicyDeniedError(PermissionError):
    pass


class OrgPolicyStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def get(self) -> OrgPolicy:
        if not self.path.exists():
            return OrgPolicy()
        try:
            return OrgPolicy.model_validate_json(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return OrgPolicy()

    def save(self, policy: OrgPolicy) -> OrgPolicy:
        updated = policy.model_copy(update={"updated_at": utc_now()})
        with self._lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(updated.model_dump_json(indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        return updated

    def allows_provider(self, provider_id: str | None) -> bool:
        policy = self.get()
        if not policy.allowed_provider_ids:
            return True
        return bool(provider_id) and provider_id in policy.allowed_provider_ids

    def filter_skill_ids(self, skill_ids: list[str]) -> list[str]:
        policy = self.get()
        if not policy.allowed_skill_ids:
            return skill_ids
        allowed = set(policy.allowed_skill_ids)
        return [item for item in skill_ids if item in allowed]
