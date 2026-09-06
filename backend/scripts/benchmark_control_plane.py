"""Offline ASGI performance baseline; never starts a Provider or SDK process.

Uses the real application, FileLedger, SkillRegistry and RunManager. The only
execution substitute is an explicitly idle, cancellable runtime. Timings are
monotonic observations, not inferred from persisted wall-clock timestamps.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "backend" / "src"))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import EventType, RunStatus  # noqa: E402
from ophira_backend.events import EventPublisher  # noqa: E402
from ophira_backend.models import RunRecord  # noqa: E402
from ophira_backend.providers import FileSecretVault  # noqa: E402

LIMITS_MS = {"create_api": 500.0, "first_event_persisted": 2000.0, "cancel_effective": 2000.0}
CURRENT_SAMPLE: ContextVar[Sample | None] = ContextVar("benchmark_sample", default=None)


@dataclass
class Sample:
    concurrency: int
    phase: str
    index: int
    lane: int
    run_id: str | None = None
    create_start_ns: int | None = None
    create_response_ns: int | None = None
    create_http_status: int | None = None
    first_event_ns: int | None = None
    runtime_started_ns: int | None = None
    cancel_start_ns: int | None = None
    cancel_response_ns: int | None = None
    cancel_http_status: int | None = None
    cancelled_event_ns: int | None = None
    runtime_exited_ns: int | None = None
    task_done_ns: int | None = None
    runtime_cancel_called: bool = False
    task_done: bool = False
    final_status: str | None = None
    persisted_events_verified: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def result(self) -> dict[str, Any]:
        def elapsed(start: int | None, end: int | None) -> float | None:
            return (end - start) / 1_000_000 if start is not None and end is not None else None

        cancel_end = (
            max(self.task_done_ns, self.cancelled_event_ns, self.runtime_exited_ns)
            if all(value is not None for value in (
                self.task_done_ns, self.cancelled_event_ns, self.runtime_exited_ns,
            )) else None
        )
        durations = {
            "create_api": elapsed(self.create_start_ns, self.create_response_ns),
            "first_event_persisted": elapsed(self.create_start_ns, self.first_event_ns),
            "cancel_effective": elapsed(self.cancel_start_ns, cancel_end),
        }
        complete = {
            "create_api": self.create_http_status == 202 and self.run_id is not None,
            "first_event_persisted": self.persisted_events_verified,
            "cancel_effective": (
                self.cancel_http_status == 200 and self.task_done
                and self.runtime_cancel_called and self.persisted_events_verified
                and self.final_status == RunStatus.CANCELLED.value
            ),
        }
        return {
            **asdict(self),
            "metrics": {
                name: {"duration_ms": duration, "complete": complete[name] and duration is not None}
                for name, duration in durations.items()
            },
        }


class OfflineWaitingRuntime:
    """No model, tools, network, or subprocess: only wait for task cancellation."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.started: dict[str, asyncio.Event] = {}

    async def run(self, run: RunRecord) -> None:
        sample = CURRENT_SAMPLE.get()
        if sample is not None:
            sample.runtime_started_ns = self.clock()
        self.started.setdefault(run.id, asyncio.Event()).set()
        try:
            await asyncio.Event().wait()
        finally:
            if sample is not None:
                sample.runtime_exited_ns = self.clock()

    async def cancel(self, _run_id: str) -> None:
        sample = CURRENT_SAMPLE.get()
        if sample is not None:
            sample.runtime_cancel_called = True
        # RunManager cancels and awaits its real execution task after this returns.


class PublishObserver:
    """Timestamp only AFTER the real publisher has appended and notified."""

    def __init__(self, publisher: EventPublisher, clock) -> None:
        self.original = publisher.publish
        self.clock = clock

    async def publish(self, *args, **kwargs):
        stored = await self.original(*args, **kwargs)
        observed_ns = self.clock()
        sample = CURRENT_SAMPLE.get()
        if sample is not None:
            sample.run_id = stored.run_id
            sample.events.append({
                "type": stored.type, "sequence": stored.sequence,
                "status": stored.data.get("status"), "published_ns": observed_ns,
            })
            queued = (
                stored.type == EventType.RUN_CREATED.value
                and stored.data.get("status") == RunStatus.QUEUED
            )
            started = (
                stored.type == EventType.RUN_STARTED.value
                and stored.data.get("status") == RunStatus.RUNNING
            )
            if (queued or started) and sample.first_event_ns is None:
                sample.first_event_ns = observed_ns
            if stored.type == EventType.RUN_CANCELLED.value:
                sample.cancelled_event_ns = observed_ns
        return stored


