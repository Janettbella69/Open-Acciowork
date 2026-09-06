"""Early native command hooks forwarded to the *same* host policy callback.

The server starts before ClaudeSDKClient.connect: deferred tools can replay
before that method finishes registering Python callback hooks (CLI 2.1.238).
No application-owned model loop or tool execution lives in this bridge.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import shlex
import sys
import tempfile
from pathlib import Path

from .native_hook_client import MAX_FRAME, TIMEOUT, failure

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop")


class NativeHookBridge:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.token = secrets.token_hex(32)
        self._directory = None
        self._server = None
        self._connections: set[asyncio.Task] = set()

    async def __aenter__(self):
        # macOS Unix sockets have a short path limit; use a private short temp
        # directory, never the Agent's writable workspace or a predictable name.
        self._directory = tempfile.TemporaryDirectory(prefix="ophira-hook-", dir="/tmp")
        self.path = Path(self._directory.name) / "gate.sock"
        try:
            self._server = await asyncio.start_unix_server(
                self._handle,
                path=self.path,
                limit=MAX_FRAME,
            )
            os.chmod(self.path, 0o600)
        except BaseException:
            self._directory.cleanup()
            raise
        return self

    async def __aexit__(self, *_args):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._directory is not None:
            self._directory.cleanup()

    def settings(self, *, ask_tools: list[str] | None = None) -> str:
        script = Path(__file__).with_name("native_hook_client.py").resolve()
        command = shlex.join([sys.executable, "-I", str(script), str(self.path), self.token])
        # A failed interpreter/exec or killed child must be a blocking hook,
        # not Claude Code's non-blocking generic command error.
        command += " || exit 2"
        return json.dumps(
            {
                "permissions": {"ask": ask_tools or []},
                "hooks": {
                    event: [
                        {"hooks": [{"type": "command", "command": command, "timeout": TIMEOUT + 1}]}
                    ]
                    for event in HOOK_EVENTS
                },
            }
        )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        event = "PreToolUse"
        response = failure()
        try:
            async with asyncio.timeout(TIMEOUT - 1):
                line = await reader.readline()
                if len(line) > MAX_FRAME:
                    raise ValueError("oversized hook frame")
                frame = json.loads(line)
                data = frame.get("input")
                # Choose the failure shape before authentication. A PostToolUse
                # transport rejection must stop, not emit an ignored PreToolUse deny.
                if isinstance(data, dict) and data.get("hook_event_name") in HOOK_EVENTS:
                    event = data["hook_event_name"]
                token = frame.get("token")
                if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
                    raise ValueError("invalid hook capability")
                if not isinstance(data, dict) or data.get("hook_event_name") not in HOOK_EVENTS:
                    raise ValueError("unsupported hook event")
                event = data["hook_event_name"]
                response = await self.callback(data, data.get("tool_use_id"), {})
                if not isinstance(response, dict):
                    raise ValueError("invalid policy output")
                response = dict(response)
                for python_key, wire_key in (("continue_", "continue"), ("async_", "async")):
                    if python_key in response:
                        response[wire_key] = response.pop(python_key)
        except (Exception, asyncio.CancelledError):
            response = failure(event)
        try:
            encoded = json.dumps(response, separators=(",", ":")).encode()
            if len(encoded) > MAX_FRAME:
                encoded = json.dumps(failure(event)).encode()
            writer.write(encoded + b"\n")
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self._connections.discard(task)
