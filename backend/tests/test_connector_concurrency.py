from __future__ import annotations

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from ophira_backend import connectors
from ophira_backend.connectors import ConnectorRegistry
from ophira_backend.constants import COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE
from ophira_backend.providers import FileSecretVault


def configure_browser(registry: ConnectorRegistry, port: int = 9222):
    return registry.upsert(
        "chrome_cdp", type="chrome_cdp", display_name="Local Chrome",
        scope="browser:attach", endpoint=f"http://127.0.0.1:{port}",
    )


@pytest.mark.parametrize("change", ["revoke", "endpoint", "identical_reconnect"])
def test_late_health_probe_preserves_current_connector(tmp_path, monkeypatch, change):
    root = tmp_path / "connectors"
    probing = ConnectorRegistry(root)
    editing = ConnectorRegistry(root)
    original = configure_browser(probing)
    started, release = Event(), Event()

    def probe(_endpoint):
        started.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(connectors, "_cdp_endpoint_reachable", probe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(probing.health_check, "chrome_cdp")
        try:
            assert started.wait(5)
            # These must finish while the remote health probe is still waiting.
            if change == "identical_reconnect":
                editing.revoke("chrome_cdp")
                changed = configure_browser(editing)
                assert changed.endpoint == original.endpoint
                assert changed.revision > original.revision
            else:
                changed = (editing.revoke("chrome_cdp") if change == "revoke"
                           else configure_browser(editing, 9333))
        finally:
            release.set()
        assert result.result(timeout=5) == changed
    persisted = ConnectorRegistry(root).get("chrome_cdp")
    assert persisted == changed
    assert persisted.health == "unknown"


def test_credential_save_invalidates_a_captured_health_result(tmp_path, monkeypatch):
    probing = ConnectorRegistry(
        tmp_path / "connectors", FileSecretVault(tmp_path / "vault"),
    )
    editing = ConnectorRegistry(
        tmp_path / "connectors", FileSecretVault(tmp_path / "vault"),
    )
    original = probing.upsert(
        "commercial", type=COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE, display_name="Commercial",
        endpoint="https://api.example.com", secret_env="OPHIRA_CONNECTOR_TEST_KEY",
    )
    started, release = Event(), Event()
    check_endpoint = connectors.check_commercial_https_endpoint

    def delayed_check(endpoint):
        reason = check_endpoint(endpoint)
        started.set()
        assert release.wait(5)
        return reason

    monkeypatch.setattr(connectors, "check_commercial_https_endpoint", delayed_check)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(probing.health_check, "commercial", {})
        try:
            assert started.wait(5)
            changed = editing.store_secret("commercial", "test-placeholder")
            assert changed.revision > original.revision
        finally:
            release.set()
        assert result.result(timeout=5) == changed
    assert editing.get("commercial").health == "unknown"
    assert editing.health_check("commercial", {}).health == "ok"


def _health_worker(root, started, release, results):
    def probe(_endpoint):
        started.set()
        if not release.wait(10):
            raise RuntimeError("health probe was not released")
        return True

    connectors._cdp_endpoint_reachable = probe
    try:
        result = ConnectorRegistry(Path(root)).health_check("chrome_cdp")
        results.put(("ok", result.enabled, result.health))
    except BaseException as exc:
        results.put(("error", type(exc).__name__))


def test_cross_process_revoke_survives_an_inflight_health_probe(tmp_path):
    root = tmp_path / "connectors"
    registry = ConnectorRegistry(root)
    configure_browser(registry)
    context = multiprocessing.get_context("spawn")
    started, release, results = context.Event(), context.Event(), context.Queue()
    worker = context.Process(target=_health_worker, args=(root, started, release, results))
    worker.start()
    try:
        assert started.wait(10)
        revoked = registry.revoke("chrome_cdp")
        release.set()
        assert results.get(timeout=10) == ("ok", False, "unknown")
        worker.join(timeout=10)
        assert worker.exitcode == 0
        assert registry.get("chrome_cdp") == revoked
    finally:
        release.set()
        if worker.is_alive():
            worker.terminate()
        worker.join(timeout=10)
        results.close()


def _credential_worker(root, connector_id, started, captured, release, results):
    root = Path(root)
    vault = FileSecretVault(root / "vault")
    original_read = vault._read

    def paused_read():
        values = original_read()
        captured.set()
        if release is not None and not release.wait(10):
            raise RuntimeError("credential write was not released")
        return values

    vault._read = paused_read
    registry = ConnectorRegistry(root / "connectors", vault)
    started.set()
    try:
        registry.upsert(
            connector_id, type="gmail", display_name=connector_id, secret="test-placeholder",
        )
        results.put((connector_id, "ok"))
    except BaseException as exc:
        results.put((connector_id, type(exc).__name__))


def test_concurrent_workers_retain_each_connectors_credential(tmp_path):
    context = multiprocessing.get_context("spawn")
    first_started, second_started = context.Event(), context.Event()
    first_captured, second_captured = context.Event(), context.Event()
    release, results = context.Event(), context.Queue()
    first = context.Process(target=_credential_worker, args=(
        tmp_path, "first", first_started, first_captured, release, results,
    ))
    second = context.Process(target=_credential_worker, args=(
        tmp_path, "second", second_started, second_captured, None, results,
    ))
    first.start()
    try:
        assert first_captured.wait(10)
        second.start()
        assert second_started.wait(10)
        # A second writer must not read the shared vault's old contents while
        # the first writer is paused between capture and publication.
        assert not second_captured.wait(0.25)
        release.set()
        assert sorted(results.get(timeout=10) for _ in range(2)) == [
            ("first", "ok"), ("second", "ok"),
        ]
        for worker in (first, second):
            worker.join(timeout=10)
            assert worker.exitcode == 0
        vault = FileSecretVault(tmp_path / "vault")
        assert vault.has("first") and vault.has("second")
        registry = ConnectorRegistry(tmp_path / "connectors", vault)
        assert {item.id for item in registry.list()} == {"first", "second"}
    finally:
        release.set()
        for worker in (first, second):
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=10)
        results.close()
