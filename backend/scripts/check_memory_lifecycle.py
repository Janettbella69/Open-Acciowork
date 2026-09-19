"""Exercise memory updates/deletion through two isolated, cooperating HTTP workers.

Run from the repository root with PYTHONPATH=backend/src .venv/bin/python
backend/scripts/check_memory_lifecycle.py. All data is disposable; no model runs.
"""

from __future__ import annotations

import json
import multiprocessing
import socket
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
            raise AssertionError("Memory governance must not invoke a model")

        async def cancel(self, _run_id):
            raise AssertionError("No model run exists to cancel")

    app = create_app(settings, runtime=NoModels())
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


def request(port, method, path, *, expected_status=None, **kwargs):
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=20) as client:
        response = client.request(method, path, **kwargs)
        if expected_status is None:
            response.raise_for_status()
        else:
            assert response.status_code == expected_status, response.text
        if path.startswith("/api/memory"):
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
        return response.json()


def start(context, directory):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        process = context.Process(target=serve, args=(directory, listener))
        process.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                request(port, "GET", "/health", timeout=0.5)
                return process, port
            except httpx.TransportError:
                if not process.is_alive():
                    raise RuntimeError("Temporary memory worker exited during startup") from None
                time.sleep(0.05)
        raise RuntimeError("Temporary memory worker did not become ready")
    except BaseException:
        stop(process)
        raise


def stop(process):
    process.terminate()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=10)


def exercise():
    context = multiprocessing.get_context("spawn")
    processes = []
    with tempfile.TemporaryDirectory(prefix="ophira-memory-http-") as directory:
        try:
            ports = []
            for _ in range(2):
                process, port = start(context, directory)
                processes.append(process)
                ports.append(port)
            project = request(ports[0], "POST", "/api/projects", json={
                "name": "Memory lifecycle", "objective": "Keep concurrent memory writes consistent",
            })
            scope = {"scope": "project", "scope_id": project["id"], "kind": "fact"}

            def create(index):
                return request(ports[index % 2], "POST", "/api/memory", json={
                    **scope, "text": f"Memory lifecycle fixture [{index}]",
                })

            with ThreadPoolExecutor(max_workers=8) as pool:
                items = list(pool.map(create, range(8)))

                def correct(index):
                    return request(
                        ports[index % 2], "PATCH", f"/api/memory/{items[index]['id']}",
                        json={"text": f"Corrected lifecycle fixture [{index}]"},
                    )

                updated = list(pool.map(correct, range(8)))
                assert all(item["version"] == 2 for item in updated)

                def remove_or_renew(index):
                    identifier = items[index]["id"]
                    if index < 4:
                        return request(
                            ports[index % 2], "DELETE", f"/api/memory/{identifier}",
                            json={"reason": "Remove the selected test memory"},
                        )
                    return request(
                        ports[index % 2], "PATCH", f"/api/memory/{identifier}/expiry",
                        json={"ttl_seconds": 86400},
                    )

                transitions = list(pool.map(remove_or_renew, range(8)))
                for index in range(4):
                    assert transitions[index]["last_version"] == 2
                    assert "text" not in transitions[index]
                for index in range(4, 8):
                    assert transitions[index]["version"] == 3
                    assert transitions[index]["expires_at"] is not None
                additions = list(pool.map(create, range(8, 12)))

            expected = {item["id"] for item in items[4:] + additions}
            query = f"/api/memory/export?scope=project&scope_id={project['id']}"
            for port in ports:
                exported = request(port, "GET", query)
                assert {item["id"] for item in exported["items"]} == expected
                assert exported["count"] == 8 and exported["tombstone_count"] == 4
                assert all("principal_session_id" not in item for item in exported["items"])

            # Reconstruct stores in a new process while the other worker remains
            # live. Recovery must preserve new records and keep deleted data absent.
            stop(processes.pop())
            restarted, port = start(context, directory)
            processes.append(restarted)
            permanent = request(
                port, "PATCH", f"/api/memory/{items[4]['id']}/expiry",
                json={"clear_expiry": True},
            )
            assert permanent["version"] == 4 and permanent["expires_at"] is None
            for peer in (ports[0], port):
                exported = request(peer, "GET", query)
                assert {item["id"] for item in exported["items"]} == expected
                assert exported["tombstone_count"] == 4
                observed = next(item for item in exported["items"] if item["id"] == permanent["id"])
                assert observed["version"] == 4 and observed["expires_at"] is None
                request(
                    peer, "PATCH", f"/api/memory/{items[0]['id']}",
                    json={"text": "Cannot resurrect deleted memory"}, expected_status=404,
                )
                request(
                    peer, "POST", f"/api/memory/{items[0]['id']}/rollback",
                    json={"version": 1}, expected_status=404,
                )
            folder = Path(directory) / "memory" / "project" / project["id"]
            text = "\n".join(path.read_text() for path in folder.iterdir() if path.is_file())
            for index in range(4):
                assert items[index]["text"] not in text
                assert updated[index]["text"] not in text
            assert request(port, "GET", f"/api/projects/{project['id']}/runs") == []
            return {
                "result": "passed", "transport": "two separate loopback HTTP server processes",
                "concurrent_creates": 8, "concurrent_corrections": 8,
                "concurrent_deletes_and_renewals": 8, "subsequent_creates": 4,
                "retained_memories": 8, "deletion_audits": 4,
                "private_response_headers": True, "restart_preserves_deletions": True,
                "restart_mutations_visible_to_both_workers": True,
                "deleted_memory_edit_and_rollback_rejected": True,
                "deleted_content_absent_from_snapshot_and_history": True,
                "model_runs": 0,
                "boundary": "Disposable local data; no Provider or external service",
            }
        finally:
            for process in processes:
                stop(process)


if __name__ == "__main__":
    print(json.dumps(exercise(), indent=2))
