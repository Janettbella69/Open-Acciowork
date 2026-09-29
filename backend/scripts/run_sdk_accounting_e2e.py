#!/usr/bin/env python3
"""Native SDK live interjection accounting against a loopback HTTP fixture.

Uses the installed SDK/CLI, native hooks, API interjection and a temporary ledger.
Only Anthropic HTTP responses are synthetic. No paid Provider call is made;
this verifies application integration, not model quality or billing accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import httpx
from run_sdk_resource_e2e import Handler as ResourceHandler

from ophira_backend.config import Settings
from ophira_backend.constants import EventType, RunStatus
from ophira_backend.main import create_app


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, defer: bool = False, budget: float | None = None):
        super().__init__(("127.0.0.1", 0), Handler)
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.saw_direction = False
        self.defer = defer
        self.budget = budget
        self.output_path: Path | None = None


class Handler(ResourceHandler):
    def do_POST(self):
        size = int(self.headers.get("content-length", 0))
        if not 0 < size <= 8 * 1024 * 1024:
            self.send_error(413)
            return
        payload = json.loads(self.rfile.read(size))
        if "count_tokens" in self.path:
            self.respond(b'{"input_tokens":20}', "application/json")
            return
        if self.path.split("?")[0] != "/v1/messages":
            self.send_error(404)
            return
        server: FixtureServer = self.server
        server.calls += 1
        turn = server.calls
        if turn == 1:
            server.arrived.set()
            if not server.release.wait(30):
                self.send_error(504)
                return
        server.saw_direction |= "accounting-followup-marker" in json.dumps(
            payload.get("messages", []),
        )
        deferred_tool = server.defer and turn == 2
        content = (
            {"type": "tool_use", "id": "toolu_accounting_write", "name": "Write", "input": {}}
            if deferred_tool else {"type": "text", "text": ""}
        )
        delta = (
            {"type": "input_json_delta", "partial_json": json.dumps({
                "file_path": str(server.output_path), "content": "Approved accounting artifact.\n",
            })}
            if deferred_tool else {
                "type": "text_delta", "text": f"Local accounting turn {turn} completed.",
            }
        )
        frames = [
            ("message_start", {"message": {
                "id": f"msg_accounting_{turn}", "type": "message", "role": "assistant",
                "model": payload["model"], "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1_000 * turn, "output_tokens": 0},
            }}),
            ("content_block_start", {
                "index": 0, "content_block": content,
            }),
            ("content_block_delta", {
                "index": 0, "delta": delta,
            }),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {
                "delta": {
                    "stop_reason": "tool_use" if deferred_tool else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 100 * turn},
            }),
            ("message_stop", {}),
        ]
        body = "".join(
            f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"
            for name, data in frames
        ).encode()
        self.respond(body, "text/event-stream")


async def wait_stopped(app, run_id: str):
    async with asyncio.timeout(75):
        while True:
            run = app.state.ledger.get_run(run_id)
            if run.status in {
                RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
                RunStatus.WAITING_PERMISSION,
            } and run_id not in app.state.runs._tasks:
                return run
            await asyncio.sleep(0.05)


async def run_case(root: Path, server: FixtureServer) -> dict:
    settings = Settings(
        _env_file=None, data_dir=root, anthropic_api_key="offline-fixture-not-a-secret",
        anthropic_auth_token="", anthropic_base_url=f"http://127.0.0.1:{server.server_port}",
        claude_model="claude-sonnet-4-5-20250929",
    )
    app = create_app(settings)
    ledger, manager = app.state.ledger, app.state.runs
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        ) as client:
            created = await client.post(
                "/api/projects", json={"name": "Accounting acceptance", "objective": "Offline"},
            )
            created.raise_for_status()
            project_id = created.json()["id"]
            server.output_path = ledger.workspace_for_project(project_id) / "approved.md"
            # Dispatch through the manager, including its real run/workspace
            # leases. A synthetic endpoint is deliberately not marked Provider
            # preflight-ready merely to satisfy the public start route.
            started = await manager.create_and_start(
                project_id, "Return the local accounting fixture without tools.",
                resolve_default_provider=False,
                max_budget_usd=(
                    server.budget if server.budget is not None else settings.max_budget_usd
                ),
            )
            run_id = started.id
            assert await asyncio.to_thread(server.arrived.wait, 20), "SDK HTTP request missing"
            for command in ("/clear", "/reset", "/new"):
                rejected = await client.post(f"/api/runs/{run_id}/interjections", json={
                    "request_id": str(uuid4()), "mode": "now", "prompt": command,
                })
                assert rejected.status_code == 409, (command, rejected.status_code)
            assert not any(event.type == EventType.RUN_INTERJECTION_REQUESTED
                           for event in ledger.list_events(run_id))
            request_id = str(uuid4())
            direction = await client.post(f"/api/runs/{run_id}/interjections", json={
                "request_id": request_id, "mode": "now",
                "prompt": "accounting-followup-marker: return the next local text without tools.",
            })
            direction.raise_for_status()
            assert direction.json()["status"] == "injected", direction.json()
            server.release.set()
            record = await wait_stopped(app, run_id)
            if server.defer:
                assert record.status == RunStatus.WAITING_PERMISSION, (record.status, record.error)
                assert not server.output_path.exists()
                session_id = record.sdk_session_id
                resumed = await client.post(
                    f"/api/runs/{run_id}/permissions/{record.suspended_permission_id}",
                    json={"decision": "allow_once", "reason": "Approve the local fixture write."},
                )
                resumed.raise_for_status()
                record = await wait_stopped(app, run_id)
                assert record.sdk_session_id == session_id
                assert server.output_path.read_text() == "Approved accounting artifact.\n"
            expected_status = RunStatus.FAILED if server.budget is not None else RunStatus.COMPLETED
            assert record.status == expected_status, (record.status, record.error)
            events = ledger.list_events(run_id)
            results = [event.data for event in events if event.type == EventType.RUN_RESULT]
            assert len(results) == (3 if server.defer else 2), results
            if server.budget is not None:
                assert results[-1]["subtype"] == "error_max_budget_usd", results[-1]
            raw_costs = [item["segment_total_cost_usd"] for item in results]
            assert 0 < raw_costs[0] < raw_costs[1], raw_costs
            assert server.calls == len(results) and server.saw_direction
            expected_cost = sum(raw_costs[1:]) if server.defer else raw_costs[-1]
            assert results[-1]["total_cost_usd"] == expected_cost, {
                "native_running_costs": raw_costs,
                "recorded_cost": results[-1]["total_cost_usd"],
            }
            assert results[1]["model_usage"] == results[1]["segment_model_usage"]
            if server.defer:
                for model, usage in results[-1]["model_usage"].items():
                    assert usage["costUSD"] == (
                        results[1]["model_usage"][model]["costUSD"]
                        + results[-1]["segment_model_usage"][model]["costUSD"]
                    )
            # The native budget error excludes the crossing response from
            # turn usage, while cost/model_usage include it (SDK contract).
            reported_inputs = 6_000 if server.defer and server.budget is None else 3_000
            assert results[-1]["usage"]["input_tokens"] == reported_inputs
            assert results[-1]["usage"]["output_tokens"] == reported_inputs // 10
            overview = await client.get("/api/tasks")
            overview.raise_for_status()
            row = next(item for item in overview.json()["rows"] if item["run_id"] == run_id)
            assert row["estimated_cost_usd"] == expected_cost
            assert not app.state.runtime._clients
            assert not app.state.runtime._interjection_channels
            children = await asyncio.to_thread(
                subprocess.run, ["pgrep", "-P", str(os.getpid())],
                capture_output=True, text=True, check=False,
            )
            assert children.returncode == 1 and not children.stdout.strip(), "SDK child remains"
            return {
                "case": (
                    "resumed_native_budget_fuse" if server.budget is not None
                    else "live_direction_then_durable_resume" if server.defer
                    else "live_direction"
                ),
                "status": record.status, "fixture_requests": server.calls,
                "live_interjection": "injected and consumed in the original Run",
                "session_reset_commands_rejected_before_dispatch": ["/clear", "/reset", "/new"],
                "native_running_costs_usd": raw_costs,
                "recorded_cost_usd": results[-1]["total_cost_usd"],
                "per_turn_usage_accumulated": results[-1]["usage"],
                "task_list_cost": "passed", "sdk_direct_child_cleanup": "passed",
                **({"native_deferred_write_and_same_session_resume": "passed"}
                   if server.defer else {}),
                **({"logical_run_budget_usd": server.budget,
                    "native_result_subtype": results[-1]["subtype"]}
                   if server.budget is not None else {}),
            }
    finally:
        server.release.set()
        await manager.stop()


async def exercise(*, defer: bool, budget: float | None = None) -> dict:
    server = FixtureServer(defer=defer, budget=budget)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ophira-sdk-accounting-") as directory:
            result = await run_case(Path(directory), server)
        return result
    finally:
        server.release.set()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


async def run() -> dict:
    return {
        "sdk_version": version("claude-agent-sdk"),
        "provider_transport": "loopback synthetic SSE; no external model service",
        "cases": [await exercise(defer=False), await exercise(defer=True),
                  await exercise(defer=True, budget=0.02)],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = json.dumps(asyncio.run(run()), indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")
