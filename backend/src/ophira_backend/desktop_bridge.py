"""Bounded request/response channel to the owning native desktop process.

The macOS host and Python sidecar already share anonymous stdin/stdout pipes.
Only the host can answer requests on this channel; no loopback HTTP endpoint
accepts model-supplied native commands.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from io import BufferedReader, BufferedWriter
from typing import Any
from uuid import uuid4

_MAX_REQUEST_BYTES = 16_384
_MAX_RESPONSE_BYTES = 24_000_000
_OPERATIONS = frozenset({"status", "select_window", "snapshot", "press"})


class DesktopBridgeError(RuntimeError):
    """The native host is unavailable or refused a bounded operation."""


class DesktopBridge:
    def __init__(
        self,
        reader: BufferedReader,
        writer: BufferedWriter,
        *,
        on_disconnect: Callable[[], None],
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.on_disconnect = on_disconnect
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self._closed = False
        threading.Thread(target=self._read_responses, daemon=True).start()

    async def request(
        self,
        operation: str,
        *,
        run_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        if operation not in _OPERATIONS:
            raise ValueError("unsupported desktop bridge operation")
        if run_id is not None and (not run_id or len(run_id) > 160):
            raise ValueError("desktop Run identity is invalid")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("desktop bridge timeout is invalid")
        request_id = uuid4().hex
        payload = {
            "v": 1,
            "id": request_id,
            "op": operation,
            "run_id": run_id,
            "args": arguments or {},
        }
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("desktop bridge request is too large")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if self._closed:
                raise DesktopBridgeError("native desktop host is unavailable")
            self._pending[request_id] = (loop, future)
        try:
            await asyncio.to_thread(self._write, encoded)
            response = await asyncio.wait_for(future, timeout_seconds)
        except TimeoutError as exc:
            raise DesktopBridgeError("native desktop operation timed out") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if not response["ok"]:
            raise DesktopBridgeError(response["error"])
        return response["result"]

    def _write(self, encoded: bytes) -> None:
        with self._lock:
            if self._closed:
                raise DesktopBridgeError("native desktop host is unavailable")
            try:
                self.writer.write(encoded)
                self.writer.flush()
            except OSError as exc:
                self._closed = True
                raise DesktopBridgeError("native desktop host pipe is unavailable") from exc

    @staticmethod
    def _deliver(future: asyncio.Future, value: dict[str, Any]) -> None:
        if not future.done():
            future.set_result(value)

    @staticmethod
    def _fail(future: asyncio.Future) -> None:
        if not future.done():
            future.set_exception(DesktopBridgeError("native desktop host disconnected"))

    def _read_responses(self) -> None:
        try:
            while True:
                line = self.reader.readline(_MAX_RESPONSE_BYTES + 1)
                if not line:
                    break
                if len(line) > _MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
                    break
                try:
                    response = json.loads(line)
                    if (
                        not isinstance(response, dict)
                        or response.get("v") != 1
                        or not isinstance(response.get("id"), str)
                        or type(response.get("ok")) is not bool
                        or (response["ok"] and not isinstance(response.get("result"), dict))
                        or (not response["ok"] and not isinstance(response.get("error"), str))
                    ):
                        break
                except (UnicodeError, ValueError):
                    break
                with self._lock:
                    pending = self._pending.pop(response["id"], None)
                if pending is not None:
                    loop, future = pending
                    try:
                        loop.call_soon_threadsafe(self._deliver, future, response)
                    except RuntimeError:
                        pass
        finally:
            with self._lock:
                self._closed = True
                pending = list(self._pending.values())
                self._pending.clear()
            for loop, future in pending:
                try:
                    loop.call_soon_threadsafe(self._fail, future)
                except RuntimeError:
                    pass
            self.on_disconnect()
