"""Offline native SDK contract probe; never connects to a real model service."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import shlex
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)


async def probe(
    *,
    tool_count: int = 1,
    startup_decision: str = "none",
    wait_batch: bool = False,
    tool_kind: str = "read",
    integrated: bool = False,
    decision: str = "allow_once",
    report_path: str | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="ophira-native-defer-") as directory:
        workspace = Path(directory)
        fixture = workspace / "fixture.txt"
        fixture.write_text("offline fixture only", encoding="utf-8")
        calls = []
        timeline = []
        assistant_ids = []
        batch_ids = []
        batch_closed = asyncio.Event()
        startup_log = workspace / "startup-hooks.jsonl"
        fixture_calls = []
        if integrated and tool_kind == "read":
            tool_kind = "mcp"
        tool_name = {"mcp": "mcp__probe__read_fixture", "read": "Read",
                     "write": "Write", "clarify": "AskUserQuestion"}[tool_kind]
        tool_input = {} if tool_kind == "mcp" else {"file_path": str(fixture)}
        question = "Which market should the fixture target?"
        if tool_kind == "clarify":
            tool_input = {"questions": [{"question": question, "header": "Market",
                          "options": [{"label": "US", "description": "US fixture"},
                                      {"label": "EU", "description": "EU fixture"}],
                          "multiSelect": False}]}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                if "count_tokens" in self.path:
                    body = b'{"input_tokens":20}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                calls.append(payload)
                blocks = (
                    [
                        {
                            "type": "tool_use",
                            "id": f"toolu_offline_{index}",
                            "name": tool_name,
                            "input": tool_input,
                        }
                        for index in range(tool_count)
                    ]
                    if len(calls) == 1
                    else [{"type": "text", "text": "Offline complete."}]
                )
                message = {
                    "id": f"msg_offline_{len(calls)}",
                    "type": "message",
                    "role": "assistant",
                    "model": payload["model"],
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 20, "output_tokens": 0},
                }
                frames = [("message_start", {"message": message})]
                for index, block in enumerate(blocks):
                    start = (
                        {**block, "input": {}}
                        if block["type"] == "tool_use"
                        else {**block, "text": ""}
                    )
                    delta = (
                        {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                        if block["type"] == "tool_use"
                        else {"type": "text_delta", "text": block["text"]}
                    )
                    frames.append(("content_block_start", {"index": index, "content_block": start}))
                    frames.append(("content_block_delta", {"index": index, "delta": delta}))
                    frames.append(("content_block_stop", {"index": index}))
                frames.extend(
                    [
                        (
                            "message_delta",
                            {
                                "delta": {
                                    "stop_reason": "tool_use" if len(calls) == 1 else "end_turn",
                                    "stop_sequence": None,
                                },
                                "usage": {"output_tokens": 10},
                            },
                        ),
                        ("message_stop", {}),
                    ]
                )
                body = "".join(
                    f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"
                    for name, data in frames
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = None
        resumed = False

        @tool("read_fixture", "Return an offline test fixture.", {})
        async def read_fixture(_args):
            fixture_calls.append({"resumed": resumed})
            print("MCP_FIXTURE_EXECUTED", json.dumps(fixture_calls[-1]), flush=True)
            return {"content": [{"type": "text", "text": "Offline MCP fixture."}]}

        async def hook(data, tool_id, _context):
            item = {
                "kind": "sdk_hook",
                "resumed": resumed,
                "tool_use_id": tool_id,
                "assistant_ids_seen": list(assistant_ids),
            }
            timeline.append(item)
            print(json.dumps(item), flush=True)
            decision = "allow" if resumed else "defer"
            if wait_batch and not resumed:
                try:
                    await asyncio.wait_for(batch_closed.wait(), timeout=2)
                    decision = "defer" if len(batch_ids) == 1 else "deny"
                    print(
                        "BATCH_READY",
                        json.dumps({"ids": batch_ids, "decision": decision}),
                        flush=True,
                    )
                except TimeoutError:
                    decision = "deny"
                    print("BATCH_TIMEOUT", flush=True)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                }
            }

        def options():
            startup_settings = None
            if resumed and startup_decision != "none":
                startup_settings = json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": tool_name,
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "timeout": 5,
                                            "command": shlex.join(
                                                [
                                                    sys.executable,
                                                    str(Path(__file__).resolve()),
                                                    "--startup-child",
                                                    startup_decision,
                                                    str(startup_log),
                                                ]
                                            ),
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            return ClaudeAgentOptions(
                cwd=workspace,
                tools=[] if tool_kind == "mcp" else [tool_name],
                allowed_tools=[tool_name],
                hooks={"PreToolUse": [HookMatcher(hooks=[hook])]},
                permission_mode="default",
                setting_sources=[],
                mcp_servers={"probe": create_sdk_mcp_server("probe", tools=[read_fixture])}
                if tool_kind == "mcp"
                else {},
                strict_mcp_config=True,
                resume=session,
                settings=startup_settings,
                include_partial_messages=True,
                include_hook_events=True,
                env={
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "ANTHROPIC_API_KEY": "offline-fixture-not-a-secret",
                    "ANTHROPIC_AUTH_TOKEN": "",
                    "CLAUDE_CODE_OAUTH_TOKEN": "",
                    "CLAUDE_CONFIG_DIR": str(workspace / "config"),
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "CLAUDE_CODE_MAX_RETRIES": "0",
                    "API_TIMEOUT_MS": "10000",
                },
                stderr=lambda text: print("CLI", text, flush=True),
            )

        def observe(message):
            item = {"kind": type(message).__name__, "resumed": resumed}
            if isinstance(message, StreamEvent):
                item["event"] = message.event.get("type")
                if item["event"] == "message_start":
                    batch_closed.clear()
                    batch_ids.clear()
                elif item["event"] == "content_block_start":
                    block = message.event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        batch_ids.append(block["id"])
                        item["tool_id"] = block["id"]
                elif item["event"] == "message_stop":
                    batch_closed.set()
            if isinstance(message, AssistantMessage):
                ids = [block.id for block in message.content if isinstance(block, ToolUseBlock)]
                assistant_ids.extend(ids)
                item["tool_ids"] = ids
            if isinstance(message, ResultMessage):
                item.update(
                    stop_reason=message.stop_reason,
                    session_id=message.session_id,
                    deferred=repr(message.deferred_tool_use),
                )
            if type(message).__name__ == "UserMessage":
                item["content"] = repr(message.content)
            if type(message).__name__ == "SystemMessage":
                item["subtype"] = message.subtype
            timeline.append(item)
            if item["kind"] != "StreamEvent" or item.get("event") in {
                "message_start",
                "message_stop",
                "content_block_start",
            }:
                print(json.dumps(item), flush=True)

        try:
            async with asyncio.timeout(90):
                if integrated:
                    from httpx import ASGITransport, AsyncClient

                    from ophira_backend.config import Settings
                    from ophira_backend.main import create_app
                    from ophira_backend.run_assembly import build_agent_runtime

                    settings = Settings(data_dir=workspace / "ledger", _env_file=None)

                    class Runtime:
                        durable_resume_supported = True

                        async def run(self, record):
                            active = self.delegate.permissions.durable.active(record)
                            await self.delegate._run_sdk_session(
                                record,
                                dataclasses.replace(
                                    options(),
                                    cwd=self.delegate.ledger.workspace_for_project(
                                        record.project_id
                                    ),
                                    resume=active.session_id if active is not None else None,
                                    can_use_tool=self.delegate._build_options(record).can_use_tool,
                                ),
                            )

                        async def cancel(self, run_id):
                            await self.delegate.cancel(run_id)

                    apps = []
                    for _ in range(2):
                        runtime = Runtime()
                        app = create_app(settings=settings, runtime=runtime)
                        runtime.delegate = build_agent_runtime(
                            settings,
                            app.state.ledger,
                            app.state.events,
                            app.state.permissions,
                        )
                        assert runtime.delegate.durable_resume_supported
                        apps.append(app)
                    async with (
                        AsyncClient(
                            transport=ASGITransport(apps[0]), base_url="http://localhost"
                        ) as a,
                        AsyncClient(
                            transport=ASGITransport(apps[1]), base_url="http://localhost"
                        ) as b,
                    ):
                        response = await a.post(
                            "/api/projects",
                            json={
                                "name": "Offline native approval",
                                "objective": "Verify exact continuation",
                            },
                        )
                        assert response.status_code == 201, response.text
                        project_id = response.json()["id"]
                        if tool_kind == "write":
                            output_file = apps[0].state.ledger.workspace_for_project(
                                project_id
                            ) / "approved.txt"
                            tool_input = {"file_path": str(output_file),
                                          "content": "Approved offline fixture"}
                        response = await a.post(
                            f"/api/projects/{project_id}/runs",
                            json={
                                "prompt": "Read the offline fixture exactly once.",
                            },
                        )
                        assert response.status_code == 202, response.text
                        run_id = response.json()["id"]
                        await apps[0].state.runs._tasks[run_id]
                        await asyncio.sleep(0)
                        pending = (await b.get("/api/permissions/pending")).json()
                        assert len(pending) == 1, repr(apps[0].state.ledger.get_run(run_id))
                        assert pending[0]["durable"] and pending[0]["state"] == "pending"
                        assert fixture_calls == []
                        print(
                            "DURABLE_PENDING_ON_SECOND_WORKER",
                            pending[0]["tool_use_id"],
                            flush=True,
                        )
                        resumed = True
                        response = await b.post(
                            f"/api/runs/{run_id}/permissions/{pending[0]['id']}",
                            json={"decision": decision,
                                  **({"answers": {question: "EU"}}
                                     if tool_kind == "clarify" else {})},
                        )
                        assert response.status_code == 204, response.text
                        await apps[1].state.runs._tasks[run_id]
                        record = apps[1].state.ledger.get_run(run_id)
                        assert record.status == "completed", repr(record)
                        expected_calls = ([{"resumed": True}]
                                          if tool_kind == "mcp" and decision != "deny" else [])
                        assert fixture_calls == expected_calls, fixture_calls
                        assert record.permission_waits[0].state == "consumed"
                        if tool_kind == "write":
                            assert output_file.exists() == (decision != "deny")
                            if decision != "deny":
                                assert output_file.read_text() == "Approved offline fixture"
                        if tool_kind == "clarify" and decision != "deny":
                            project = apps[1].state.ledger.get_project(project_id)
                            assert "EU" in repr(project.constraints), repr(project.constraints)
                            assert "EU" in json.dumps(calls[-1]["messages"])
                        completed = [e for e in apps[1].state.ledger.list_events(run_id)
                                     if e.type == "tool.completed"]
                        if decision != "deny":
                            assert any(e.tool_use_id == "toolu_offline_0"
                                       for e in completed), repr([
                                           (e.type, e.data) for e in
                                           apps[1].state.ledger.list_events(run_id)
                                           if e.type.startswith("tool.")
                                       ])
                        response = await a.post(
                            f"/api/runs/{run_id}/permissions/{pending[0]['id']}",
                            json={"decision": "allow_once"},
                        )
                        assert response.status_code == 409, response.text
                        print("NATIVE_CROSS_WORKER_CAS_COMPLETE", run_id, flush=True)
                else:
                    async with ClaudeSDKClient(options=options()) as client:
                        await client.query("Read the offline fixture exactly once.")
                        async for message in client.receive_response():
                            observe(message)
                            if isinstance(message, ResultMessage):
                                if message.stop_reason == "tool_deferred":
                                    session = message.session_id
                    if session is not None:
                        resumed = True
                        async with ClaudeSDKClient(options=options()) as client:
                            print("RESUME CONNECTED WITHOUT PROMPT", flush=True)
                            async for message in client.receive_response():
                                observe(message)
            summary = {
                        "sdk_version": version("claude-agent-sdk"),
                        "integrated": integrated,
                        "decision": decision,
                        "verification": "passed" if integrated else "observed",
                        "boundary": (
                            "offline model fixture, two control planes, native CLI subprocesses"
                        ),
                        "api_calls": len(calls),
                        "tool_count": tool_count,
                        "tool_kind": tool_kind,
                        "fixture_calls": fixture_calls,
                        "startup_decision": startup_decision,
                        "startup_hooks": startup_log.read_text() if startup_log.exists() else "",
                        "timeline": [
                            item
                            for item in timeline
                            if item["kind"] != "StreamEvent" or item.get("event") == "message_stop"
                        ],
                    }
            if report_path is not None:
                report = Path(report_path)
                report.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    report.write_text, json.dumps(summary, indent=2) + "\n", encoding="utf-8",
                )
            print("SUMMARY", json.dumps(summary), flush=True)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--startup-child":
        decision, log_file = sys.argv[2:]
        data = json.load(sys.stdin)
        with Path(log_file).open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "decision": decision,
                        "tool_use_id": data.get("tool_use_id"),
                        "tool_name": data.get("tool_name"),
                    }
                )
                + "\n"
            )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": decision,
                    }
                }
            )
        )
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int, choices=[1, 2], default=1)
        parser.add_argument("--startup", choices=["none", "allow", "deny", "defer"], default="none")
        parser.add_argument("--wait-batch", action="store_true")
        parser.add_argument("--tool-kind", choices=["read", "mcp", "write", "clarify"],
                            default="read")
        parser.add_argument("--integrated", action="store_true")
        parser.add_argument("--decision", choices=["allow_once", "deny"], default="allow_once")
        parser.add_argument("--report")
        arguments = parser.parse_args()
        asyncio.run(
            probe(
                tool_count=arguments.count,
                startup_decision=arguments.startup,
                wait_batch=arguments.wait_batch,
                tool_kind=arguments.tool_kind,
                integrated=arguments.integrated,
                decision=arguments.decision,
                report_path=arguments.report,
            )
        )
