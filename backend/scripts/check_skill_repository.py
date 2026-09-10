"""Exercise private Skill review over loopback HTTP with a separate, temporary server.

Run from the repository root with PYTHONPATH=backend/src .venv/bin/python
backend/scripts/check_skill_repository.py. No Provider or model Run is started.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import socket
import tempfile
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx


def serve(data, listener):
    import uvicorn

    from ophira_backend.config import Settings
    from ophira_backend.main import create_app

    class NoRuns:
        async def run(self, _run):
            raise AssertionError("Private Skill management must not invoke a model")

        async def cancel(self, _run_id):
            raise AssertionError("There is no model Run to cancel")

    app = create_app(
        Settings(_env_file=None, data_dir=Path(data), auth_mode="local_only"), runtime=NoRuns()
    )
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


def archive(version):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as output:
        output.writestr(
            "private-research/SKILL.md",
            "---\nname: private-research\ndescription: Verify private supplier checks\n"
            f"version: {version}\n---\nRead references/guide.md before comparison.\n",
        )
        output.writestr("private-research/references/guide.md", "Use a current sourced quote.")
    return payload.getvalue()


def exercise():
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="ophira-private-skill-http-") as directory:
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
                    assert response.headers["cache-control"] == "no-store"
                    assert response.headers["x-content-type-options"] == "nosniff"
                    return response

                assert request("GET", "/api/skills/repository").json()["can_manage"]
                published = []
                for version in ("1.0.0", "2.0.0"):
                    path = f"/api/skills/install?request_id={uuid4().hex}"
                    payload = archive(version)
                    detail = request("POST", path, content=payload).json()
                    assert request("POST", path, content=payload).json() == detail
                    item = detail["import"]
                    assert item["state"] == "pending"
                    review_path = f"/api/skills/imports/{item['id']}"
                    preview = request("GET", review_path + "/files?path=SKILL.md").json()
                    assert f"version: {version}" in preview["text"]
                    review = {
                        "request_id": uuid4().hex,
                        "decision": "approve",
                        "revision": item["revision"],
                        "content_hash": item["skill"]["content_hash"],
                        "expected_target": detail["current_target"],
                        "note": "Human inspected the immutable HTTP fixture",
                    }
                    result = request("POST", review_path + "/review", json=review).json()
                    assert request("POST", review_path + "/review", json=review).json() == result
                    assert result["import"]["state"] == "published"
                    assert result["current_target"]["enabled"] is False
                    published.append(result)
                old, current = [item["import"]["skill"] for item in published]
                selection_path = f"/api/skills/repository/{current['name']}/selection"
                enable = {
                    "operation": "enable",
                    "content_hash": current["content_hash"],
                    "expected_target": published[1]["current_target"],
                }
                enabled = request("POST", selection_path, json=enable).json()
                assert request("POST", selection_path, json=enable).json() == enabled
                assert enabled["skill"]["enabled"]
                restore = {
                    "operation": "rollback",
                    "content_hash": old["content_hash"],
                    "expected_target": enabled["current_target"],
                }
                restored = request("POST", selection_path, json=restore).json()
                assert restored["skill"]["content_hash"] == old["content_hash"]
                assert restored["skill"]["enabled"]
                stale = client.post(selection_path, json=enable)
                assert stale.status_code == 409
                base = f"/api/skills/repository/{old['name']}/versions/{old['content_hash']}"
                downloaded = request("GET", base + "/download")
                with zipfile.ZipFile(io.BytesIO(downloaded.content)) as opened:
                    assert b"version: 1.0.0" in opened.read("SKILL.md")
                    assert opened.read("references/guide.md") == b"Use a current sourced quote."
                assert len(request("GET", f"/api/skills/{old['id']}/versions").json()) == 2
                assert len(request("GET", "/api/skills/imports").json()) == 2
                from ophira_backend.storage import FileLedger

                assert FileLedger(Path(directory)).list_runs() == []
                return {
                    "status": "passed",
                    "transport": "loopback HTTP; separate server process",
                    "reviewed_imports": 2,
                    "immutable_versions": 2,
                    "upload_and_review_retries": "same durable records",
                    "selection": "explicit enablement, historical restore, stale review rejected",
                    "export": "original historical package bytes verified",
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
