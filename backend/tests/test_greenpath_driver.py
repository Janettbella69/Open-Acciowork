"""Offline HTTP regressions for the real Green Path approval driver."""

import json

import httpx
import pytest

from ophira_backend.constants import EventType, RunStatus
from ophira_backend.greenpath import DENY_MARKER, PermissionPolicy, drive_run
from ophira_backend.models import EventActor, EventEnvelope, ProjectCreate
from ophira_backend.storage import FileLedger


class DriverHTTP:
    def __init__(
        self, tmp_path, *, state="pending", approval_codes=(204,), resume_codes=(202,),
        confirmation_polls=0, resolve_on_conflict=False, pending_code=200,
    ):
        self.ledger = FileLedger(tmp_path / "data")
        self.project = self.ledger.create_project(
            ProjectCreate(name="Green Path", objective="Exercise approval continuation"),
        )
        self.state = state
        self.approval_codes = list(approval_codes)
        self.resume_codes = list(resume_codes)
        self.confirmation_polls = confirmation_polls
        self.resolve_on_conflict = resolve_on_conflict
        self.pending_code = pending_code
        self.pending_polls = 0
        self.approvals = []
        self.resumes = 0
        self.cancelled = False
        self.run = None

    def event(self, event_type, data):
        self.ledger.append_event(EventEnvelope(
            id="pending", sequence=1, type=event_type.value, run_id=self.run.id,
            project_id=self.project.id, trace_id=self.run.trace_id,
            actor=EventActor(type="system", name="fixture"), data=data,
        ))

    def handle(self, request):
        path = request.url.path
        if path == f"/api/projects/{self.project.id}/runs" and request.method == "POST":
            self.run = self.ledger.create_run(
                self.project.id, json.loads(request.content)["prompt"],
            )
            self.run = self.ledger.update_run(self.run.id, status=RunStatus.WAITING_PERMISSION)
            self.event(EventType.PERMISSION_REQUESTED, {
                "request_id": "approval-one", "tool_name": "Write",
                "tool_input": {"file_path": f"{DENY_MARKER}.md"}, "durable": True,
            })
            return httpx.Response(202, json=self.run.model_dump(mode="json"))
        if path == "/api/permissions/pending" and request.method == "GET":
            self.pending_polls += 1
            if self.pending_code != 200:
                return httpx.Response(self.pending_code, json={"detail": "Cannot read approvals"})
            visible = self.pending_polls > self.confirmation_polls
            return httpx.Response(200, json=[{
                "id": "approval-one", "run_id": self.run.id, "project_id": self.project.id,
                "tool_name": "Write", "tool_input": {"file_path": f"{DENY_MARKER}.md"},
                "durable": True, "state": self.state,
                "scope": "path:.", "destructive": False, "persistable": True,
            }] if visible else [])
        if path == f"/api/runs/{self.run.id}/permissions/approval-one":
            self.approvals.append(json.loads(request.content))
            code = self.approval_codes.pop(0) if self.approval_codes else 409
            if code == 204 or (code == 409 and self.resolve_on_conflict):
                self.state = "resolved"
                self.event(EventType.PERMISSION_RESOLVED, {
                    "request_id": "approval-one", "decision": self.approvals[-1]["decision"],
                })
            return httpx.Response(code, json={"detail": "Fixture approval response"})
        if path == f"/api/runs/{self.run.id}/resume-permission":
            self.resumes += 1
            code = self.resume_codes.pop(0) if self.resume_codes else 409
            if code == 202:
                self.ledger.update_run(self.run.id, status=RunStatus.COMPLETED)
            return httpx.Response(code, json={"detail": "Fixture resume response"})
        if path == f"/api/runs/{self.run.id}/cancel":
            self.cancelled = True
            self.ledger.update_run(self.run.id, status=RunStatus.CANCELLED)
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected HTTP request: {request.method} {path}")

    async def drive(self, policy, *, timeout_s=2):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), base_url="http://fixture",
        ) as client:
            return await drive_run(
                client, self.ledger, self.project.id, "Fixture prompt",
                provider_id="fixture", policy=policy, timeout_s=timeout_s, poll_s=0,
            )


@pytest.mark.asyncio
async def test_driver_retries_409_then_resumes_without_counting_deny_twice(tmp_path):
    server = DriverHTTP(tmp_path, approval_codes=(409, 204), resume_codes=(409, 202))
    policy = PermissionPolicy()
    result = await server.drive(policy)
    assert result["status"] == "completed"
    assert len(server.approvals) == 2
    assert server.approvals[0] == server.approvals[1]
    assert server.approvals[0]["decision"] == "deny"
    assert server.resumes == 2
    assert policy.denied == ["Write"]
    assert server.pending_polls >= 4


@pytest.mark.asyncio
async def test_driver_waits_for_confirmed_pending_instead_of_acting_on_event(tmp_path):
    server = DriverHTTP(tmp_path, confirmation_polls=2)
    result = await server.drive(PermissionPolicy())
    assert result["status"] == "completed"
    assert server.pending_polls >= 3
    assert len(server.approvals) == 1


@pytest.mark.asyncio
async def test_driver_resumes_durable_already_resolved_without_making_another_decision(tmp_path):
    server = DriverHTTP(tmp_path, state="resolved")
    policy = PermissionPolicy()
    assert (await server.drive(policy))["status"] == "completed"
    assert server.approvals == []
    assert server.resumes == 1
    assert policy.denied == [] and policy.allowed == []


@pytest.mark.asyncio
async def test_conflict_reloads_resolved_state_instead_of_reposting_resolution(tmp_path):
    server = DriverHTTP(tmp_path, approval_codes=(409,), resolve_on_conflict=True)
    policy = PermissionPolicy()
    assert (await server.drive(policy))["status"] == "completed"
    assert len(server.approvals) == 1
    assert server.resumes == 1
    assert policy.denied == ["Write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [403, 404, 500])
@pytest.mark.parametrize("operation", ["pending", "approval", "resume"])
async def test_failed_http_request_is_reported_as_failure(tmp_path, operation, code):
    settings = {
        "pending": {"pending_code": code},
        "approval": {"approval_codes": (code,)},
        "resume": {"state": "resolved", "resume_codes": (code,)},
    }[operation]
    server = DriverHTTP(tmp_path, **settings)
    with pytest.raises(httpx.HTTPStatusError) as error:
        await server.drive(PermissionPolicy())
    assert error.value.response.status_code == code
    assert server.ledger.get_run(server.run.id).status == RunStatus.WAITING_PERMISSION


@pytest.mark.asyncio
async def test_persistent_conflict_retries_until_timeout_then_checks_cancellation(tmp_path):
    server = DriverHTTP(tmp_path, approval_codes=(409,))
    policy = PermissionPolicy()
    with pytest.raises(TimeoutError, match="exceeded"):
        await server.drive(policy, timeout_s=0.1)
    assert len(server.approvals) > 1
    assert policy.denied == ["Write"]
    assert server.cancelled


@pytest.mark.asyncio
async def test_failed_approval_cannot_be_hidden_by_a_terminal_run_status(tmp_path):
    server = DriverHTTP(tmp_path, approval_codes=(500,))
    original = server.handle

    def finish_with_failed_http(request):
        response = original(request)
        if response.status_code == 500:
            server.ledger.update_run(server.run.id, status=RunStatus.COMPLETED)
        return response

    server.handle = finish_with_failed_http
    with pytest.raises(httpx.HTTPStatusError) as error:
        await server.drive(PermissionPolicy())
    assert error.value.response.status_code == 500
