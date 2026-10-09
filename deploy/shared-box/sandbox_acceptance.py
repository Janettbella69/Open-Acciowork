#!/usr/bin/env python3
"""Key-free check that agent runs can execute sandboxed Bash on this host.

Drives one Run through the real application runtime and the pinned Claude Agent SDK
CLI. Only the model endpoint is synthetic: a loopback fixture answers the first
model request with a single Bash tool call and the next with plain text. No model
account, user data or paid request is involved, so this proves the host side
(CLI start-up, sandbox prerequisites, Bash inside the sandbox, workspace writes)
and says nothing about a real Provider.

Usage (from the repository root, with the backend's interpreter):
    python deploy/shared-box/sandbox_acceptance.py
Exit status 0 means the sandboxed command ran and its output came back.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "backend" / "src" / "ophira_backend").is_dir():
            return parent
    return Path.cwd()


sys.path.insert(0, str(repo_root() / "backend" / "src"))

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import RunStatus  # noqa: E402
from ophira_backend.main import create_app  # noqa: E402
from ophira_backend.models import ProjectCreate  # noqa: E402

TOOL_ID = "toolu_sandbox_acceptance"


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, command: str):
        super().__init__(("127.0.0.1", 0), Handler)
        self.command = command
        self.tool_requested = False
        self.tool_result: str | None = None
        self.tool_error: bool | None = None
        self.requests: list[str] = []


def tool_result_of(payload: dict) -> tuple[str, bool] | None:
    for message in reversed(payload.get("messages", [])):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result" and block.get("tool_use_id") == TOOL_ID:
                body = block.get("content")
                if isinstance(body, list):
                    body = "".join(
                        part.get("text", "") for part in body if isinstance(part, dict)
                    )
                return str(body or ""), bool(block.get("is_error"))
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("content-length", 0))
        if not 0 < size <= 16 * 1024 * 1024:
            self.send_error(413)
            return
        payload = json.loads(self.rfile.read(size))
        if "count_tokens" in self.path:
            self.reply_json({"input_tokens": 20})
            return
        if self.path.split("?")[0] != "/v1/messages":
            self.send_error(404)
            return
        server: Fixture = self.server
        has_bash = any(tool.get("name") == "Bash" for tool in payload.get("tools") or [])
        result = tool_result_of(payload)
        if result is not None:
            server.tool_result, server.tool_error = result
            kind, block, delta, stop = (
                "final", {"type": "text", "text": ""},
                {"type": "text_delta", "text": "Sandbox acceptance finished."}, "end_turn",
            )
        elif has_bash and not server.tool_requested:
            server.tool_requested = True
            kind, block, delta, stop = (
                "tool_call",
                {"type": "tool_use", "id": TOOL_ID, "name": "Bash", "input": {}},
                {"type": "input_json_delta", "partial_json": json.dumps({
                    "command": server.command,
                    "description": "Write and read back the acceptance marker",
                })},
                "tool_use",
            )
        else:
            kind, block, delta, stop = (
                "auxiliary", {"type": "text", "text": ""},
                {"type": "text_delta", "text": "none"}, "end_turn",
            )
        server.requests.append(kind)
        message = {
            "id": f"msg_sandbox_{len(server.requests)}", "type": "message",
            "role": "assistant", "model": payload.get("model", "fixture"),
            "stop_sequence": None,
        }
        if not payload.get("stream"):
            content = (
                {**block, "input": json.loads(delta["partial_json"])}
                if block["type"] == "tool_use" else {"type": "text", "text": delta["text"]}
            )
            self.reply_json({
                **message, "content": [content], "stop_reason": stop,
                "usage": {"input_tokens": 20, "output_tokens": 5},
            })
            return
        frames = [
            ("message_start", {"message": {
                **message, "content": [], "stop_reason": None,
                "usage": {"input_tokens": 20, "output_tokens": 0},
            }}),
            ("content_block_start", {"index": 0, "content_block": block}),
            ("content_block_delta", {"index": 0, "delta": delta}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 5},
            }),
            ("message_stop", {}),
        ]
        body = "".join(
            f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"
            for name, data in frames
        ).encode()
        self.reply(body, "text/event-stream")

    def reply_json(self, value: dict):
        self.reply(json.dumps(value).encode(), "application/json")

    def reply(self, body: bytes, content_type: str):
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


async def wait_stopped(app, run_id: str, seconds: float):
    async with asyncio.timeout(seconds):
        while True:
            run = app.state.ledger.get_run(run_id)
            if run.status in {
                RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
                RunStatus.WAITING_PERMISSION,
            } and run_id not in app.state.runs._tasks:
                return run
            await asyncio.sleep(0.05)


async def exercise(root: Path) -> dict:
    marker = f"sandbox-proof-{secrets.token_hex(6)}"
    server = Fixture(f"printf '%s\\n' {marker} > acceptance-proof.txt && cat acceptance-proof.txt")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        _env_file=None, data_dir=root, anthropic_api_key="offline-fixture-not-a-secret",
        anthropic_auth_token="", anthropic_base_url=f"http://127.0.0.1:{server.server_port}",
        claude_model="claude-sonnet-4-5-20250929",
    )
    app = create_app(settings)
    ledger, manager = app.state.ledger, app.state.runs
    try:
        project = ledger.create_project(
            ProjectCreate(name="Sandbox acceptance", objective="Offline"),
        )
        workspace = ledger.workspace_for_project(project.id)
        started = await manager.create_and_start(
            project.id, "Run the acceptance command once and report its output.",
            resolve_default_provider=False, max_budget_usd=settings.max_budget_usd,
        )
        record = await wait_stopped(app, started.id, 150)
        proof = workspace / "acceptance-proof.txt"
        written = proof.read_text(encoding="utf-8").strip() if proof.is_file() else None
        passed = (
            record.status == RunStatus.COMPLETED
            and server.tool_error is False
            and marker in (server.tool_result or "")
            and written == marker
        )
        return {
            "passed": passed,
            "sdk_version": version("claude-agent-sdk"),
            "model_transport": "loopback synthetic endpoint; no external model service",
            "run_status": str(record.status),
            "run_error": getattr(record, "error", None),
            "model_requests": server.requests,
            "bash_tool_error": server.tool_error,
            "bash_output_has_marker": marker in (server.tool_result or ""),
            "bash_output_head": (server.tool_result or "")[:300],
            "workspace_file_written": written == marker,
        }
    finally:
        await manager.stop()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ophira-sandbox-acceptance-") as directory:
        report = await exercise(Path(directory))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
