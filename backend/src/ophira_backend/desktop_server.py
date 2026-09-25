"""Sidecar entry point for the macOS desktop host.

The native process sends a fresh secret over stdin. It is never put in an
environment variable, process argument, URL, or log message.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
from pathlib import Path

import uvicorn

from .desktop_host import DesktopHost


def main() -> None:
    parser = argparse.ArgumentParser(description="OPHIRA private desktop sidecar")
    parser.add_argument("--ui-dir", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--plugin-dir", type=Path)
    args = parser.parse_args()

    token_bytes = sys.stdin.buffer.readline(66)
    if len(token_bytes) != 65 or not token_bytes.endswith(b"\n"):
        parser.error("desktop session handshake is missing")
    try:
        token = token_bytes[:-1].decode("ascii")
    except UnicodeDecodeError:
        parser.error("desktop session handshake is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        parser.error("desktop session handshake is invalid")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2048)
        port = listener.getsockname()[1]
        os.environ["OPHIRA_HOST"] = "127.0.0.1"
        os.environ["OPHIRA_PORT"] = str(port)
        os.environ["OPHIRA_AUTH_MODE"] = "local_only"
        os.environ["OPHIRA_DATA_DIR"] = str(args.data_dir.resolve())
        os.environ["OPHIRA_COMPANION_ENABLED"] = "false"
        if args.plugin_dir is not None:
            os.environ["OPHIRA_CAPABILITY_PLUGIN_DIR"] = str(args.plugin_dir.resolve())

        # Import after the sidecar settings are fixed: main.app is constructed
        # at import time for the ordinary uvicorn entry point.
        from .main import app  # noqa: PLC0415

        desktop = DesktopHost(app, ui_dir=args.ui_dir, token=token, port=port)
        server = uvicorn.Server(uvicorn.Config(desktop, log_level="warning", access_log=False))

        def stop_with_native_host() -> None:
            # The native app keeps stdin open for its lifetime. EOF means the
            # owning app exited, including a crash or forced termination.
            try:
                while sys.stdin.buffer.read(1):
                    pass
            finally:
                server.should_exit = True

        threading.Thread(target=stop_with_native_host, daemon=True).start()
        print(json.dumps({"port": port}), flush=True)
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
