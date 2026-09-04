"""User feedback loop (P2 spec): conclusions, evidence issues, failed runs."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from .models import FeedbackCreate, FeedbackItem, PrincipalContext


class FeedbackNotFoundError(KeyError):
    pass


class FeedbackStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or value in {".", ".."}:
            raise ValueError("invalid feedback id")
        return value

    def _path(self, item_id: str) -> Path:
        return self.root / f"{self._safe_id(item_id)}.json"

    def add(
        self,
        payload: FeedbackCreate,
        *,
        principal: PrincipalContext | None = None,
    ) -> FeedbackItem:
        identity = (
            {
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "principal_role": principal.role,
                "principal_session_id": principal.session_id,
                "identity_binding": "authenticated_principal",
            }
            if principal is not None
            else {}
        )
        item = FeedbackItem(**payload.model_dump(), **identity)
        with self._lock:
            path = self._path(item.id)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(item.model_dump_json(indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        return item

    def get(self, feedback_id: str) -> FeedbackItem:
        path = self._path(feedback_id)
        if not path.exists():
            raise FeedbackNotFoundError(feedback_id)
        return FeedbackItem.model_validate_json(path.read_text("utf-8"))

    def list(self, *, limit: int = 200) -> list[FeedbackItem]:
        items: list[FeedbackItem] = []
        for path in self.root.glob("*.json"):
            try:
                items.append(FeedbackItem.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:limit]