def isolated_app(settings: Settings, runtime: OfflineWaitingRuntime):
    """Contain main.py's import-time app construction inside the temporary store."""
    from ophira_backend import config, sdk_adapter

    name = "ophira_backend.main"
    if name not in sys.modules:
        # Only import-time defaults are redirected. No measured API implementation
        # or store is replaced; restore the imported aliases immediately afterwards.
        with (
            patch.object(config, "get_settings", return_value=settings),
            patch.object(sdk_adapter, "ClaudeAgentRuntime", return_value=runtime),
        ):
            module = importlib.import_module(name)
        module.get_settings = config.get_settings
        module.ClaudeAgentRuntime = sdk_adapter.ClaudeAgentRuntime
        return module.app
    return sys.modules[name].create_app(settings, runtime=runtime)


@contextmanager
def offline_guard() -> Iterator[None]:
    """Fail closed on outgoing sockets, child processes and secret-vault reads."""
    with ExitStack() as stack:
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
            "socket.getaddrinfo", "subprocess.Popen",
        ):
            stack.enter_context(patch(target, side_effect=RuntimeError("offline_boundary")))
        stack.enter_context(patch.object(
            FileSecretVault, "_read", side_effect=RuntimeError("secret_read_forbidden"),
        ))
        yield


async def measure_sample(app, client, runtime, sample: Sample, clock,
                         timeout_seconds: float) -> None:
    token = CURRENT_SAMPLE.set(sample)
    task = None
    try:
        sample.create_start_ns = clock()
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await client.post(
                    f"/api/projects/{app.state.benchmark_project_id}/runs",
                    json={"prompt": "Offline control-plane timing: wait for cancellation."},
                )
            sample.create_response_ns = clock()
            sample.create_http_status = response.status_code
            if response.status_code != 202:
                sample.errors.append("create_http_failed")
            elif response.json().get("id") != sample.run_id:
                sample.errors.append("create_run_id_mismatch")
        except Exception as exc:
            # Keep the elapsed failure duration; never serialize response bodies
            # or exception messages, which could contain paths or credentials.
            sample.create_response_ns = clock()
            sample.errors.append(f"create_exception:{type(exc).__name__}")

        if sample.run_id is None:
            sample.errors.append("run_not_created")
            return
        task = app.state.runs._tasks.get(sample.run_id)
        if task is None:
            sample.errors.append("execution_task_missing")
            return

        def record_done(completed: asyncio.Task) -> None:
            sample.task_done_ns = clock()
            sample.task_done = completed.done()

        task.add_done_callback(record_done)
        try:
            async with asyncio.timeout(timeout_seconds):
                await runtime.started.setdefault(sample.run_id, asyncio.Event()).wait()
        except TimeoutError:
            sample.errors.append("runtime_start_timeout")

        sample.cancel_start_ns = clock()
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await client.post(f"/api/runs/{sample.run_id}/cancel")
                sample.cancel_response_ns = clock()
                sample.cancel_http_status = response.status_code
                if response.status_code != 200:
                    sample.errors.append("cancel_http_failed")
                await asyncio.shield(task)
                # A done callback timestamps the actual task completion observation,
                # independent of when the HTTP response happens to arrive.
                await asyncio.sleep(0)
        except Exception as exc:
            sample.cancel_response_ns = sample.cancel_response_ns or clock()
            sample.errors.append(f"cancel_exception:{type(exc).__name__}")

        current = app.state.ledger.get_run(sample.run_id)
        sample.final_status = current.status.value
        persisted = app.state.ledger.list_events(sample.run_id)
        actual = {(event.sequence, event.type, event.data.get("status")) for event in persisted}
        sample.persisted_events_verified = bool(sample.events) and all(
            (event["sequence"], event["type"], event["status"]) in actual
            for event in sample.events
        )
        if current.status != RunStatus.CANCELLED:
            sample.errors.append("cancelled_state_missing")
        if sample.cancelled_event_ns is None:
            sample.errors.append("cancelled_event_missing")
        if not sample.persisted_events_verified:
            sample.errors.append("event_persistence_mismatch")
        if not sample.task_done or sample.runtime_exited_ns is None:
            sample.errors.append("execution_exit_missing")
    except Exception as exc:
        sample.errors.append(f"sample_exception:{type(exc).__name__}")
    finally:
        # Failed samples remain failures. Cleanup is outside their measurements.
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        CURRENT_SAMPLE.reset(token)


