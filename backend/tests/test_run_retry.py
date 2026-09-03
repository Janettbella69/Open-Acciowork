import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.constants import EvidenceKind, RunMode, RunStatus
from ophira_backend.main import create_app
from ophira_backend.models import (
    AgentCreate,
    Evidence,
    OrgPolicy,
    ProjectCreate,
    ProviderProfileUpsert,
    RunRecord,
    utc_now,
)
from ophira_backend.providers import successful_preflight


class RecordingRuntime:
    def __init__(self) -> None:
        self.runs: list[RunRecord] = []

    async def run(self, run: RunRecord) -> None:
        self.runs.append(run)

    async def cancel(self, run_id: str) -> None:
        return None


async def wait_for_terminal(client: AsyncClient, run_id: str) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/api/runs/{run_id}")
        body = response.json()
        if body["status"] in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach a terminal state")


def add_ready_provider(app, provider_id: str = "persisted-provider") -> dict[str, object]:
    app.state.providers.upsert(
        provider_id,
        ProviderProfileUpsert(
            display_name="Persisted Provider",
            base_url="https://provider.example/anthropic",
            auth_env_name="ANTHROPIC_AUTH_TOKEN",
            model="model-v1",
            secret="test-secret",
            production_eligible=True,
        ),
    )
    app.state.providers.record_preflight(successful_preflight(provider_id))
    return app.state.providers.snapshot(provider_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "expected_resume_id", "source_status"),
    [
        ("resume", "sdk-source-session", RunStatus.FAILED),
        ("new_branch", None, RunStatus.CANCELLED),
    ],
)
async def test_retry_derives_source_configuration_and_preserves_project_work(
    tmp_path: Path,
    strategy: str,
    expected_resume_id: str | None,
    source_status: RunStatus,
) -> None:
    runtime = RecordingRuntime()
    app = create_app(Settings(data_dir=tmp_path), runtime=runtime)
    ledger = app.state.ledger
    project = ledger.create_project(
        ProjectCreate(name="Retry project", objective="Keep all durable work")
    )
    agent = ledger.create_agent(AgentCreate(name="Researcher", template="research"))
    provider_snapshot = add_ready_provider(app)
    evidence = Evidence(
        kind=EvidenceKind.OBSERVED_FACT,
        claim="Persisted evidence",
        source_title="Fixture",
        confidence=0.9,
        recorded_by="test",
    )
    ledger.update_project(project.id, lambda item: item.evidence.append(evidence))
    artifact = ledger.save_upload(project.id, "evidence.txt", b"durable artifact")
    skill_snapshot = [{"id": "market-research:fixture", "version": "v1"}]
    source = ledger.create_run(
        project.id,
        "Continue the persisted investigation",
        agent_id=agent.id,
        schedule_id="schedule_original",
        mode=RunMode.DEEP_RESEARCH,
        provider_profile_id="persisted-provider",
        provider_snapshot=provider_snapshot,
        skill_snapshot=skill_snapshot,
    )
    ledger.update_run(
        source.id,
        status=source_status,
        sdk_session_id="sdk-source-session",
        finished_at=utc_now(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{source.id}/retry",
            json={"strategy": strategy},
        )

        assert response.status_code == 202
        retried = response.json()
        assert retried["id"] != source.id
        assert retried["project_id"] == project.id
        assert retried["prompt"] == source.prompt
        assert retried["agent_id"] == agent.id
        assert retried["schedule_id"] is None
        assert retried["mode"] == RunMode.DEEP_RESEARCH.value
        assert retried["provider_profile_id"] == "persisted-provider"
        assert retried["provider_snapshot"] == provider_snapshot
        assert retried["skill_snapshot"] == skill_snapshot
        assert retried["resume_session_id"] == expected_resume_id
        await wait_for_terminal(client, retried["id"])

    persisted_project = ledger.get_project(project.id)
    assert [item.id for item in persisted_project.evidence] == [evidence.id]
    assert [item.id for item in persisted_project.artifacts] == [artifact.id]
    assert runtime.runs[-1].provider_snapshot == provider_snapshot
    assert runtime.runs[-1].skill_snapshot == skill_snapshot


@pytest.mark.asyncio
async def test_retry_keeps_an_explicitly_providerless_source_providerless(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=RecordingRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(
        ProjectCreate(name="Legacy project", objective="Do not substitute current defaults")
    )
    source = ledger.create_run(project.id, "Legacy prompt")
    ledger.update_run(source.id, status=RunStatus.FAILED, finished_at=utc_now())
    add_ready_provider(app, "new-default")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{source.id}/retry",
            json={"strategy": "new_branch"},
        )

        assert response.status_code == 202
        assert response.json()["provider_profile_id"] is None
        assert response.json()["provider_snapshot"] is None
        await wait_for_terminal(client, response.json()["id"])


@pytest.mark.asyncio
async def test_retry_rejects_invalid_sources_and_maps_provider_failures(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=RecordingRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(
        ProjectCreate(name="Failure project", objective="Exercise retry errors")
    )
    active = ledger.create_run(project.id, "Still active")
    missing_session = ledger.create_run(project.id, "No session")
    ledger.update_run(missing_session.id, status=RunStatus.FAILED, finished_at=utc_now())
    missing_provider = ledger.create_run(
        project.id,
        "Missing provider",
        provider_profile_id="removed-provider",
        provider_snapshot={"id": "removed-provider"},
    )
    ledger.update_run(missing_provider.id, status=RunStatus.FAILED, finished_at=utc_now())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post(
            "/api/runs/run_missing/retry",
            json={"strategy": "new_branch"},
        )
        assert missing.status_code == 404

        conflict = await client.post(
            f"/api/runs/{active.id}/retry",
            json={"strategy": "new_branch"},
        )
        assert conflict.status_code == 409
        assert "failed or cancelled" in conflict.json()["detail"]

        no_session = await client.post(
            f"/api/runs/{missing_session.id}/retry",
            json={"strategy": "resume"},
        )
        assert no_session.status_code == 409
        assert "new branch" in no_session.json()["detail"].lower()

        client_session = await client.post(
            f"/api/runs/{missing_session.id}/retry",
            json={"strategy": "new_branch", "resume_session_id": "client-injected"},
        )
        assert client_session.status_code == 422

        provider_failure = await client.post(
            f"/api/runs/{missing_provider.id}/retry",
            json={"strategy": "new_branch"},
        )
        assert provider_failure.status_code == 403

        app.state.policy.save(OrgPolicy(allowed_provider_ids=["approved-only"]))
        policy_failure = await client.post(
            f"/api/runs/{missing_provider.id}/retry",
            json={"strategy": "new_branch"},
        )
        assert policy_failure.status_code == 403
        assert "organization policy" in policy_failure.json()["detail"]
