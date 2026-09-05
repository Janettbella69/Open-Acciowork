from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, RunRecord


class IdleRuntime:
    async def run(self, run: RunRecord) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_run_telemetry_endpoint_reads_the_durable_ledger(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=IdleRuntime())
    project = app.state.ledger.create_project(
        ProjectCreate(name="Telemetry", objective="Verify telemetry replay")
    )
    run = app.state.ledger.create_run(project.id, "Measure this run")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        response = await client.get(f"/api/runs/{run.id}/telemetry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "1"
    assert payload["run_id"] == run.id
    assert payload["project_id"] == project.id
    assert payload["latency"]["total_task_duration_ms"]["availability"] == "unavailable"
    assert payload["tokens"]["input_tokens"]["availability"] == "unavailable"


@pytest.mark.asyncio
async def test_run_telemetry_endpoint_returns_404_for_an_unknown_run(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=IdleRuntime())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        response = await client.get("/api/runs/run_missing/telemetry")

    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found"}
