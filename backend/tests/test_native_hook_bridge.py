from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ophira_backend import native_hook_client
from ophira_backend.native_hooks import NativeHookBridge


class Reader:
    def __init__(self, data):
        self.data = data

    async def readline(self):
        return self.data


class Writer:
    data = b""

    def write(self, value):
        self.data += value

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["good", "bad-token", "malformed", "error", "timeout", "stop"])
async def test_bridge_authenticates_and_fails_closed_without_sdk_or_tools(monkeypatch, case):
    calls = []

    async def callback(data, _tool_id, _context):
        calls.append(data)
        if case == "error":
            raise RuntimeError("not exposed")
        if case == "timeout":
            await asyncio.sleep(1)
        return (
            {"continue_": False, "stopReason": "fixture stop"}
            if case == "stop"
            else {
                "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "defer"}
            }
        )

    bridge = NativeHookBridge(callback)
    token = "invalid" if case == "bad-token" else bridge.token
    payload = json.dumps({"token": token, "input": {"hook_event_name": "PreToolUse"}}).encode()
    if case == "malformed":
        payload = b"not-json"
    if case == "timeout":
        monkeypatch.setattr("ophira_backend.native_hooks.TIMEOUT", 1.001)
    writer = Writer()
    await bridge._handle(Reader(payload), writer)
    result = json.loads(writer.data)
    if case == "good":
        assert result["hookSpecificOutput"]["permissionDecision"] == "defer"
    elif case == "stop":
        assert result == {"continue": False, "stopReason": "fixture stop"}
    else:
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    if case in {"bad-token", "malformed"}:
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
async def test_real_hook_client_disconnected_socket_returns_explicit_deny_or_stop(tmp_path, event):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(native_hook_client.__file__)),
        str(tmp_path / "absent.sock"),
        "not-an-approval",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await process.communicate(json.dumps({"hook_event_name": event}).encode())
    assert process.returncode == 0  # A JSON deny, never an empty/non-blocking process error.
    result = json.loads(output)
    if event == "PreToolUse":
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert result["continue"] is False


@pytest.mark.asyncio
async def test_invalid_post_tool_capability_stops_instead_of_returning_ignored_pre_deny():
    bridge = NativeHookBridge(None)
    writer = Writer()
    await bridge._handle(Reader(json.dumps({
        "token": "wrong", "input": {"hook_event_name": "PostToolUse"},
    }).encode()), writer)
    assert json.loads(writer.data)["continue"] is False


def test_startup_hook_uses_single_native_entrypoint_and_blocking_exit_wrapper():
    bridge = NativeHookBridge(None)
    bridge.path = Path("/tmp/not-a-real-test-socket")
    settings = json.loads(bridge.settings(ask_tools=["Write"]))
    assert settings["permissions"]["ask"] == ["Write"]
    for entries in settings["hooks"].values():
        assert len(entries) == 1 and len(entries[0]["hooks"]) == 1
        command = entries[0]["hooks"][0]["command"]
        assert " -I " in command
        assert command.endswith(" || exit 2")
