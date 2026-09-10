"""Exercise template HTTP routes against a separate local server with temporary data.

Run from the repository root: PYTHONPATH=backend/src .venv/bin/python
backend/scripts/check_project_templates.py. This script never starts a model Run.
"""

from __future__ import annotations

import json
import multiprocessing
import socket
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx


def serve(data, listener):
    import uvicorn

    from ophira_backend.config import Settings
    from ophira_backend.main import create_app

    class NoRuns:
        async def run(self, _run):
            raise AssertionError("Template creation must not invoke a model")

        async def cancel(self, _run_id):
            raise AssertionError("There is no model Run to cancel")

    app = create_app(
        Settings(_env_file=None, data_dir=Path(data), auth_mode="local_only"), runtime=NoRuns()
    )
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


def exercise():
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="ophira-template-http-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            server = context.Process(target=serve, args=(directory, listener))
            server.start()
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False
            ) as client:
                for _ in range(100):
                    try:
                        if client.get("/health").is_success:
                            break
                    except httpx.TransportError:
                        pass
                    if not server.is_alive():
                        raise RuntimeError("Temporary server exited before readiness")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Temporary server did not become ready")

                def request(method, path, **kwargs):
                    response = client.request(method, path, **kwargs)
                    response.raise_for_status()
                    return response

                project = request(
                    "POST",
                    "/api/projects",
                    json={"name": "HTTP source", "objective": "Review a fresh supplier"},
                ).json()
                prefix = f"/api/projects/{project['id']}"
                artifact = request(
                    "POST",
                    prefix + "/files?filename=reference.txt&mime=text/plain",
                    content=b"Original fixed quote",
                    headers={"Content-Type": "text/plain"},
                ).json()
                seed = {
                    "name": "Fresh opportunity",
                    "objective": "Obtain current quotes",
                    "product_brief": "Use the selected reference",
                    "target_markets": ["Japan"],
                    "constraints": ["Verify current MOQ"],
                }
                payload = {
                    "request_id": uuid4().hex,
                    "title": "Supplier template",
                    "seed": seed,
                    "artifact_ids": [artifact["id"]],
                }
                base = prefix + "/templates"
                template = request("POST", base, json=payload).json()
                path = base + "/" + template["id"]
                assert request("GET", base).json()["items"][0]["id"] == template["id"]
                fixed = request("GET", path + "/versions/1").json()["version"]
                download = request(
                    "GET", path + f"/versions/1/references/{artifact['id']}/download"
                )
                assert download.content == b"Original fixed quote"
                assert download.headers["cache-control"] == "no-store"
                update = {
                    **payload,
                    "request_id": uuid4().hex,
                    "expected_revision": 1,
                    "title": "Updated template",
                    "artifact_ids": [],
                }
                assert (
                    request("POST", path + "/versions", json=update).json()["latest_version"] == 2
                )
                assert request("GET", path + "/versions/1").json()["version"] == fixed
                apply = {
                    "request_id": uuid4().hex,
                    "version": 1,
                    "content_hash": fixed["content_hash"],
                    "seed": {**seed, "name": "Reviewed HTTP project"},
                    "artifact_ids": [artifact["id"]],
                }
                created = request("POST", path + "/applications", json=apply).json()
                repeated = request("POST", path + "/applications", json=apply).json()
                assert repeated["project"]["id"] == created["project"]["id"]
                target = created["project"]
                assert target["name"] == apply["seed"]["name"]
                assert target["evidence"] == target["decisions"] == target["hypotheses"] == []
                assert request("GET", f"/api/projects/{target['id']}/runs").json() == []
                copied = request(
                    "GET",
                    f"/api/projects/{target['id']}/artifacts/{target['artifacts'][0]['id']}/download",
                )
                assert copied.content == b"Original fixed quote"
                assert (
                    request("GET", path + "/applications").json()["items"][0]["state"] == "applied"
                )
                archived = request(
                    "POST",
                    path + "/archive",
                    json={"request_id": uuid4().hex, "expected_revision": 2, "archived": True},
                ).json()
                assert archived["archived"]
                rejected = client.post(
                    path + "/applications", json={**apply, "request_id": uuid4().hex}
                )
                assert (
                    rejected.status_code == 409 and rejected.headers["cache-control"] == "no-store"
                )
                assert len(request("GET", "/api/projects").json()) == 2
                return {
                    "status": "passed",
                    "transport": "loopback HTTP; separate server process",
                    "projects": 2,
                    "template_versions": 2,
                    "copied_references": 1,
                    "model_runs": 0,
                    "data": "temporary fixture removed after process exit",
                }
        finally:
            if server.is_alive():
                server.terminate()
            server.join(10)
            if server.is_alive():
                server.kill()
                server.join(5)


if __name__ == "__main__":
    print(json.dumps(exercise(), ensure_ascii=False, indent=2))
