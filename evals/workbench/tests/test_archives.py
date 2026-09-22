"""Offline archive/API contracts; all annotations below use synthetic temporary data."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acciowork_eval.api import app, get_store
from acciowork_eval.config import WORKBENCH_ROOT, database_path
from acciowork_eval.contract import JUDGES
from acciowork_eval.importer import digest
from acciowork_eval.store import Conflict, Store


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def archive(tmp_path):
    report = tmp_path / "reports" / "l2-synthetic" / "overall.json"
    runs = tmp_path / "runs"
    samples = tmp_path / "gold" / "samples.json"
    run_id = "run_synthetic"
    write(
        report,
        {
            "provider": "synthetic",
            "results": [
                {"task": "case-one", "run_id": run_id, "passed": True},
            ],
        },
    )
    write(
        report.parent / "case-one" / "summary.json",
        {
            "run": {"id": run_id, "status": "completed", "trace_id": "trace_synthetic"},
        },
    )
    write(
        runs / run_id / "run.json",
        {
            "id": run_id,
            "trace_id": "trace_synthetic",
            "status": "completed",
            "prompt": "Synthetic task",
            "provider_snapshot": {"model": "test-model"},
            "skill_snapshot": [],
        },
    )
    events = [
        {
            "id": f"{run_id}:3",
            "sequence": 3,
            "run_id": run_id,
            "type": "run.result",
            "data": {
                "result": "Synthetic result",
                "duration_ms": 0,
                "total_cost_usd": 0,
                "cost_basis": "synthetic",
                "is_error": False,
            },
        },
        {
            "id": f"{run_id}:1",
            "sequence": 1,
            "run_id": run_id,
            "type": "tool.started",
            "data": {"tool_name": "Read", "tool_input": {"path": "fixture.csv"}},
        },
        {
            "id": f"{run_id}:2",
            "sequence": 2,
            "run_id": run_id,
            "type": "assistant.text.delta",
            "data": {"text": "100%_synthetic"},
        },
    ]
    event_path = runs / run_id / "events.jsonl"
    raw = ("\n".join(json.dumps(e) for e in events) + "\n").encode()
    event_path.write_bytes(raw)
    write(
        samples,
        {
            "generated_at": "2026-01-01T00:00:00Z",
            "runs": {
                run_id: {
                    "evidence": [{"id": "ev_1", "kind": "inference", "claim": "Synthetic claim"}],
                    "artifacts": [{"name": "synthetic.md", "text": "# Synthetic artifact"}],
                }
            },
            "items": [
                {
                    "item_id": "item_synthetic",
                    "run_id": run_id,
                    "judge": "fact-inference-confusion",
                    "split": "test",
                    "item": "Synthetic claim",
                    "source": "ledger:ev_1",
                }
            ],
        },
    )
    return {
        "report": report,
        "runs_dir": runs,
        "samples": samples,
        "event_path": event_path,
        "raw": raw,
    }


def import_one(store, archive):
    result = store.import_report(archive["report"], archive["runs_dir"], archive["samples"])
    return result, store.traces(result["id"])[0]["id"]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "eval" / "archives.db")


def test_import_is_idempotent_copies_original_bytes_and_does_not_invent_scores(store, archive):
    result, trace_id = import_one(store, archive)
    repeated, _ = import_one(store, archive)
    assert result["created"] and not repeated["created"]
    assert len(store.traces()) == len(store.experiments()) == 1
    trace = store.trace(trace_id)
    assert trace["execution_status"] == "completed"
    assert trace["legacy_passed"] is True
    assert trace["snapshot_accepted"] is None
    assert trace["quality_status"] == "unlabeled"
    assert trace["cost_usd"] == trace["duration_ms"] == 0
    assert trace["evidence_source"] == "gold_batch"
    assert trace["snapshot"] == {}
    assert trace["versions"]["fixture_hash"] is None
    assert store.file(digest(archive["raw"])) == archive["raw"]
    assert archive["event_path"].read_bytes() == archive["raw"]
    archive["event_path"].unlink()
    assert store.events(trace_id)["total"] == 2
    assert store.file(digest(archive["raw"])) == archive["raw"]


def test_events_are_sorted_filtered_paginated_and_keep_tool_data(store, archive):
    _, trace_id = import_one(store, archive)
    events = store.events(trace_id, kind="all")
    assert [e["sequence"] for e in events["events"]] == [1, 2, 3]
    assert store.events(trace_id, offset=1, limit=1)["events"][0]["sequence"] == 3
    assert store.events(trace_id, kind="tool.started")["events"][0]["data"]["tool_input"] == {
        "path": "fixture.csv"
    }
    assert store.events(trace_id, query="100%_", kind="all")["total"] == 1
    assert store.events(trace_id, sequence=2)["events"][0]["id"] == "run_synthetic:2"


@pytest.mark.parametrize("content", [b'{"invalid":\n', b"null\n", b"[]\n", b"\xff\n"])
def test_corrupt_logs_are_visible_and_preserved(store, archive, content):
    archive["event_path"].write_bytes(archive["raw"] + content)
    _, trace_id = import_one(store, archive)
    trace = store.trace(trace_id)
    ref = trace["sources"]["events"]
    assert ref["status"] == "corrupt" and ref["invalid_count"] == 1
    assert store.file(ref["sha256"]) == archive["raw"] + content
    assert trace["event_count"] == 3


def test_foreign_run_events_and_duplicate_sequences_are_not_silently_accepted(store, archive):
    lines = archive["raw"].splitlines()
    foreign = json.loads(lines[0])
    foreign["run_id"] = "run_other"
    archive["event_path"].write_bytes(
        archive["raw"] + lines[0] + b"\n" + json.dumps(foreign).encode()
    )
    _, trace_id = import_one(store, archive)
    source = store.trace(trace_id)["sources"]["events"]
    assert source["invalid_count"] == 1 and source["sequence_warning"]
    assert store.events(trace_id, sequence=3)["total"] == 2


def test_missing_log_and_metadata_remain_unknown(store, archive):
    archive["event_path"].unlink()
    _, trace_id = import_one(store, archive)
    trace = store.trace(trace_id)
    assert trace["event_count"] == 0 and trace["cost_usd"] is None
    assert trace["duration_ms"] is None
    assert trace["sources"]["events"]["status"] == "missing"
    assert trace["snapshot_accepted"] is None


def test_empty_log_and_truncated_tail_are_visible(store, archive):
    archive["event_path"].write_bytes(b"")
    _, trace_id = import_one(store, archive)
    assert store.trace(trace_id)["sources"]["events"]["status"] == "empty"
    archive["event_path"].write_bytes(archive["raw"])
    summary_path = archive["report"].parent / "case-one" / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["event_count"] = 9
    write(summary_path, summary)
    _, trace_id = import_one(store, archive)
    assert "imported 3" in store.trace(trace_id)["sources"]["events"]["sequence_warning"]


@pytest.mark.parametrize("task", ["../outside", "/absolute", "..", "folder\\escape"])
def test_rejects_path_traversal(store, archive, task):
    write(archive["report"], {"results": [{"task": task, "run_id": "run_synthetic"}]})
    with pytest.raises(ValueError):
        import_one(store, archive)
    assert not store.experiments()


def test_symlink_escape_is_not_read(store, archive, tmp_path):
    secret = tmp_path / "outside.jsonl"
    secret.write_bytes(b"this content must never be imported")
    archive["event_path"].unlink()
    archive["event_path"].symlink_to(secret)
    _, trace_id = import_one(store, archive)
    assert store.trace(trace_id)["sources"]["events"]["status"] == "unreadable"
    with pytest.raises(KeyError):
        store.file(digest(secret.read_bytes()))


def test_gold_label_direction_persistence_export_and_concurrent_edit(store, archive):
    _, trace_id = import_one(store, archive)
    saved = store.save_label(
        "item_synthetic",
        label=True,
        rationale="Synthetic owner rationale",
        revision=0,
        owner_confirmed=True,
    )
    assert saved["annotation"]["label"] is True
    assert JUDGES["fact-inference-confusion"]["positive_is_failure"] is True
    assert not JUDGES["evidence-coverage"]["positive_is_failure"]
    refreshed = Store(store.path)
    assert refreshed.trace(trace_id)["quality_status"] == "labeled"
    record = json.loads(refreshed.export_labels("fact-inference-confusion"))
    for field in (
        "item_id",
        "run_id",
        "item",
        "judge",
        "source",
        "split",
        "batch",
        "rationale",
        "labeled_at",
    ):
        assert field in record
    assert record["split"] == "test"
    with pytest.raises(Conflict):
        store.save_label(
            "item_synthetic", label=False, rationale="Stale", revision=0, owner_confirmed=True
        )
    import_one(store, archive)
    assert store.trace(trace_id)["items"][0]["annotation"]["label"] is True
    assert not list(archive["samples"].parent.glob("**/labels.jsonl"))


@pytest.mark.parametrize(
    "label, owner, rationale",
    [("false", True, "reason"), (1, True, "reason"), (True, False, "reason"), (False, True, " ")],
)
def test_requires_explicit_owner_boolean_and_reason(store, archive, label, owner, rationale):
    import_one(store, archive)
    with pytest.raises(ValueError):
        store.save_label(
            "item_synthetic", label=label, rationale=rationale, revision=0, owner_confirmed=owner
        )


def test_conflicting_batch_split_rolls_back_import(store, archive):
    import_one(store, archive)
    batch = json.loads(archive["samples"].read_text())
    batch["items"][0]["split"] = "train"
    write(archive["samples"], batch)
    with pytest.raises(ValueError, match="Conflicting Gold"):
        import_one(store, archive)
    assert len(store.experiments()) == 1


def test_existing_gold_import_is_lossless_idempotent_and_refuses_conflicts(
    store, archive, tmp_path
):
    _, trace_id = import_one(store, archive)
    record = {
        **json.loads(archive["samples"].read_text())["items"][0],
        "label": False,
        "rationale": "Synthetic existing human annotation",
        "labeled_at": "2026-01-01T00:00:00Z",
        "batch": "samples.json",
    }
    labels = tmp_path / "existing-labels.jsonl"
    labels.write_text(json.dumps(record) + "\n")
    assert store.import_labels(labels)["imported"] == 1
    assert store.import_labels(labels)["imported"] == 0
    assert json.loads(store.export_labels(record["judge"])) == record
    assert store.trace(trace_id)["items"][0]["annotation"]["label"] is False
    record["label"] = True
    labels.write_text(json.dumps(record) + "\n")
    with pytest.raises(Conflict):
        store.import_labels(labels)


def test_foreign_snapshot_is_not_presented_as_this_runs_frozen_input(store, archive):
    bundle = archive["report"].parent / "case-one"
    write(bundle / "snapshot.json", {"output": {"run": {"id": "run_other"}}})
    write(bundle / "snapshot-eval.json", {"accepted": True})
    _, trace_id = import_one(store, archive)
    trace = store.trace(trace_id)
    assert trace["snapshot_accepted"] is None and trace["snapshot"] == {}
    assert trace["sources"]["snapshot"]["status"] == "corrupt"


@pytest.mark.parametrize(
    "name, payload",
    [
        ("summary.json", {"run": None}),
        ("snapshot.json", {"schema_version": "1", "output": None}),
        ("snapshot.json", {"schema_version": "1", "output": []}),
    ],
)
def test_null_or_malformed_nested_metadata_does_not_crash_archive_browser(
    store, archive, name, payload
):
    write(archive["report"].parent / "case-one" / name, payload)
    _, trace_id = import_one(store, archive)
    trace = store.trace(trace_id)
    assert trace["execution_status"] == "completed"
    assert trace["snapshot_accepted"] is None


def test_open_codes_do_not_create_gold_labels_and_survive_refresh(store, archive):
    _, trace_id = import_one(store, archive)
    store.save_review(
        trace_id,
        status="flagged",
        open_codes=["source-gap", "source-gap"],
        notes="Synthetic note",
        revision=0,
    )
    trace = Store(store.path).trace(trace_id)
    assert trace["review"]["open_codes"] == ["source-gap"]
    assert trace["review"]["status"] == "flagged"
    assert trace["items"][0]["annotation"] is None
    with pytest.raises(Conflict):
        store.save_review(trace_id, status="reviewed", open_codes=[], notes="Stale", revision=0)


def test_comparison_unknown_versions_and_added_cases(store, archive):
    left, _ = import_one(store, archive)
    other = archive["report"].parent.parent / "l2-other" / "overall.json"
    write(
        other,
        {
            "results": [
                {"task": "case-one", "run_id": "run_synthetic", "passed": False},
                {"task": "case-new", "run_id": "run_new", "passed": False},
            ]
        },
    )
    right = store.import_report(other, archive["runs_dir"])
    rows = {r["case_key"]: r for r in store.compare(left["id"], right["id"])["cases"]}
    assert rows["case-one"]["change"] == "unknown"
    assert not rows["case-one"]["comparable"]
    assert rows["case-new"]["change"] == "added"
    assert store.compare(right["id"], left["id"])["cases"][0]["change"] == "removed"


def test_snapshot_findings_frozen_input_and_regression_require_version_evidence(store, archive):
    bundle = archive["report"].parent / "case-one"
    snapshot = {"schema_version": "1", "output": {"run": {"id": "run_synthetic"}, "artifacts": []}}
    write(bundle / "snapshot.json", snapshot)
    write(bundle / "snapshot-eval.json", {"accepted": True, "profile": "test", "findings": []})
    report = json.loads(archive["report"].read_text())
    report["results"][0].update(fixture_hash="frozen-fixture", grader_version="grader-1")
    write(archive["report"], report)
    left, _ = import_one(store, archive)
    write(
        bundle / "snapshot-eval.json",
        {
            "accepted": False,
            "profile": "test",
            "findings": [
                {
                    "code": "artifact.missing",
                    "path": "output.artifacts",
                    "message": "Synthetic finding",
                },
            ],
        },
    )
    right, trace_id = import_one(store, archive)
    assert left["id"] != right["id"]
    row = store.compare(left["id"], right["id"])["cases"][0]
    assert row["comparable"] and row["change"] == "regressed"
    trace = store.trace(trace_id)
    assert trace["snapshot"] == snapshot
    assert trace["artifacts"] == [] and trace["evidence"] == []
    assert trace["snapshot_eval"]["findings"][0]["path"] == "output.artifacts"


def test_http_contract_bounds_owner_protection_and_conflict(store, archive):
    _, trace_id = import_one(store, archive)
    app.dependency_overrides[get_store] = lambda: store
    try:
        with TestClient(app) as client:
            assert client.get("/api/acciowork/experiments").status_code == 200
            assert client.get("/api/acciowork/traces/missing").status_code == 404
            assert (
                client.get(f"/api/acciowork/traces/{trace_id}/events?limit=201").status_code == 422
            )
            assert (
                client.get(
                    "/api/acciowork/experiments", headers={"Host": "attacker.example"}
                ).status_code
                == 403
            )
            body = {
                "label": True,
                "rationale": "Synthetic UI check",
                "revision": 0,
                "owner_confirmed": True,
            }
            url = "/api/acciowork/items/item_synthetic/label"
            assert client.put(url, json=body).status_code == 403
            headers = {"X-Eval-Review": "owner-ui", "Origin": "http://attacker.example"}
            assert client.put(url, json=body, headers=headers).status_code == 403
            headers["Origin"] = "http://localhost:5201"
            assert (
                client.put(url, json={**body, "label": "false"}, headers=headers).status_code == 422
            )
            assert client.put(url, json=body, headers=headers).status_code == 200
            assert client.put(url, json=body, headers=headers).status_code == 409
            export = client.get("/api/acciowork/labels/fact-inference-confusion/export")
            assert export.status_code == 200 and export.json()["label"] is True
            assert "attachment" in export.headers["content-disposition"]
    finally:
        app.dependency_overrides.clear()


def test_relocated_archive_preserves_reviews_and_serves_without_originals(store, archive, tmp_path):
    result, trace_id = import_one(store, archive)
    store.save_label(
        "item_synthetic",
        label=False,
        rationale="Synthetic retained rationale",
        revision=0,
        owner_confirmed=True,
    )
    store.save_review(
        trace_id,
        status="flagged",
        open_codes=["source-gap"],
        notes="Retain review",
        revision=0,
    )
    expected_label = store.export_labels("fact-inference-confusion")
    expected_trace = store.trace(trace_id)
    destination = tmp_path / "independent-workbench" / "archive.sqlite3"
    destination.parent.mkdir()
    with store.connect() as source, sqlite3.connect(destination) as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        expected_history = source.execute("SELECT count(*) FROM review_history").fetchone()[0]
        assert (
            target.execute("SELECT count(*) FROM review_history").fetchone()[0] == expected_history
        )
    for directory in (
        store.path.parent,
        archive["report"].parent.parent,
        archive["runs_dir"],
        archive["samples"].parent,
    ):
        shutil.rmtree(directory)
    relocated = Store(destination)
    assert relocated.trace(trace_id) == expected_trace
    assert relocated.export_labels("fact-inference-confusion") == expected_label
    assert relocated.file(digest(archive["raw"])) == archive["raw"]
    app.dependency_overrides[get_store] = lambda: relocated
    try:
        with TestClient(app) as client:
            assert client.get("/api/acciowork/experiments").json()[0]["id"] == result["id"]
            assert (
                client.get(f"/api/acciowork/traces/{trace_id}/events?kind=all").json()["total"] == 3
            )
            assert (
                client.get("/api/acciowork/labels/fact-inference-confusion/export").text
                == expected_label
            )
    finally:
        app.dependency_overrides.clear()


def test_default_database_is_local_to_workbench_and_ignores_other_projects(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCIOWORK_EVAL_DB", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "other-eval.db"))
    monkeypatch.setenv("OPHIRA_DATA_DIR", str(tmp_path / "product-data"))
    monkeypatch.chdir(tmp_path)
    assert database_path() == WORKBENCH_ROOT / "data/archive.sqlite3"
    assert database_path().is_absolute()
    assert not list(tmp_path.iterdir())


def test_review_can_be_edited_after_refresh_without_resending_audit_fields(store, archive):
    _, trace_id = import_one(store, archive)
    app.dependency_overrides[get_store] = lambda: store
    headers = {"X-Eval-Review": "owner-ui", "Origin": "http://localhost:5201"}
    url = f"/api/acciowork/traces/{trace_id}/review"
    try:
        with TestClient(app) as client:
            first = client.put(
                url,
                headers=headers,
                json={
                    "status": "flagged",
                    "open_codes": ["source-gap"],
                    "notes": "First synthetic review",
                    "revision": 0,
                },
            )
            assert first.status_code == 200
            refreshed = client.get(f"/api/acciowork/traces/{trace_id}").json()["review"]
            assert refreshed["revision"] == 1
            assert refreshed["reviewed_at"] and refreshed["annotator"]
            editable = {
                key: refreshed[key] for key in ("status", "open_codes", "notes", "revision")
            }
            editable.update(status="reviewed", notes="Second synthetic review")
            second = client.put(url, headers=headers, json=editable)
            assert second.status_code == 200
            final = client.get(f"/api/acciowork/traces/{trace_id}").json()["review"]
            assert final["revision"] == 2 and final["notes"] == "Second synthetic review"
            assert client.put(url, headers=headers, json=editable).status_code == 409
            assert client.put(url, headers=headers, json=final).status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_import_runs_in_isolated_copy_with_own_database(archive, tmp_path, explicit):
    isolated = tmp_path / "independent-workbench"
    shutil.copytree(
        WORKBENCH_ROOT / "backend",
        isolated / "backend",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(isolated / "backend")
    env["ACCIOWORK_EVAL_DB"] = str(tmp_path / "env-archive.sqlite3")
    env["DB_PATH"] = str(tmp_path / "other-project.sqlite3")
    args = [sys.executable, "-m", "acciowork_eval"]
    target = Path(env["ACCIOWORK_EVAL_DB"])
    if explicit:
        target = tmp_path / "explicit-archive.sqlite3"
        args.extend(["--db", str(target)])
    args.extend(
        [
            "import",
            "--report",
            str(archive["report"]),
            "--runs-dir",
            str(archive["runs_dir"]),
            "--samples",
            str(archive["samples"]),
        ]
    )
    first = subprocess.run(
        args, cwd=unrelated_cwd, env=env, capture_output=True, text=True, check=True
    )
    second = subprocess.run(
        args, cwd=unrelated_cwd, env=env, capture_output=True, text=True, check=True
    )
    assert json.loads(first.stdout)["created"] is True
    assert json.loads(second.stdout)["created"] is False
    assert len(Store(target).experiments()) == 1
    assert not Path(env["DB_PATH"]).exists()
    assert not (isolated / "data").exists()
    assert not list(unrelated_cwd.iterdir())
    if explicit:
        assert not Path(env["ACCIOWORK_EVAL_DB"]).exists()
