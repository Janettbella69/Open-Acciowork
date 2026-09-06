"""Independent process/authorization audit of durable approval boundaries."""

from __future__ import annotations

import multiprocessing
import os
from datetime import timedelta

import pytest
from test_durable_permissions import ACTOR, ALLOW, SESSION, TOOL_INPUT, checkpoint

from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.durable_permissions import DurablePermissionConflict
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.models import PrincipalContext, ProjectCreate, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.storage import FileLedger


def _compete_for_permission(root, run_id, request_id, operation, barrier, outcomes):
    ledger = FileLedger(root)
    broker = PermissionBroker(ledger, EventPublisher(ledger), 300)
    barrier.wait(timeout=15)
    try:
        if operation == "resolve":
            broker.durable.resolve(run_id, request_id, ALLOW, ACTOR)
        else:
            broker.durable.consume(run_id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)
        outcomes.put((os.getpid(), "accepted"))
    except DurablePermissionConflict:
        outcomes.put((os.getpid(), "conflict"))


@pytest.mark.parametrize("operation", ["resolve", "consume"])
def test_durable_transition_has_one_winner_across_spawned_processes(tmp_path, operation):
    ledger, run, broker, wait = checkpoint(tmp_path)
    if operation == "consume":
        broker.durable.resolve(run.id, wait.id, ALLOW, ACTOR)
        broker.durable.claim_resume(run.id)
        ledger.update_run(run.id, status=RunStatus.RUNNING)
    context = multiprocessing.get_context("spawn")
    barrier, outcomes = context.Barrier(3), context.Queue()
    children = [context.Process(target=_compete_for_permission, args=(
        tmp_path, run.id, wait.id, operation, barrier, outcomes,
    )) for _ in range(2)]
    for child in children:
        child.start()
    try:
        barrier.wait(timeout=15)
        results = [outcomes.get(timeout=15) for _ in children]
        assert len({pid for pid, _ in results}) == 2
        assert all(pid != os.getpid() for pid, _ in results)
        assert sorted(outcome for _, outcome in results) == ["accepted", "conflict"]
    finally:
        for child in children:
            child.join(timeout=15)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        outcomes.close()
    assert all(child.exitcode == 0 for child in children)
    current = broker.durable.lookup(run.id, wait.id)
    assert current.state == ("resolved" if operation == "resolve" else "consumed")


def _authenticated_checkpoint(tmp_path):
    config = Settings(
        _env_file=None, data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_tenant_id="audit_tenant",
        trusted_gateway_auth_secret="test-only-audit-gateway-secret-over-32-bytes",
    )
    app = create_app(config)
    now = utc_now()
    owner = PrincipalContext(
        tenant_id="audit_tenant", principal_id="owner", role=PrincipalRole.OWNER,
        session_id="audit_owner_session", assertion_issued_at=now,
        assertion_expires_at=now + timedelta(minutes=1),
        assertion_nonce="audit_nonce_0000000000000000001",
    )
    member = owner.model_copy(update={
        "principal_id": "member", "role": PrincipalRole.ADMIN,
        "session_id": "audit_member_session",
    })
    ledger, access = app.state.ledger, app.state.project_access
    project = ledger.create_project(ProjectCreate(name="Audit", objective="Local fixture"),
                                    principal=owner)
    access.bind_project(project.id, owner)
    access.grant(project.id, owner, principal_id=member.principal_id, role=member.role)
    run = ledger.create_run(project.id, "Write audit fixture", principal=member)
    runtime = app.state.runs.runtime
    run = ledger.update_run(run.id, status=RunStatus.RUNNING,
                            skill_snapshot=runtime.skills.snapshot())
    runtime._build_options(run)  # Freeze the logical Run's existing SDK limits/policy.
    store = app.state.permissions.durable
    wait = store.prepare(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)
    store.confirm(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT)
    store.resolve(run.id, wait.id, ALLOW, ACTOR)
    store.claim_resume(run.id)
    return app, owner, member, ledger.get_run(run.id), wait


