"""Stdlib-only client for the documented Claude Code command hook interface.

It returns hook decisions only: it cannot resolve human approvals or execute
tools. Invoke with isolated Python (-I), a private socket, and a per-run token.
"""

from __future__ import annotations

import json
import socket
import sys
import time

MAX_FRAME = 16 * 1024 * 1024
TIMEOUT = 15


def failure(event: str = "PreToolUse") -> dict:
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": "OPHIRA approval gate is unavailable; tool denied.",
            }
        }
    # A post-tool failure must stop, not continue without output sanitization.
    return {"continue": False, "stopReason": "OPHIRA tool audit gate is unavailable."}


def main() -> None:
    event = "PreToolUse"
    response = failure()
    try:
        raw = sys.stdin.buffer.read(MAX_FRAME + 1)
        if len(raw) > MAX_FRAME or len(sys.argv) != 3:
            raise ValueError("invalid hook frame")
        data = json.loads(raw)
        event = data.get("hook_event_name", event)
        frame = json.dumps({"token": sys.argv[2], "input": data}, separators=(",", ":")).encode()
        if len(frame) > MAX_FRAME:
            raise ValueError("hook frame too large")
        deadline = time.monotonic() + TIMEOUT
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(TIMEOUT)
            client.connect(sys.argv[1])
            client.sendall(frame + b"\n")
            output = bytearray()
            while not output.endswith(b"\n"):
                client.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = client.recv(min(65536, MAX_FRAME + 1 - len(output)))
                if not chunk or len(output) + len(chunk) > MAX_FRAME:
                    raise ValueError("incomplete hook response")
                output.extend(chunk)
            response = json.loads(output)
            if not isinstance(response, dict):
                raise ValueError("invalid hook response")
    except Exception:
        response = failure(event)
    # Always emit an explicit deny on transport failure. A failed/empty command
    # hook can otherwise be treated by Claude Code as a non-blocking failure.
    print(json.dumps(response, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
