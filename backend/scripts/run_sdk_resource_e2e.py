#!/usr/bin/env python3
"""Real pinned SDK/CLI, native hooks, API reads and file-ledger resource checks.

The Anthropic HTTP transport is a loopback fixture. No model account, user data,
external side effect, or paid request is used. This is not Provider certification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import ProviderErrorCode, RunStatus  # noqa: E402
from ophira_backend.main import create_app  # noqa: E402
from ophira_backend.models import ProjectCreate, SdkResourceUsage  # noqa: E402


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.calls: dict[str, int] = {}
        self.release = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("content-length", 0))
        if not 0 < size <= 8 * 1024 * 1024:
            self.send_error(413)
            return
        payload = json.loads(self.rfile.read(size))
        if "count_tokens" in self.path:
            self.respond(b'{"input_tokens":20}', "application/json")
            return
        case = self.path.split("/")[1]
        if case not in {"normal", "output", "buffer", "timeout"}:
            self.send_error(404)
            return
        server: FixtureServer = self.server
        server.calls[case] = server.calls.get(case, 0) + 1
        if case == "timeout":
            server.release.wait(30)
        texts = {
            "normal": ["Local SDK resource acceptance completed."],
            "output": ["Bounded output fixture. " * 512] * 120,
            "buffer": ["Large frame fixture. " * 8192],
            "timeout": ["Too late."],
        }[case]
        frames = [
            ("message_start", {"message": {
                "id": "msg_resource_fixture", "type": "message", "role": "assistant",
                "model": payload["model"], "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 20, "output_tokens": 0},
            }}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            *[("content_block_delta", {
                "index": 0, "delta": {"type": "text_delta", "text": text},
            }) for text in texts],
            ("content_block_stop", {"index": 0}),
            ("message_delta", {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": len(texts)},
            }),
            ("message_stop", {}),
        ]
        body = "".join(
            f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"
            for name, data in frames
        ).encode()
        self.respond(body, "text/event-stream")

    def respond(self, body, content_type):
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the SDK process is terminated by the envelope.


async def run_case(root: Path, server: FixtureServer, case: str) -> dict:
    settings = Settings(
        _env_file=None, data_dir=root, anthropic_api_key="offline-fixture-not-a-secret",
        anthropic_base_url=f"http://127.0.0.1:{server.server_port}/{case}",
        claude_model="claude-sonnet-4-5-20250929",
    )
    app = create_app(settings)
    ledger, manager = app.state.ledger, app.state.runs
    project = ledger.create_project(ProjectCreate(name="Resource acceptance", objective="Offline"))
    workspace = ledger.workspace_for_project(project.id)
    original = b"# Saved evidence\nThis local artifact must survive SDK termination.\n"
    (workspace / "saved.md").write_bytes(original)
    artifact = ledger.register_artifact(project.id, "saved.md")
    run = ledger.create_run(
        project.id, "Return the local text fixture without using tools.",
        skill_snapshot=app.state.skills.snapshot(),
    )
    ledger.update_run(run.id, sdk_resource_usage=SdkResourceUsage(
        active_limit_seconds=8 if case == "timeout" else 60,
        output_limit_bytes=1_048_576 if case == "output" else 67_108_864,
        message_buffer_bytes=65_536 if case == "buffer" else 16_777_216,
    ))
    await asyncio.wait_for(manager._execute_runtime(run.id), 75)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as client:
        response = await client.get(f"/api/runs/{run.id}")
        response.raise_for_status()
        record = response.json()
        downloaded = await client.get(artifact.download_url)
        downloaded.raise_for_status()
        assert downloaded.content == original
    assert record["status"] == (RunStatus.COMPLETED if case == "normal" else RunStatus.FAILED), (
        case, record["status"], record.get("error_code"),
    )
    if case != "normal":
        assert record["error_code"] == ProviderErrorCode.SDK_RESOURCE_LIMIT_EXCEEDED, (
            case, record.get("error_code"),
        )
        expected = {"output": "output allowance", "buffer": "message buffer", "timeout": "time"}
        assert expected[case] in record["error"]
    usage = record["sdk_resource_usage"]
    assert usage["active_started_at"] is None
    assert usage["output_bytes_reserved"] <= usage["output_limit_bytes"]
    assert not app.state.runtime._clients
    assert not app.state.runtime._interjection_channels
    assert server.calls.get(case, 0) > 0
    # Inspect the SDK's own direct subprocess children by PID only, never argv.
    import os

    children = await asyncio.to_thread(
        subprocess.run, ["pgrep", "-P", str(os.getpid())],
        capture_output=True, text=True, check=False,
    )
    assert children.returncode == 1 and not children.stdout.strip(), "SDK child still running"
    return {
        "case": case, "status": record["status"], "error_code": record.get("error_code"),
        "fixture_requests": server.calls[case],
        "output_bytes_reserved": usage["output_bytes_reserved"],
        "active_seconds_used": round(usage["active_seconds_used"], 3),
        "saved_artifact_download": "passed", "sdk_direct_child_cleanup": "passed",
    }


async def run() -> dict:
    server = FixtureServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ophira-sdk-resources-") as directory:
            cases = []
            for case in ("normal", "output", "buffer", "timeout"):
                cases.append(await run_case(Path(directory) / case, server, case))
            return {
                "sdk_version": version("claude-agent-sdk"),
                "provider_transport": "loopback synthetic SSE; no external model service",
                "cases": cases,
            }
    finally:
        server.release.set()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run())
    report = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")
