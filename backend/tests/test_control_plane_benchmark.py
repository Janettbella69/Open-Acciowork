"""Verify that the offline baseline measures real lifecycle boundaries honestly."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from httpx import AsyncClient

from ophira_backend.config import Settings
from ophira_backend.constants import EventType, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate
from ophira_backend.providers import FileSecretVault
from ophira_backend.storage import FileLedger

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_control_plane.py"
SPEC = importlib.util.spec_from_file_location("ophira_control_plane_benchmark", SCRIPT)
BENCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BENCH
SPEC.loader.exec_module(BENCH)


def valid_row(duration=1.0):
    return {
        "errors": [],
        "metrics": {name: {"duration_ms": duration, "complete": True} for name in BENCH.LIMITS_MS},
    }


def test_nearest_rank_and_strict_prd_thresholds():
    assert BENCH.nearest_rank(list(range(1, 101)), 95) == 95
    assert BENCH.nearest_rank([4, 1, 3, 2], 95) == 4
    assert BENCH.nearest_rank([], 95) is None
    with pytest.raises(ValueError):
        BENCH.nearest_rank([1], 0)
    row = valid_row()
    for metric, threshold in BENCH.LIMITS_MS.items():
        row["metrics"][metric]["duration_ms"] = threshold
        assert not BENCH.summarize([row])["metrics"][metric]["passed"]
        row["metrics"][metric]["duration_ms"] = threshold - 0.001
        assert BENCH.summarize([row])["metrics"][metric]["passed"]


@pytest.mark.parametrize("missing", [1, 6])
def test_incomplete_samples_remain_in_denominator_and_fail_gate(missing):
    rows = [valid_row(float(index)) for index in range(1, 101)]
    for row in rows[-missing:]:
        row["errors"].append("create_http_failed")
        row["metrics"]["create_api"] = {"duration_ms": 0.1, "complete": False}
    summary = BENCH.summarize(rows)
    metric = summary["metrics"]["create_api"]
    assert summary["sample_count"] == metric["sample_count"] == 100
    assert summary["failed_sample_count"] == metric["incomplete_count"] == missing
    assert not summary["passed"] and not metric["passed"]
    assert metric["p95_unbounded"] is (missing == 6)
    assert metric["p95_ms"] == (None if missing == 6 else 95.0)
    json.dumps(summary, allow_nan=False)


async def test_publish_timestamp_follows_actual_publish_completion(tmp_path, monkeypatch):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Timing", objective="Post-publish"))
    run = ledger.create_run(project.id, "Synthetic")
    original = EventPublisher.publish
    completed_at = None

    async def delayed_publish(self, *args, **kwargs):
        nonlocal completed_at
        stored = await original(self, *args, **kwargs)
        assert ledger.list_events(run.id)[-1] == stored
        await asyncio.sleep(0.01)
        completed_at = time.perf_counter_ns()
        return stored

    monkeypatch.setattr(EventPublisher, "publish", delayed_publish)
    observer = BENCH.PublishObserver(EventPublisher(ledger), time.perf_counter_ns)
    sample = BENCH.Sample(1, "measured", 0, 0)
    token = BENCH.CURRENT_SAMPLE.set(sample)
    try:
        await observer.publish(run.id, EventType.RUN_CREATED, {"status": RunStatus.QUEUED})
    finally:
        BENCH.CURRENT_SAMPLE.reset(token)
    assert sample.first_event_ns >= completed_at
    assert sample.events[0]["sequence"] == 1
    assert sample.run_id == run.id


async def test_real_asgi_lifecycle_with_bundled_skills_and_isolated_settings(tmp_path, monkeypatch):
    forbidden = tmp_path / "must-not-be-used"
    monkeypatch.setenv("OPHIRA_DATA_DIR", str(forbidden))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "benchmark-secret-sentinel")

    def forbid_settings_sources(*_args, **_kwargs):
        raise AssertionError("BaseSettings constructor would read environment/dotenv")

    monkeypatch.setattr(Settings, "__init__", forbid_settings_sources)
    report, rows = await BENCH.benchmark(samples=8, warmup=4)
    assert len(rows) == 24
    assert not report["acceptance_eligible"]  # Small verification is not a 100-sample baseline.
    assert not forbidden.exists()
    for case in report["cases"]:
        assert case["measured"]["sample_count"] == 8
        assert case["warmup"]["sample_count"] == 4
        assert case["measured"]["passed"] and case["warmup"]["passed"]
        assert len(case["skill_snapshot"]) >= 10
    for row in rows:
        assert row["errors"] == []
        assert row["create_http_status"] == 202 and row["cancel_http_status"] == 200
        assert row["final_status"] == "cancelled" and row["persisted_events_verified"]
        assert row["task_done"] and row["runtime_cancel_called"]
        assert row["first_event_ns"] < row["create_response_ns"]
        assert row["runtime_exited_ns"] <= row["cancelled_event_ns"] <= row["task_done_ns"]
        assert [event["type"] for event in row["events"]] == [
            "run.created", "run.started", "run.cancel_requested", "run.cancelled",
        ]
    measured_parallel = [row for row in rows
                         if row["concurrency"] == 4 and row["phase"] == "measured"]
    assert {row["lane"] for row in measured_parallel} == {0, 1, 2, 3}
    first_wave = [row for row in measured_parallel if row["index"] < 4]
    assert max(row["create_start_ns"] for row in first_wave) < min(
        row["task_done_ns"] for row in first_wave
    )
    encoded = json.dumps({"report": report, "rows": rows}, allow_nan=False)
    assert "benchmark-secret-sentinel" not in encoded
    assert str(forbidden) not in encoded and "/Users/" not in encoded


async def test_response_delay_does_not_shift_persisted_event_measurement(monkeypatch):
    original = AsyncClient.post

    async def slow_response(self, url, **kwargs):
        response = await original(self, url, **kwargs)
        if str(url).endswith("/runs"):
            await asyncio.sleep(0.025)
        return response

    monkeypatch.setattr(AsyncClient, "post", slow_response)
    _, rows = await BENCH.benchmark(samples=1, warmup=1)
    for row in rows:
        assert row["errors"] == []
        assert row["create_response_ns"] - row["first_event_ns"] >= 25_000_000


async def test_cancel_waits_for_task_cleanup_in_addition_to_runtime_exit(monkeypatch):
    original = BENCH.OfflineWaitingRuntime.run

    async def slow_cleanup(self, run):
        try:
            await original(self, run)
        finally:
            await asyncio.sleep(0.025)

    monkeypatch.setattr(BENCH.OfflineWaitingRuntime, "run", slow_cleanup)
    _, rows = await BENCH.benchmark(samples=1, warmup=1)
    for row in rows:
        assert row["errors"] == []
        assert row["task_done_ns"] - row["runtime_exited_ns"] >= 25_000_000
        metric = row["metrics"]["cancel_effective"]
        assert metric["complete"] and metric["duration_ms"] >= 25
        assert metric["duration_ms"] == (row["task_done_ns"] - row["cancel_start_ns"]) / 1e6


async def test_http_failures_are_recorded_without_losing_requested_samples(monkeypatch):
    original = AsyncClient.post

    async def missing_project(self, url, **kwargs):
        if str(url).endswith("/runs"):
            url = "/api/projects/project_missing/runs"
        return await original(self, url, **kwargs)

    monkeypatch.setattr(AsyncClient, "post", missing_project)
    report, rows = await BENCH.benchmark(samples=3, warmup=1)
    assert len(rows) == 8
    for row in rows:
        assert row["create_http_status"] == 404
        assert row["metrics"]["create_api"]["duration_ms"] is not None
        assert not row["metrics"]["create_api"]["complete"]
        assert "create_http_failed" in row["errors"]
    assert not report["passed"]
    for case in report["cases"]:
        assert case["measured"]["failed_sample_count"] == 3
        assert case["measured"]["metrics"]["first_event_persisted"]["p95_unbounded"]


async def test_second_case_exception_retains_first_case_and_records_unattempted_rows(monkeypatch):
    original = BENCH.run_case

    async def fail_second(*args, **kwargs):
        if kwargs["concurrency"] == 4:
            raise OSError("synthetic case setup failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(BENCH, "run_case", fail_second)
    report, rows = await BENCH.benchmark(samples=2, warmup=1)
    assert len(rows) == 6
    assert report["cases"][0]["measured"]["passed"]
    assert all(not row["errors"] for row in rows if row["concurrency"] == 1)
    assert report["cases"][1]["fatal_error"] == "OSError"
    assert all(row["errors"] == ["case_exception:OSError"]
               for row in rows if row["concurrency"] == 4)
    assert not report["passed"]


async def test_temporary_directory_cleanup_failure_retains_all_completed_rows(monkeypatch):
    original = BENCH.tempfile.TemporaryDirectory

    class FailingCleanup(original):
        def __exit__(self, *args):
            super().__exit__(*args)
            if Path(self.name).name.startswith("ophira-control-plane-"):
                raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(BENCH.tempfile, "TemporaryDirectory", FailingCleanup)
    report, rows = await BENCH.benchmark(samples=2, warmup=1)
    assert len(rows) == 6 and all(not row["errors"] for row in rows)
    assert report["fatal_error"] == "OSError"
    assert not report["passed"]
    assert all(case["measured"]["passed"] for case in report["cases"])


def test_offline_guard_blocks_network_process_and_vault_access(tmp_path):
    vault = FileSecretVault(tmp_path / "empty-vault")
    with BENCH.offline_guard():
        with pytest.raises(RuntimeError, match="offline_boundary"):
            socket.create_connection(("localhost", 1))
        with pytest.raises(RuntimeError, match="offline_boundary"):
            subprocess.Popen(["nonexistent-offline-probe"])
        with pytest.raises(RuntimeError, match="secret_read_forbidden"):
            vault.get("unused")
