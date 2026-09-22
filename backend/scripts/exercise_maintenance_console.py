"""Exercise the real loopback console with synthetic data and compiled UI assets."""

import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))

from exercise_application_backup import prepare  # noqa: E402

from ophira_backend.application_backup import implementation, inspect_application  # noqa: E402
from ophira_backend.backup_files import BackupError, private_json, remove_staging  # noqa: E402


def install_guard():
    forbidden = [ROOT / "backend/data", ROOT / "backend/.env", Path.home() / ".claude"]

    def guard(event, arguments):
        if event in {"socket.connect", "socket.bind"}:
            address = arguments[1]
            if not isinstance(address, tuple) or address[0] != "127.0.0.1":
                raise BackupError("external_network_forbidden")
        if event == "socket.getaddrinfo" and arguments[0] != "127.0.0.1":
            raise BackupError("external_network_forbidden")
        if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(arguments[0])).absolute().resolve(strict=False)
            if any(path == item or path.is_relative_to(item) for item in forbidden):
                raise BackupError("live_data_forbidden")

    sys.addaudithook(guard)


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        path = (
            values.get("src") if tag == "script" else values.get("href") if tag == "link" else None
        )
        if path and path.startswith("/assets/"):
            self.paths.add(path)


def exercise(work):
    source = work / "source"
    source.mkdir()
    data, plugin, fixture = prepare(source)
    canonical = (data / "skills/registry-state.json").read_bytes()
    workspace = work / "maintenance"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--server",
        "--data-dir",
        str(data),
        "--plugin-dir",
        str(plugin),
        "--workspace",
        str(workspace),
        "--frontend-dist",
        str(ROOT / "frontend/dist"),
    ]
    with (
        tempfile.TemporaryFile() as errors,
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            cwd=ROOT,
            env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "backend/src")},
        ) as process,
    ):
        try:
            ready, _, _ = select.select([process.stdout], [], [], 15)
            if not ready:
                raise BackupError("maintenance_start_timeout")
            line = process.stdout.readline()
            if not line.startswith("Open the private maintenance link on this computer: "):
                raise BackupError("maintenance_start_failed")
            private_url = line.split(": ", 1)[1].strip()
            parts = urlsplit(private_url)
            assert parts.hostname == "127.0.0.1"
            origin, token = f"http://{parts.netloc}", parse_qs(parts.fragment)["key"][0]
            opener = build_opener(ProxyHandler({}))

            def request(path, body=None, *, authorized=True, expected=200):
                headers = {"Origin": origin}
                if authorized:
                    headers["Authorization"] = f"Bearer {token}"
                raw = None
                if body is not None:
                    raw = json.dumps(body).encode()
                    headers["Content-Type"] = "application/json"
                try:
                    response = opener.open(
                        Request(origin + path, data=raw, headers=headers), timeout=20
                    )
                except HTTPError as error:
                    response = error
                with response:
                    assert response.status == expected
                    content = response.read()
                    assert token.encode() not in content
                    assert b"synthetic-private-canary" not in content
                    return content

            deadline = time.monotonic() + 10
            while True:
                try:
                    html = request("/", authorized=False)
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        raise BackupError("maintenance_listen_timeout") from None
                    time.sleep(0.05)
            assets = Assets()
            assets.feed(html.decode())
            assert len(assets.paths) >= 3
            served = {
                path: hashlib.sha256(request(path, authorized=False)).hexdigest()
                for path in sorted(assets.paths)
            }
            assert b"<svg" in request("/trademind-mark.svg", authorized=False)
            request("/api/maintenance", authorized=False, expected=403)
            reviewed = json.loads(
                request(
                    "/api/maintenance/reviews",
                    {
                        "request_id": uuid4().hex,
                        "kind": "backup",
                    },
                )
            )
            assert reviewed["state"] == "reviewed" and not Path(reviewed["destination"]).exists()
            route = f"/api/maintenance/reviews/{reviewed['request_id']}/confirm"
            request(
                route, {"fingerprint": reviewed["fingerprint"], "confirmed": False}, expected=422
            )
            confirmation = {"fingerprint": reviewed["fingerprint"], "confirmed": True}
            completed = json.loads(request(route, confirmation))
            assert completed["state"] == "completed"
            assert json.loads(request(route, confirmation)) == completed
            backup = Path(completed["destination"])
            remove_staging(source)
            state = json.loads(request("/api/maintenance"))
            restore = json.loads(
                request(
                    "/api/maintenance/reviews",
                    {
                        "request_id": uuid4().hex,
                        "kind": "restore",
                        "backup_id": state["backups"][0]["id"],
                    },
                )
            )
            assert restore["summary"]["provider_invoked"] is False
            restored = json.loads(
                request(
                    f"/api/maintenance/reviews/{restore['request_id']}/confirm",
                    {"fingerprint": restore["fingerprint"], "confirmed": True},
                )
            )
            target = Path(restored["destination"])
            logical = inspect_application(target / "data", target / "application-plugin")
            assert logical["projects"] == [fixture["project_id"]]
            assert logical["runs"] == [fixture["run_id"]]
            assert (target / "data/skills/registry-state.json").read_bytes() == canonical
            assert (target / "data/secrets/synthetic.json").read_text() == (
                '{"fixture":"synthetic-private-canary"}'
            )
            corrupted = workspace / "backups/tampered-backup"
            shutil.copytree(backup, corrupted)
            (corrupted / "payload/data/secrets/synthetic.json").write_text("changed")
            state = json.loads(request("/api/maintenance"))
            row = next(row for row in state["backups"] if row["name"] == corrupted.name)
            rejected = json.loads(
                request(
                    "/api/maintenance/reviews",
                    {
                        "request_id": uuid4().hex,
                        "kind": "restore",
                        "backup_id": row["id"],
                    },
                    expected=409,
                )
            )
            assert rejected["detail"] == "file_inventory_mismatch"
            assert len(list((workspace / "restores").glob("restore-*"))) == 1
            assert "ophira_backend.main" not in sys.modules
            return {
                "passed": True,
                "scope": "real_loopback_http_with_compiled_assets_and_synthetic_data",
                "external_network_blocked": True,
                "live_data_read": False,
                "provider_invoked": False,
                "source_removed_before_restore": True,
                "canonical_preserved": True,
                "credential_preserved_without_http_exposure": True,
                "compiled_assets_served": served,
                "backup_summary": reviewed["summary"],
                "restore_summary": restore["summary"],
                "corrupt_restore_rejected": True,
                "authorization_and_confirmation_rejected": True,
                "completed_request_replayed": True,
                "browser_visual_qa": False,
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    install_guard()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    report = ROOT / "evals/reports" / f"maintenance-console-{stamp}"
    report.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="ophira-maintenance-exercise-") as temporary:
        work = Path(temporary).resolve()
        try:
            result = exercise(work)
        finally:
            remove_staging(work)
    result["implementation"] = implementation()
    result["exercise_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    private_json(report / "summary.json", result)
    print(json.dumps({"passed": result["passed"], "report": str(report / "summary.json")}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--server"]:
        install_guard()
        from ophira_backend.maintenance_console import main as serve

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        serve()
        assert "ophira_backend.main" not in sys.modules
    else:
        main()
