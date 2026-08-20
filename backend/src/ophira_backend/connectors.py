"""Minimal FR-21 groundwork: a file-backed connector registry with credential-presence
health checks. Real integrations (Gmail, store backends, IM) arrive in Phase 1; this
registry only makes their configuration state observable — it never fakes an integration
and never stores a raw credential (only the env-var name that should hold it)."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path

from .models import Connector, utc_now


class ConnectorNotFoundError(KeyError):
    pass


class ConnectorRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or value in {".", ".."}:
            raise ValueError("invalid connector id")
        return value

    def _path(self, connector_id: str) -> Path:
        return self.root / f"{self._safe_id(connector_id)}.json"

    def _write(self, connector: Connector) -> None:
        path = self._path(connector.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(connector.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def list(self) -> list[Connector]:
        connectors: list[Connector] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                connectors.append(Connector.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        return connectors

    def get(self, connector_id: str) -> Connector:
        path = self._path(connector_id)
        if not path.exists():
            raise ConnectorNotFoundError(connector_id)
        return Connector.model_validate_json(path.read_text("utf-8"))

    def upsert(
        self,
        connector_id: str,
        *,
        type: str,
        display_name: str,
        scope: str = "",
        secret_env: str | None = None,
        endpoint: str | None = None,
    ) -> Connector:
        connector = Connector(
            id=self._safe_id(connector_id),
            type=type,
            display_name=display_name,
            scope=scope,
            secret_env=secret_env,
            endpoint=endpoint,
        )
        with self._lock:
            self._write(connector)
        return connector

    def health_check(
        self,
        connector_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> Connector:
        """Deterministic offline check: is the referenced credential present?
        (Endpoint reachability probing arrives with the real Phase-1 integrations.)"""
        env = os.environ if environ is None else environ
        connector = self.get(connector_id)
        if connector.secret_env and not env.get(connector.secret_env):
            health = "no_credential"
        else:
            health = "ok"
        updated = connector.model_copy(
            update={"health": health, "last_checked_at": utc_now()}
        )
        with self._lock:
            self._write(updated)
        return updated
