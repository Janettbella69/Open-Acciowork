"""Verify the copied portable app without a repository Python runtime.

Run after ``zsh desktop/macos/build-portable-app.sh`` in a macOS GUI session.
All project data goes to a temporary directory; no model is called.
"""

from __future__ import annotations

import json
import os
import plistlib
import secrets
import selectors
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

APP = Path(__file__).resolve().parents[1] / "build" / "OPHIRA-portable.app"


def wait_for_handshake(process: subprocess.Popen[str]) -> int:
    assert process.stdout is not None
    watcher = selectors.DefaultSelector()
    watcher.register(process.stdout, selectors.EVENT_READ)
    try:
        if not watcher.select(55):
            raise TimeoutError("Frozen sidecar did not send its handshake")
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("Frozen sidecar exited before its handshake")
        port = json.loads(line)["port"]
        if not isinstance(port, int) or not 1 <= port <= 65_535:
            raise ValueError("Frozen sidecar returned an invalid port")
        return port
    finally:
        watcher.close()


def check_private_sidecar(app: Path, data_dir: Path) -> None:
    sidecar = app / "Contents/Resources/sidecar/OPHIRA-sidecar"
    token = secrets.token_hex(32)
    process = subprocess.Popen(
        [
            str(sidecar), "--ui-dir", str(app / "Contents/Resources/ui"),
            "--data-dir", str(data_dir), "--plugin-dir",
            str(app / "Contents/Resources/backend/ophira-capabilities"),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(token + "\n")
        process.stdin.flush()
        port = wait_for_handshake(process)
        origin = f"http://127.0.0.1:{port}"
        health = urllib.request.Request(
            origin + "/health", headers={"X-Ophira-Desktop-Token": token},
        )
        page = urllib.request.Request(
            origin + "/app", headers={"Cookie": "ophira_desktop_session=" + token},
        )
        with urllib.request.urlopen(health, timeout=10) as response:
            assert response.status == 200
        with urllib.request.urlopen(page, timeout=10) as response:
            assert response.status == 200
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def check_native_host(app: Path, data_dir: Path) -> None:
    environment = os.environ.copy()
    environment["OPHIRA_DESKTOP_DATA_DIR"] = str(data_dir)
    environment["OPHIRA_DESKTOP_PYTHON"] = "/no/python/runtime"
    environment["PYTHONPATH"] = "/no/python/modules"
    environment["PYTHONHOME"] = "/no/python/home"
    process = subprocess.Popen(
        [str(app / "Contents/MacOS/OPHIRA")],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            lines.append(line.rstrip())

    Thread(target=read_stderr, daemon=True).start()
    try:
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline and process.poll() is None:
            if any("OPHIRA native bridge ready" in line for line in lines):
                break
            time.sleep(0.25)
        assert any("OPHIRA desktop UI loaded" in line for line in lines), lines[-20:]
        assert any("OPHIRA native bridge ready" in line for line in lines), lines[-20:]
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> None:
    if not APP.is_dir():
        raise SystemExit("Build the portable app before running this smoke test")
    with TemporaryDirectory(prefix="ophira-portable-smoke-") as temporary:
        root = Path(temporary)
        copied = root / "OPHIRA.app"
        shutil.copytree(APP, copied, symlinks=True)
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(copied)], check=True,
        )
        with (copied / "Contents/Info.plist").open("rb") as source:
            info = plistlib.load(source)
        assert "OphiraPythonExecutable" not in info
        assert not (copied / "Contents/Resources/backend/src").exists()
        assert (copied / "Contents/Resources/sidecar/OPHIRA-sidecar").is_file()
        cli = copied / "Contents/Resources/sidecar/_internal/claude_agent_sdk/_bundled/claude"
        version = subprocess.run(
            [str(cli), "--version"], check=True, capture_output=True,
            text=True, timeout=15,
        )
        assert "Claude Code" in version.stdout
        check_private_sidecar(copied, root / "direct-data")
        check_native_host(copied, root / "gui-data")
    print("Portable sidecar and WebKit host smoke passed without repository Python")


if __name__ == "__main__":
    main()
