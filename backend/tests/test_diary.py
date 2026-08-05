import asyncio
from pathlib import Path

import pytest

from ophira_backend.constants import AgentTemplate, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import AgentCreate, ProjectCreate, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.runs import RunManager
from ophira_backend.storage import FileLedger


class FakeRuntime:
    async def run(self, run) -> None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None


class FailingRuntime(FakeRuntime):
    async def run(self, run) -> None:
        raise RuntimeError("boom")


def build_manager(tmp_path: Path, runtime) -> tuple[FileLedger, RunManager]:
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    return ledger, RunManager(ledger, events, permissions, runtime)


async def wait_terminal(ledger: FileLedger, run_id: str) -> None:
    for _ in range(200):
        if ledger.get_run(run_id).status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("run did not reach a terminal state")


@pytest.mark.asyncio
async def test_completed_run_writes_project_diary(tmp_path: Path) -> None:
    ledger, runs = build_manager(tmp_path, FakeRuntime())
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))

    run = await runs.create_and_start(project.id, "整理报价")
    await wait_terminal(ledger, run.id)

    diary = (
        ledger.projects_dir / project.id / "diary" / f"{utc_now().date().isoformat()}.md"
    )
    content = diary.read_text(encoding="utf-8")
    assert run.id in content
    assert "整理报价" in content
    assert "状态：completed" in content


@pytest.mark.asyncio
async def test_agent_run_writes_agent_core_diary(tmp_path: Path) -> None:
    ledger, runs = build_manager(tmp_path, FakeRuntime())
    agent = ledger.create_agent(AgentCreate(name="小研", template=AgentTemplate.RESEARCH))
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))

    run = await runs.create_and_start(project.id, "竞品调研", agent_id=agent.id)
    await wait_terminal(ledger, run.id)

    diary = (
        ledger.agents_dir / agent.id / "diary" / f"{utc_now().date().isoformat()}.md"
    )
    assert run.id in diary.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_failed_run_records_note(tmp_path: Path) -> None:
    ledger, runs = build_manager(tmp_path, FailingRuntime())
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))

    run = await runs.create_and_start(project.id, "会失败的任务")
    await wait_terminal(ledger, run.id)

    diary = (
        ledger.projects_dir / project.id / "diary" / f"{utc_now().date().isoformat()}.md"
    )
    content = diary.read_text(encoding="utf-8")
    assert "状态：failed" in content
    assert "boom" in content