def nearest_rank(values: list[float], percentile: float) -> float | None:
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    if not values:
        return None
    return sorted(values)[math.ceil(len(values) * percentile / 100) - 1]


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = {}
    for name, limit in LIMITS_MS.items():
        observations = [sample["metrics"][name] for sample in samples]
        # Missing/incomplete measurements occupy the slow end of the FULL sample
        # population. JSON represents an unbounded percentile as null plus a flag.
        values = [
            item["duration_ms"] if item["complete"] else math.inf for item in observations
        ]
        failed = sum(not item["complete"] for item in observations)
        quantiles = {f"p{p}_ms": nearest_rank(values, p) for p in (50, 95, 99, 100)}
        p95 = quantiles["p95_ms"]
        metrics[name] = {
            "sample_count": len(values), "incomplete_count": failed, "limit_ms": limit,
            **{key: value if value is not None and math.isfinite(value) else None
               for key, value in quantiles.items()},
            "p95_unbounded": p95 is not None and not math.isfinite(p95),
            "passed": bool(values) and failed == 0 and p95 is not None and p95 < limit,
        }
    failures = sum(bool(sample["errors"]) for sample in samples)
    return {
        "sample_count": len(samples), "failed_sample_count": failures, "metrics": metrics,
        "passed": failures == 0 and all(item["passed"] for item in metrics.values()),
    }


async def run_case(root: Path, plugin: Path, *, concurrency: int, samples: int,
                   warmup: int, timeout_seconds: float,
                   on_sample: Callable[[dict[str, Any]], None] | None = None,
                   ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    origin = time.perf_counter_ns()

    def clock() -> int:
        return time.perf_counter_ns() - origin

    # model_construct reads neither environment variables nor a dotenv/secret file.
    # All controlled fields below have the same types as their declared defaults.
    settings = Settings.model_construct(data_dir=root, capability_plugin_dir=plugin)
    runtime = OfflineWaitingRuntime(clock)
    app = isolated_app(settings, runtime)
    observer = PublishObserver(app.state.events, clock)
    app.state.events.publish = observer.publish
    rows: list[dict[str, Any]] = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://localhost", trust_env=False,
        ) as client:
            response = await client.post(
                "/api/projects", json={"name": "Offline benchmark", "objective": "Timing only"},
            )
            if response.status_code != 201:
                raise RuntimeError("benchmark_project_setup_failed")
            app.state.benchmark_project_id = response.json()["id"]
            for phase, count in (("warmup", warmup), ("measured", samples)):
                async def lane(worker: int, phase=phase, count=count) -> None:
                    for index in range(worker, count, concurrency):
                        sample = Sample(concurrency, phase, index, worker)
                        await measure_sample(app, client, runtime, sample, clock, timeout_seconds)
                        row = sample.result()
                        rows.append(row)
                        if on_sample is not None:
                            on_sample(row)

                await asyncio.gather(*(lane(worker) for worker in range(concurrency)))
        pins = app.state.skills.snapshot()
        return {
            "concurrency": concurrency,
            "project_count": 1,
            "skill_snapshot": pins,
            "warmup": summarize([row for row in rows if row["phase"] == "warmup"]),
            "measured": summarize([row for row in rows if row["phase"] == "measured"]),
        }, sorted(rows, key=lambda row: (row["phase"] != "warmup", row["index"]))
    finally:
        await app.state.runs.stop()