def _change_authority(app, owner, member, run, change):
    if change == "membership_revoked":
        app.state.project_access.revoke(run.project_id, owner, principal_id=member.principal_id)
    elif change == "role_changed":
        app.state.project_access.grant(
            run.project_id, owner, principal_id=member.principal_id, role=PrincipalRole.VIEWER,
        )
    else:
        app.state.runs.runtime.settings.trusted_gateway_tenant_id = "replacement_tenant"


@pytest.mark.parametrize("change", ["membership_revoked", "role_changed", "tenant_changed"])
def test_deferred_resume_preflight_rechecks_bound_principal(tmp_path, change):
    app, owner, member, run, _wait = _authenticated_checkpoint(tmp_path)
    runtime = app.state.runs.runtime
    runtime.validate_deferred_resume(run)
    _change_authority(app, owner, member, run, change)
    with pytest.raises(DurablePermissionConflict, match="authority"):
        runtime.validate_deferred_resume(run)


@pytest.mark.parametrize("change", ["membership_revoked", "role_changed", "tenant_changed"])
async def test_deferred_hook_rechecks_authority_after_approval_before_consumption(tmp_path, change):
    app, owner, member, run, wait = _authenticated_checkpoint(tmp_path)
    runtime = app.state.runs.runtime
    runtime.durable_resume_supported = True
    runtime.validate_deferred_resume(run)
    _change_authority(app, owner, member, run, change)
    run = app.state.ledger.update_run(run.id, status=RunStatus.RUNNING)
    hook = runtime._permission_hook(run, app.state.ledger.workspace_for_project(run.project_id))
    result = await hook({
        "hook_event_name": "PreToolUse", "session_id": SESSION,
        "tool_use_id": "toolu_fixture", "tool_name": "Write", "tool_input": TOOL_INPUT,
    }, "toolu_fixture", {})
    assert (result.get("continue_", result.get("continue")) is False
            or result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
    assert app.state.permissions.durable.lookup(run.id, wait.id).state != "consumed"


@pytest.mark.parametrize("status", [
    RunStatus.CANCELLING, RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.WAITING_PERMISSION,
])
@pytest.mark.parametrize("tool_name, tool_input", [
    ("Read", {"file_path": "report.txt"}), ("Bash", {"command": "pwd"}),
])
async def test_nonrunning_run_cannot_automatically_execute_tools(
    tmp_path, status, tool_name, tool_input,
):
    app, _owner, _member, run, _wait = _authenticated_checkpoint(tmp_path)
    runtime = app.state.runs.runtime
    runtime.durable_resume_supported = True
    run = app.state.ledger.update_run(run.id, status=status)
    hook = runtime._permission_hook(run, app.state.ledger.workspace_for_project(run.project_id))
    result = await hook({
        "hook_event_name": "PreToolUse", "session_id": SESSION,
        "tool_use_id": "toolu_automatic", "tool_name": tool_name, "tool_input": tool_input,
    }, "toolu_automatic", {})
    assert result.get("continue_", result.get("continue")) is False
    assert "toolu_automatic" not in runtime._approved_invocations.get(run.id, {})


@pytest.mark.parametrize("change", ["revoke", "cancel"])
async def test_authority_is_rechecked_after_the_awaited_policy_callback(
    tmp_path, monkeypatch, change,
):
    app, owner, member, run, wait = _authenticated_checkpoint(tmp_path)
    runtime = app.state.runs.runtime
    runtime.durable_resume_supported = True
    run = app.state.ledger.update_run(run.id, status=RunStatus.RUNNING)
    calls = []

    async def policy_callback(*_args):
        calls.append(True)
        if change == "revoke":
            app.state.project_access.revoke(run.project_id, owner, principal_id=member.principal_id)
        else:
            app.state.ledger.update_run(run.id, status=RunStatus.CANCELLING)
        return {}

    monkeypatch.setattr(runtime, "_hook", lambda *_args: policy_callback)
    hook = runtime._permission_hook(run, app.state.ledger.workspace_for_project(run.project_id))
    result = await hook({
        "hook_event_name": "PreToolUse", "session_id": SESSION,
        "tool_use_id": "toolu_fixture", "tool_name": "Write", "tool_input": TOOL_INPUT,
    }, "toolu_fixture", {})
    assert calls == [True]
    assert result.get("continue_", result.get("continue")) is False
    assert app.state.permissions.durable.lookup(run.id, wait.id).state != "consumed"
