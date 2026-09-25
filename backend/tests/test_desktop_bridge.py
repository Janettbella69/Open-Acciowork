from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

from ophira_backend.desktop_bridge import DesktopBridge, DesktopBridgeError


@pytest.mark.asyncio
async def test_native_pipe_round_trip_and_parent_disconnect() -> None:
    inbound_read, inbound_write = os.pipe()
    outbound_read, outbound_write = os.pipe()
    disconnected = threading.Event()
    reader = os.fdopen(inbound_read, "rb", buffering=0)
    writer = os.fdopen(outbound_write, "wb", buffering=0)
    native_reader = os.fdopen(outbound_read, "rb", buffering=0)
    native_writer = os.fdopen(inbound_write, "wb", buffering=0)
    bridge = DesktopBridge(reader, writer, on_disconnect=disconnected.set)

    def native() -> None:
        command = json.loads(native_reader.readline())
        assert command["v"] == 1
        assert command["op"] == "status"
        assert command["run_id"] is None
        native_writer.write(json.dumps({
            "v": 1,
            "id": command["id"],
            "ok": True,
            "result": {"platform": "macos", "accessibility_authorized": False},
        }).encode() + b"\n")
        native_writer.flush()
        native_writer.close()

    thread = threading.Thread(target=native)
    thread.start()
    try:
        result = await bridge.request("status")
        assert result == {"platform": "macos", "accessibility_authorized": False}
        assert await asyncio.to_thread(disconnected.wait, 2)
        with pytest.raises(DesktopBridgeError, match="unavailable"):
            await bridge.request("status")
    finally:
        native_writer.close()
        native_reader.close()
        reader.close()
        writer.close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_malformed_native_response_fails_pending_request() -> None:
    inbound_read, inbound_write = os.pipe()
    outbound_read, outbound_write = os.pipe()
    disconnected = threading.Event()
    reader = os.fdopen(inbound_read, "rb", buffering=0)
    writer = os.fdopen(outbound_write, "wb", buffering=0)
    native_reader = os.fdopen(outbound_read, "rb", buffering=0)
    native_writer = os.fdopen(inbound_write, "wb", buffering=0)
    bridge = DesktopBridge(reader, writer, on_disconnect=disconnected.set)

    def native() -> None:
        assert native_reader.readline()
        native_writer.write(b'{"v":1,"id":"wrong","ok":"yes"}\n')
        native_writer.flush()
        native_writer.close()

    thread = threading.Thread(target=native)
    thread.start()
    try:
        with pytest.raises(DesktopBridgeError, match="disconnected"):
            await bridge.request("status")
        assert await asyncio.to_thread(disconnected.wait, 2)
    finally:
        native_writer.close()
        native_reader.close()
        reader.close()
        writer.close()
        thread.join(timeout=2)
