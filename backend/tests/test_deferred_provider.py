"""Live registry changes must gate an existing approval, not only a new Run."""

from unittest.mock import AsyncMock

import pytest
from test_sdk_adapter import build_runtime

from ophira_backend.constants import PermissionDecision, ProviderErrorCode, RunStatus
from ophira_backend.durable_permissions import DurablePermissionConflict
from ophira_backend.models import (
    EventActor,
    PermissionResolution,
    ProviderPreflightResult,
    ProviderProfileUpsert,
)
from ophira_backend.runs import RunManager


def provider_checkpoint(tmp_path, monkeypatch):
    ledger, run, runtime = build_runtime(tmp_path)
    registry = runtime.providers
    registry.upsert("fixture", ProviderProfileUpsert(
        display_name="Offline readiness fixture", auth_env_name="ANTHROPIC_API_KEY",
        model="fixture-model", production_eligible=True, secret="offline-fixture-only",
    ))
    registry.record_preflight(ProviderPreflightResult(
        provider_profile_id="fixture", ok=True, message="Offline test fixture, not certification",
    ))
    run = ledger.update_run(
        run.id, provider_profile_id="fixture", provider_snapshot=registry.snapshot("fixture"),
    )
    runtime._build_options(run)
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    arguments = {"file_path": "report.txt", "content": "fixture"}
    broker = runtime.permissions
    broker.durable.prepare(run.id, "session-fixture", "tool-fixture", "Write", arguments, None)
    wait = broker.durable.confirm(
        run.id, "session-fixture", "tool-fixture", "Write", arguments,
    )
    runtime.durable_resume_supported = True
    # Even a regressed gate must never reach a model or write the native tool.
    monkeypatch.setattr(runtime, "run", AsyncMock())
    manager = RunManager(
        ledger, runtime.events, broker, runtime, providers=registry, policy=runtime.policy,
    )
    return ledger, run, runtime, manager, wait


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_decision", [False, True], ids=["approval", "continuation"])
@pytest.mark.parametrize("change,expected", [
    ({"enabled": False}, "disabled"),
    ({"production_eligible": False}, "not production eligible"),
    ({"secret_configured": False}, "credential missing"),
    ({"last_verified_at": None}, "preflight not completed"),
    ({"last_error_code": ProviderErrorCode.SDK_RUNTIME_FAILED}, "preflight failed"),
])
async def test_approval_and_continuation_recheck_current_provider_readiness(
    tmp_path, monkeypatch, saved_decision, change, expected,
):
    ledger, run, runtime, manager, wait = provider_checkpoint(tmp_path, monkeypatch)
    resolution = PermissionResolution(decision=PermissionDecision.ALLOW_ONCE)
    if saved_decision:
        runtime.permissions.durable.resolve(
            run.id, wait.id, resolution, EventActor(type="user", name="fixture reviewer"),
        )
    profile = runtime.providers.get("fixture")
    if "secret_configured" in change:
        runtime.providers.vault.delete("fixture")
    else:
        runtime.providers._write(profile.model_copy(update=change))
    try:
        with pytest.raises(DurablePermissionConflict, match=expected):
            if saved_decision:
                await manager.resume_permission(run.id)
            else:
                await manager.resolve_permission(run.id, wait.id, resolution)
        current = ledger.get_run(run.id)
        assert current.status == RunStatus.WAITING_PERMISSION
        assert runtime.permissions.durable.active(current).state == (
            "resolved" if saved_decision else "pending"
        )
        assert manager._tasks == {}
        runtime.run.assert_not_called()
        assert not (ledger.workspace_for_project(run.project_id) / "report.txt").exists()

        # A failed eligibility check preserves the exact checkpoint. Restoring
        # the operator configuration permits revalidation without a new Run.
        runtime.providers._write(profile)
        runtime.providers.vault.set("fixture", "offline-fixture-only")
        runtime.validate_deferred_resume(current)
        assert len(ledger.list_runs()) == 1
    finally:
        await manager.stop()
