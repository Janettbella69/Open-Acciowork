"""Exercise reviewed ZIP exports over loopback HTTP using a disposable installation.

Run from the repository root: PYTHONPATH=backend/src .venv/bin/python
backend/scripts/check_project_export.py. No model or external connector is called.
"""

from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import socket
import tempfile
import time
import zipfile
from pathlib import Path

import httpx


def serve(data, listener):
    import uvicorn

    from ophira_backend import config, web_tools

    settings = config.Settings(
        _env_file=None, data_dir=Path(data), auth_mode="local_only",
        anthropic_api_key=None, anthropic_auth_token=None, anthropic_base_url=None,
    )
    # main also constructs its default ASGI app at import time. Keep that app
    # in this same disposable installation, away from operator data/credentials.
    config.get_settings = lambda: settings
    # Identity validation checks DNS but never opens a seller page. Resolve the
    # synthetic identity deterministically so the check needs no public DNS.
    web_tools._resolve_ips = lambda host: ["93.184.216.34"] if host == "admin.shopify.com" else []
    from ophira_backend.main import create_app

    class NoRuns:
        async def run(self, _run):
            raise AssertionError("Project export must not invoke a model")

        async def cancel(self, _run_id):
            raise AssertionError("There is no model Run to cancel")

    app = create_app(settings, runtime=NoRuns())
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


def exercise():
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="ophira-export-http-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            server = context.Process(target=serve, args=(directory, listener))
            server.start()
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=20, trust_env=False,
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

                project = request("POST", "/api/projects", json={
                    "name": "Portable project", "objective": "Retain exact project references",
                }).json()
                base = f"/api/projects/{project['id']}"
                source = "Supplier,price\n测试,120\n".encode()
                artifact = request(
                    "POST", base + "/files?filename=reference.csv&mime=text/csv", content=source,
                ).json()
                # The catalog is created lazily when the UI opens its connector list.
                request("GET", "/api/connectors")
                shop = request("POST", base + "/shops", json={
                    "name": "Shopify identity", "platform": "shopify", "seller_shop_id": "shop-1",
                    "connector_id": "shopify", "entry_url": "https://admin.shopify.com/store/test",
                }).json()
                assert shop["browser_state"] == "closed" and not shop["agent_access"]
                assert request("GET", base + "/marketplace-access").json()["bindings"] == []
                preview = request("GET", base + "/export").json()
                artifact = request(
                    "POST", base + "/files?filename=second.txt&mime=text/plain",
                    content=b"Another exact reference",
                ).json()
                stale = client.post(
                    base + "/export/download", json={"fingerprint": preview["fingerprint"]},
                )
                assert stale.status_code == 409
                preview = request("GET", base + "/export").json()
                archive = request(
                    "POST", base + "/export/download",
                    json={"fingerprint": preview["fingerprint"]},
                )
                assert archive.headers["cache-control"] == "no-store"
                assert archive.headers["content-type"] == "application/zip"
                with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
                    manifest = json.loads(package.read("manifest.json"))
                    assert manifest["fingerprint"] == preview["fingerprint"]
                    contents = []
                    for entry in preview["entries"]:
                        raw = package.read(entry["path"])
                        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
                        assert len(raw) == entry["size_bytes"]
                        contents.append(raw)
                    assert source in contents and b"Another exact reference" in contents
                    assert any(artifact["id"].encode() in raw for raw in contents)
                    assert any(b"Shopify identity" in raw for raw in contents)
                runs = request("GET", base + "/runs").json()
                assert runs == []
                return {
                    "result": "passed", "transport": "loopback HTTP / separate server process",
                    "files": preview["file_count"], "bytes": preview["total_bytes"],
                    "archive_sha256": hashlib.sha256(archive.content).hexdigest(),
                    "stale_preview_rejected": True, "exact_bytes_and_hashes": True,
                    "shopify_identity_without_access_grant": True, "model_runs": len(runs),
                    "boundary": (
                        "Disposable local data and fixture seller DNS; "
                        "no live Provider or seller account"
                    ),
                }
        finally:
            server.terminate()
            server.join(timeout=10)
            if server.is_alive():
                server.kill()
                server.join(timeout=10)


if __name__ == "__main__":
    print(json.dumps(exercise(), indent=2))