def source_fingerprints() -> dict[str, str]:
    paths = sorted((REPOSITORY / "backend" / "src").rglob("*.py")) + [Path(__file__)]
    return {str(path.relative_to(REPOSITORY)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def environment_info() -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPOSITORY,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    return {
        "os": platform.system(), "os_release": platform.release(),
        "machine": platform.machine(), "logical_cpu_count": os.cpu_count(),
        "python": platform.python_version(), "git_head": revision,
        "packages": {name: importlib.metadata.version(name) for name in (
            "fastapi", "starlette", "httpx", "pydantic", "claude-agent-sdk",
        )},
        "clock": "time.perf_counter_ns; offsets relative to each case start",
        "clock_resolution_seconds": time.get_clock_info("perf_counter").resolution,
        "load": "Shared development machine; other workloads were not isolated or paused.",
    }


async def benchmark(*, samples: int = 100, warmup: int = 10,
                    timeout_seconds: float = 10.0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if samples < 1 or warmup < 0 or timeout_seconds <= 0:
        raise ValueError("invalid benchmark sample count or timeout")
    report: dict[str, Any] = {
        "schema_version": 1, "started_at": datetime.now(UTC).isoformat(),
        "environment": environment_info(),
        "configuration": {"samples_per_case": samples, "warmup_per_case": warmup,
                          "concurrency": [1, 4], "timeout_seconds": timeout_seconds},
        "limits_ms": LIMITS_MS,
        "percentile_method": "nearest rank: sorted[ceil(N*p/100)-1]; incomplete = +infinity",
        "scope": {
            "transport": "in-process HTTPX ASGI; no network or SSE client transport",
            "runtime": "OfflineWaitingRuntime; no SDK execution, model or Provider",
            "storage": "real temporary FileLedger; one growing project per concurrency case",
            "skills": "real SkillRegistry over a temporary copy of the bundled Plugin",
            "auth": "local_only; no trusted gateway or remote worker",
            "lifespan": "background scheduler/channel/push services not started",
            "create_api": "immediately before client.post through complete ASGI HTTP response",
            "first_event_persisted": "create start through first queued/started publish return",
            "cancel_effective": (
                "before cancel POST through later of task done callback, runtime exit, "
                "and cancelled publish return; terminal ledger verified afterwards"
            ),
            "excluded": (
                "socket/TLS/proxy/SSE delivery, SDK process interruption, model first token, "
                "multiple workers, remote filesystems, production load"
            ),
        },
        "source_before": source_fingerprints(), "cases": [],
    }
    rows: list[dict[str, Any]] = []
    def failed_case(concurrency: int, error: Exception) -> dict[str, Any]:
        # Preserve every observed row even if setup/finalization fails;
        # make unattempted planned rows explicit rather than dropping them.
        existing = {(row["phase"], row["index"]) for row in rows
                    if row["concurrency"] == concurrency}
        for phase, count in (("warmup", warmup), ("measured", samples)):
            for index in range(count):
                if (phase, index) not in existing:
                    missing = Sample(concurrency, phase, index, index % concurrency)
                    missing.errors.append(f"case_exception:{type(error).__name__}")
                    rows.append(missing.result())
        return {
            "concurrency": concurrency, "fatal_error": type(error).__name__,
            **{phase: summarize([row for row in rows
                                if row["concurrency"] == concurrency and row["phase"] == phase])
               for phase in ("warmup", "measured")},
        }

    try:
        with tempfile.TemporaryDirectory(prefix="ophira-control-plane-") as directory:
            root = Path(directory)
            plugin = root / "plugin"
            shutil.copytree(REPOSITORY / "backend" / "ophira-capabilities", plugin)
            with offline_guard():
                for concurrency in (1, 4):
                    try:
                        case, _ = await run_case(
                            root / f"case-{concurrency}", plugin, concurrency=concurrency,
                            samples=samples, warmup=warmup, timeout_seconds=timeout_seconds,
                            on_sample=rows.append,
                        )
                    except Exception as exc:
                        case = failed_case(concurrency, exc)
                    report["cases"].append(case)
        report["source_after"] = source_fingerprints()
    except Exception as exc:
        # This also retains all rows if temporary-store cleanup itself raises.
        report["fatal_error"] = type(exc).__name__
        report["source_after"] = None
        finished = {case["concurrency"] for case in report["cases"]}
        for concurrency in (1, 4):
            if concurrency not in finished:
                report["cases"].append(failed_case(concurrency, exc))
    report["source_unchanged_during_measurement"] = (
        report["source_before"] == report["source_after"]
    )
    report["acceptance_eligible"] = samples >= 100 and warmup >= 1
    report["passed"] = (
        report["acceptance_eligible"] and report["source_unchanged_during_measurement"]
        and not report.get("fatal_error")
        and not any(case.get("fatal_error") for case in report["cases"])
        and all(case["measured"]["passed"] for case in report["cases"])
        and all(case["warmup"]["passed"] for case in report["cases"])
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report, rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples", type=int, default=100, help="measured samples per case (>=100)",
    )
    parser.add_argument("--warmup", type=int, default=10, help="separate warmup samples per case")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.samples < 100 or args.warmup < 1 or args.timeout_seconds <= 0:
        parser.error("requires samples >= 100, warmup >= 1 and a positive timeout")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir or REPOSITORY / "evals" / "reports" / f"control-plane-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    try:
        report, rows = asyncio.run(benchmark(
            samples=args.samples, warmup=args.warmup, timeout_seconds=args.timeout_seconds,
        ))
    except Exception as exc:
        report, rows = {"passed": False, "fatal_error": type(exc).__name__}, []
    raw = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows)
    (output / "samples.jsonl").write_text(raw, encoding="utf-8")
    report["raw_samples_file"] = "samples.jsonl"
    report["raw_samples_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
    print(json.dumps({"passed": report["passed"], "cases": report.get("cases", []),
                      "fatal_error": report.get("fatal_error")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
