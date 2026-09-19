"""Two real HTTP workers and process death after a durable deletion intent.

PYTHONPATH=backend/src .venv/bin/python backend/scripts/check_project_deletion.py
Uses disposable data, synthetic native sessions, and zero model/network calls.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import socket
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx


def serve(data, listener):
    import uvicorn

    from ophira_backend import config

    settings = config.Settings(
        _env_file=None, data_dir=Path(data), auth_mode="local_only",
        anthropic_api_key=None, anthropic_auth_token=None, anthropic_base_url=None,
    )
    config.get_settings = lambda: settings
    from ophira_backend.main import create_app

    class NoModels:
        async def run(self, _run):
            raise AssertionError("Deletion must not invoke a model")

        async def cancel(self, _run_id):
            raise AssertionError("No model exists to cancel")

    app = create_app(settings, runtime=NoModels())
    import ophira_backend.project_deletion as module

    original = module.purge_project_deletion

    def crash_once(root, plan):
        flag = Path(data) / ".synthetic-crash-once"
        if flag.exists():
            flag.unlink()
            os._exit(71)  # Intent and native erasure are durable; no cleanup handler runs.
        return original(root, plan)

    module.purge_project_deletion = crash_once
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


def request(port, method, path, *, status=200, **kwargs):
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=20) as client:
        response = client.request(method, path, **kwargs)
        assert response.status_code == status, response.text
        if "deletion" in path:
            assert response.headers["cache-control"] == "no-store"
        return response.json()


def stop(process):
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
    process.join(5)


def start(context, data):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        process = context.Process(target=serve, args=(data, listener))
        process.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                request(port, "GET", "/health", timeout=0.5)
                return process, port
            except httpx.TransportError:
                if not process.is_alive():
                    raise RuntimeError("Disposable deletion worker exited") from None
                time.sleep(0.05)
        raise RuntimeError("Disposable deletion worker was not ready")
    except BaseException:
        stop(process)
        raise


def exercise():
    from ophira_backend.models import ProjectCreate
    from ophira_backend.storage import FileLedger

    context = multiprocessing.get_context("spawn")
    processes = []
    with tempfile.TemporaryDirectory(prefix="ophira-project-deletion-http-") as temporary:
        root = Path(temporary).resolve()
        ledger = FileLedger(root)
        project = ledger.create_project(ProjectCreate(name="Erase fixture", objective="synthetic"))
        other = ledger.create_project(ProjectCreate(name="Keep fixture", objective="synthetic"))
        run = ledger.create_run(project.id, "private prompt canary")
        sid = str(uuid4())
        ledger.update_run(run.id, sdk_session_id=sid)
        workspace = ledger.workspace_for_project(project.id)
        (workspace / "private.txt").write_text("private file canary")
        config = root / "runtime-config" / "environment"
        native = config / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(workspace))
        native.mkdir(parents=True)
        (native / f"{sid}.jsonl").write_text(json.dumps({
            "type": "user", "cwd": str(workspace), "sessionId": sid, "uuid": str(uuid4()),
            "message": {"role": "user", "content": "private native canary"},
        }) + "\n")
        checkpoints = config / "file-history" / sid
        checkpoints.mkdir(parents=True)
        (checkpoints / "checkpoint").write_text("private checkpoint canary")
        try:
            first, port = start(context, temporary)
            processes.append(first)
            second, peer = start(context, temporary)
            processes.append(second)
            path = f"/api/projects/{project.id}"
            preview = request(port, "GET", path + "/deletion")
            assert preview["can_delete"] and preview["counts"]["sessions"] == 1
            payload = {
                "operation_id": str(uuid4()), "fingerprint": preview["fingerprint"],
                "confirmation_name": project.name, "reason": "Synthetic cleanup",
            }
            (root / ".synthetic-crash-once").touch()
            try:
                request(port, "POST", path + "/deletion", json=payload)
                raise AssertionError("Expected connection loss after process death")
            except httpx.TransportError:
                pass
            first.join(10)
            assert first.exitcode == 71
            operation = f"/api/project-deletions/{project.id}/{payload['operation_id']}"
            assert request(peer, "GET", operation)["status"] == "deleting"
            request(peer, "GET", path, status=404)
            request(peer, "POST", path + "/runs", json={"prompt": "no resurrection"}, status=404)
            assert request(peer, "GET", f"/api/projects/{other.id}")["id"] == other.id
            restarted, restarted_port = start(context, temporary)
            processes.append(restarted)
            for observed_port in (peer, restarted_port):
                assert request(observed_port, "GET", operation)["status"] == "deleted"
                assert request(observed_port, "POST", operation + "/retry")["status"] == "deleted"
                request(observed_port, "GET", path, status=404)
                request(observed_port, "GET", f"/api/runs/{run.id}", status=404)
            assert not (root / "projects" / project.id).exists()
            assert not (root / "runs" / run.id).exists()
            assert not native.exists() and not checkpoints.exists()
            assert (root / "projects" / other.id / "project.json").exists()
            return {
                "result": "passed", "transport": "two separate real loopback HTTP workers",
                "process_death_after_durable_intent_and_native_cleanup": True,
                "peer_denies_reads_and_new_runs": True, "startup_completes_erasure": True,
                "same_operation_retry_is_idempotent": True, "other_project_preserved": True,
                "application_and_native_content_absent": True, "model_calls": 0,
                "boundary": "Temporary fixture data; no external services or user data",
            }
        finally:
            for process in processes:
                stop(process)


if __name__ == "__main__":
    print(json.dumps(exercise(), indent=2))
