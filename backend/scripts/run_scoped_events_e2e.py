#!/usr/bin/env python3
"""Real Uvicorn/HTTP/SSE acceptance for project and Provider event streams.

Uses temporary application data and an explicitly offline runtime. HTTP routes,
SSE framing, event storage, project writes and scheduler dispatch are real; no
SDK, external Provider, user credential or paid service is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import ProviderErrorCode, RunStatus  # noqa: E402
from ophira_backend.main import create_app  # noqa: E402
from ophira_backend.models import ProviderPreflightResult, utc_now  # noqa: E402


class OfflineRuntime:
    def __init__(self):
        self.runs: list[str] = []
        self.preflight_gates: dict[str, asyncio.Event] = {}

    async def run(self, run):
        self.runs.append(run.id)
        await asyncio.sleep(0)
        if "SCOPED_FIXTURE_FAILURE" in run.prompt:
            raise RuntimeError("Intentional offline runtime failure")

    async def cancel(self, _run_id):
        pass

    async def preflight(self, provider_id):
        await self.preflight_gates[provider_id].wait()
        ok = provider_id == "offline_pass"
        return ProviderPreflightResult(
            provider_profile_id=provider_id, ok=ok,
            error_code=None if ok else ProviderErrorCode.SDK_RUNTIME_FAILED,
            message="Synthetic offline preflight outcome; no model was called.",
            sdk_session_id=f"offline-fixture-{provider_id}",
        )


async def request(client, method: str, path: str, **kwargs):
    response = await client.request(method, path, **kwargs)
    if response.is_error:
        raise AssertionError({
            "method": method, "path": path, "status": response.status_code,
            "response": response.text[:500],
        })
    return response.json()


async def next_event(lines, event_type: str, *, resource_id: str | None = None):
    """Read actual SSE wire frames, including their durable replay cursor."""
    async with asyncio.timeout(15):
        frame = {}
        async for line in lines:
            if line.startswith(":"):
                continue
            if line:
                key, _, value = line.partition(":")
                frame[key] = value.removeprefix(" ")
                continue
            if "data" not in frame:
                frame = {}
                continue
            data = json.loads(frame["data"])
            assert frame["id"] == data["id"]
            assert frame["event"] == data["type"]
            if data["type"] == event_type and (
                resource_id is None or data["data"].get("schedule_id") == resource_id
            ):
                return data
            frame = {}
    raise AssertionError(f"SSE ended before {event_type}")


async def history(client, base: str, **params):
    return await request(client, "GET", f"{base}/events/history", params=params)


async def wait_run(client, run_id: str):
    async with asyncio.timeout(15):
        while True:
            record = await request(client, "GET", f"/api/runs/{run_id}")
            if record["status"] in {"completed", "failed", "cancelled"}:
                return record
            await asyncio.sleep(0.02)


async def project_checks(client, checks: dict) -> tuple[str, str]:
    project = await request(client, "POST", "/api/projects", json={
        "name": "Scoped HTTP acceptance", "objective": "Offline event transport validation",
    })
    base = f"/api/projects/{project['id']}"
    initial = await history(client, base, tail=True)
    assert initial["events"][-1]["data"]["revision"] == project["revision"]
    assert initial["stream_id"] == f"project:{project['id']}"
    async with client.stream(
        "GET", f"{base}/events", params={"after": initial["last_sequence"]},
    ) as stream:
        stream.raise_for_status()
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.aiter_lines()
        artifact = await request(
            client, "POST", f"{base}/files", params={"filename": "first.txt"},
            content=b"First real HTTP upload.\n",
        )
        changed = await next_event(lines, "project.changed")
        updated = await request(client, "GET", base)
        assert changed["sequence"] > initial["last_sequence"]
        assert changed["data"]["revision"] == updated["revision"]
        assert artifact["id"] in {item["id"] for item in updated["artifacts"]}
    checks["project_http_upload_live_sse"] = True

    await request(client, "POST", f"{base}/files", params={"filename": "second.txt"},
                  content=b"A committed change while disconnected.\n")
    missing = await history(client, base, after=changed["sequence"])
    assert missing["events"] and all(
        event["sequence"] > changed["sequence"] for event in missing["events"]
    )
    async with client.stream(
        "GET", f"{base}/events", headers={"Last-Event-ID": changed["id"]},
    ) as stream:
        stream.raise_for_status()
        replayed = await next_event(stream.aiter_lines(), "project.changed")
    assert replayed == missing["events"][0]
    latest = await history(client, base, tail=True, limit=1)
    assert latest["events"] == [replayed]
    assert latest["last_sequence"] == replayed["sequence"]
    checks["project_history_tail_and_last_event_id_replay"] = True
    return project["id"], replayed["id"]


async def provider_checks(client, runtime, project_cursor: str, checks: dict):
    for provider_id, expected in (
        ("offline_pass", "provider.preflight.passed"),
        ("offline_fail", "provider.preflight.failed"),
    ):
        base = f"/api/providers/{provider_id}"
        await request(client, "PUT", base, json={
            "display_name": "Offline event fixture", "auth_env_name": "ANTHROPIC_API_KEY",
            "model": "offline-no-model", "base_url": "http://127.0.0.1:1",
            "production_eligible": False,
        })
        runtime.preflight_gates[provider_id] = asyncio.Event()
        empty = await history(client, base, tail=True)
        assert empty["events"] == [] and empty["last_sequence"] == 0
        async with client.stream("GET", f"{base}/events") as stream:
            stream.raise_for_status()
            lines = stream.aiter_lines()
            pending = asyncio.create_task(request(client, "POST", f"{base}/preflight"))
            first_event = asyncio.create_task(next_event(lines, "provider.preflight.started"))
            try:
                done, _ = await asyncio.wait(
                    {pending, first_event}, return_when=asyncio.FIRST_COMPLETED,
                )
                if pending in done:
                    pending.result()  # Surface an HTTP rejection instead of waiting for SSE.
                    raise AssertionError("Preflight finished before its fixture was released")
                started = first_event.result()
                assert not pending.done()
                runtime.preflight_gates[provider_id].set()
                result = await asyncio.wait_for(pending, 10)
                finished = await next_event(lines, expected)
            finally:
                runtime.preflight_gates[provider_id].set()
                for task in (pending, first_event):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(pending, first_event, return_exceptions=True)
        assert result["ok"] == (provider_id == "offline_pass")
        assert started["data"]["preflight_id"] == finished["data"]["preflight_id"]
        assert started["trace_id"] == finished["trace_id"]
        assert finished["sequence"] == started["sequence"] + 1
        saved = await history(client, base)
        assert saved["events"] == [started, finished]
        assert all(event["run_id"] is None for event in saved["events"])
        assert "secret" not in json.dumps(saved).lower()
        async with client.stream(
            "GET", f"{base}/events", headers={"Last-Event-ID": started["id"]},
        ) as stream:
            stream.raise_for_status()
            assert await next_event(stream.aiter_lines(), expected) == finished
        foreign = await client.get(f"{base}/events", headers={"Last-Event-ID": project_cursor})
        assert foreign.status_code == 400
        checks[f"{provider_id}_started_then_outcome_live_history_and_resume"] = True
    checks["cross_scope_last_event_id_rejected"] = True


async def schedule_checks(client, app, runtime, project_id: str, checks: dict):
    base = f"/api/projects/{project_id}"
    for label, prompt, expected_status, event_type in (
        ("success", "SCOPED_FIXTURE_SUCCESS", "completed", "schedule.completed"),
        ("failure", "SCOPED_FIXTURE_FAILURE", "failed", "schedule.failed"),
        ("skip", "SCOPED_FIXTURE_SKIP", None, "schedule.skipped"),
    ):
        schedule = await request(client, "POST", "/api/schedules", json={
            "project_id": project_id, "prompt": prompt,
            "run_once_at": (utc_now() + timedelta(hours=1)).isoformat(),
        })
        schedule_id = schedule["id"]
        holding = None
        if label == "skip":
            # A prior active Run is setup data; the scheduler's overlap decision
            # and its durable event are exercised without faking either path.
            holding = app.state.ledger.create_run(
                project_id, "Active fixture", schedule_id=schedule_id,
            )
        before = await history(client, base, tail=True)
        count = len(runtime.runs)
        async with client.stream(
            "GET", f"{base}/events", params={"after": before["last_sequence"]},
        ) as stream:
            stream.raise_for_status()
            lines = stream.aiter_lines()
            app.state.ledger.update_schedule(
                schedule_id, next_fire_at=utc_now() - timedelta(seconds=1),
            )
            await app.state.scheduler.tick()
            if expected_status is not None:
                fired = await next_event(lines, "schedule.fired", resource_id=schedule_id)
                run = await wait_run(client, fired["run_id"])
                assert run["status"] == expected_status
                assert run["schedule_id"] == schedule_id
            outcome = await next_event(lines, event_type, resource_id=schedule_id)
        if label == "skip":
            assert outcome["run_id"] is None and outcome["data"]["reason"] == "active_run"
            assert len(runtime.runs) == count
            app.state.ledger.update_run(
                holding.id, status=RunStatus.CANCELLED, finished_at=utc_now(),
            )
        else:
            assert outcome["run_id"] == fired["run_id"]
            assert len(runtime.runs) == count + 1
        await app.state.scheduler.tick()
        saved = await history(client, base, after=before["last_sequence"])
        matches = [event for event in saved["events"]
                   if event["type"] == event_type
                   and event["data"].get("schedule_id") == schedule_id]
        assert matches == [outcome]
        advanced = next(item for item in await request(client, "GET", "/api/schedules")
                        if item["id"] == schedule_id)
        if label == "skip":
            assert advanced["next_fire_at"] == schedule["run_once_at"]
        else:
            assert not advanced["enabled"] and advanced["next_fire_at"] is None
        checks[f"schedule_{label}_tick_live_sse_and_durable_history"] = True


async def run() -> dict:
    checks = {}
    runtime = OfflineRuntime()
    with tempfile.TemporaryDirectory(prefix="ophira-scoped-http-") as temporary:
        app = create_app(Settings(
            _env_file=None, data_dir=Path(temporary), auth_mode="local_only",
            anthropic_api_key="", anthropic_auth_token="", claude_model=None,
            dingtalk_app_key=None, dingtalk_app_secret=None,
            wecom_bot_id=None, wecom_bot_secret=None, feishu_app_id=None,
            feishu_app_secret=None, channel_webhook_url=None, push_enabled=False,
            companion_enabled=False,
        ), runtime=runtime)
        # Keep automatic scanning out of deterministic, explicitly ticked cases.
        app.state.scheduler.interval_seconds = 3600
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", access_log=False,
        ))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    if serving.done():
                        serving.result()
                        raise RuntimeError("Uvicorn stopped before startup")
                    await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=20,
            ) as client:
                project_id, cursor = await project_checks(client, checks)
                await provider_checks(client, runtime, cursor, checks)
                await schedule_checks(client, app, runtime, project_id, checks)
        finally:
            for gate in runtime.preflight_gates.values():
                gate.set()
            server.should_exit = True
            try:
                await asyncio.wait_for(serving, 15)
            finally:
                listener.close()
    return {
        "transport": "real Uvicorn loopback HTTP and SSE; no ASGI transport mocks",
        "checks": checks,
        "boundaries": [
            "Temporary data only; no real credentials, model calls or external services.",
            "Runtime results and Provider preflight outcomes are explicit offline fixtures.",
            "Schedule due times and one prior active Run are seeded into the temporary ledger.",
            "Validates HTTP/SSE persistence and replay, not browser rendering or Provider quality.",
            "Local-only authorization; deployed tenant and revoked-member matrices are separate.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run())
    assert all(result["checks"].values())
    report = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")
