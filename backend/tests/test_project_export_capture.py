"""Archive capture preserves business data while rejecting unsafe or changing sources."""

import json
import os
from dataclasses import replace

import pytest

from ophira_backend.project_export_capture import EXPORT_LIMITS, ExportCapture, export_redact
from ophira_backend.project_export_models import ProjectExportConflict


def test_structured_redaction_keeps_business_text_and_usage_but_no_session_handles():
    text = "完整的项目正文。" * 10_000
    value = {
        "text": text,
        "usage": {"input_tokens": 123, "outputTokens": 456, "cache_read_input_tokens": 7},
        "nested": [{
            "principal_binding": {"session_id": "login-fixture"},
            "requested_session_id": "login-request-fixture",
            "staged_principal_session_id": "login-staging-fixture",
            "sdk_session_id": "sdk-fixture",
            "source_sdk_session_id": "source-sdk-fixture",
            "authorized_session_id": "browser-fixture",
            "resume_session_id": "resume-fixture",
            "API-Key": 123456,
            "password": False,
            "access_token": 99,
        }],
        "message": "Authorization: Bearer fixture-token password=fixture-password",
    }
    redacted = export_redact(value)
    assert redacted["text"] == text
    assert redacted["usage"] == value["usage"]
    assert redacted["nested"][0]["principal_binding"] == {"session_id": "[REDACTED]"}
    assert all(
        item == "[REDACTED]" for key, item in redacted["nested"][0].items()
        if key != "principal_binding"
    )
    assert "fixture" not in redacted["message"]
    assert value["nested"][0]["sdk_session_id"] == "sdk-fixture"


def test_missing_and_empty_sources_are_distinct_and_new_records_invalidate_capture(tmp_path):
    capture = ExportCapture(tmp_path)
    assert capture.names("runs") == []
    before = dict(capture.observed)
    (tmp_path / "runs").mkdir()
    fresh = ExportCapture(tmp_path)
    assert fresh.names("runs") == []
    assert fresh.observed != before
    with pytest.raises(ProjectExportConflict, match="changed"):
        capture.verify()
    (tmp_path / "runs/new.json").write_text("{}")
    with pytest.raises(ProjectExportConflict, match="changed"):
        fresh.verify()


@pytest.mark.parametrize("change", ["edit", "remove", "symlink", "hardlink"])
def test_changed_or_replaced_file_invalidates_snapshot(tmp_path, change):
    path = tmp_path / "record.json"
    path.write_text('{"prompt":"captured"}')
    capture = ExportCapture(tmp_path)
    capture.json("record.json")
    other = tmp_path / "other.json"
    other.write_text('{"prompt":"captured"}')
    if change == "edit":
        path.write_text('{"prompt":"modified"}')
    else:
        path.unlink()
        if change == "symlink":
            path.symlink_to(other)
        elif change == "hardlink":
            os.link(other, path)
    with pytest.raises(ProjectExportConflict, match="changed"):
        capture.verify()


def test_optional_file_appearing_during_capture_requires_new_preview(tmp_path):
    capture = ExportCapture(tmp_path)
    assert capture.json("comments.json", optional=True) is None
    (tmp_path / "comments.json").write_text("{}")
    with pytest.raises(ProjectExportConflict, match="changed"):
        capture.verify()


def test_workspace_bytes_and_empty_directories_are_preserved_and_each_read_is_charged(tmp_path):
    (tmp_path / "workspace/empty/nested").mkdir(parents=True)
    payload = b"\x00\x80original file bytes, api_key=synthetic-file-secret"
    (tmp_path / "workspace/file.bin").write_bytes(payload)
    capture = ExportCapture(tmp_path)
    capture.tree("workspace", "project/workspace", "workspace")
    assert capture.files == {"project/workspace/file.bin": payload}
    assert {"project/workspace/empty", "project/workspace/empty/nested"} <= capture.directories
    assert capture.read_bytes == len(payload)
    capture.verify()
    assert capture.read_bytes == len(payload) * 2
    (tmp_path / "workspace/empty/new.txt").write_text("new")
    with pytest.raises(ProjectExportConflict, match="changed"):
        capture.verify()


@pytest.mark.parametrize("kind", ["file", "directory", "hardlink", "fifo"])
def test_workspace_never_follows_links_or_opens_special_files(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    if kind == "directory":
        outside.mkdir()
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(workspace / "pipe")
    else:
        outside.write_bytes(b"excluded")
        if kind == "file":
            (workspace / "link").symlink_to(outside)
        else:
            os.link(outside, workspace / "link")
    with pytest.raises(ValueError, match="linked or special"):
        ExportCapture(tmp_path).tree("workspace", "workspace", "workspace")


def test_linked_source_ancestor_is_rejected(tmp_path):
    (tmp_path / "actual").mkdir()
    (tmp_path / "actual/record.json").write_text("{}")
    (tmp_path / "linked").symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(OSError):
        ExportCapture(tmp_path).json("linked/record.json")


@pytest.mark.parametrize("first,second", [
    ("record", "record/child"),
    ("record/child", "record"),
    ("Folder/one", "folder/two"),
    ("file", "FILE"),
])
def test_archive_rejects_file_directory_and_case_collisions(tmp_path, first, second):
    capture = ExportCapture(tmp_path)
    capture.add(first, b"one", "fixture")
    with pytest.raises(ValueError, match="conflicting"):
        capture.add(second, b"two", "fixture")


def test_export_limits_apply_across_trees_and_verification(tmp_path):
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "file").write_bytes(b"123456")
    capture = ExportCapture(tmp_path, limits=replace(EXPORT_LIMITS, total_bytes=10))
    capture.tree("first", "first", "fixture")
    with pytest.raises(ProjectExportConflict, match="total size"):
        capture.tree("second", "second", "fixture")
    capture = ExportCapture(tmp_path, limits=replace(EXPORT_LIMITS, entries=2))
    capture.names("first")
    capture.names("second")
    with pytest.raises(ProjectExportConflict):
        capture.verify()


def test_scan_capacity_bounds_unrelated_metadata_reads(tmp_path):
    (tmp_path / "record").write_bytes(b"1234")
    capture = ExportCapture(tmp_path, limits=replace(EXPORT_LIMITS, total_bytes=1))
    assert capture.read("record") == b"1234"
    with pytest.raises(ValueError, match="byte limit"):
        capture.read("record")
    assert capture.read_bytes == 4


def test_jsonl_redaction_remains_valid_and_rejects_ambiguous_json(tmp_path):
    capture = ExportCapture(tmp_path)
    capture.add_lines("events.jsonl", [{"session_id": "fixture"}, {"message": "正文"}], "events")
    assert [json.loads(line) for line in capture.files["events.jsonl"].splitlines()] == [
        {"session_id": "[REDACTED]"}, {"message": "正文"},
    ]
    (tmp_path / "record.json").write_text('{"id":1,"id":2}')
    with pytest.raises(ValueError, match="duplicate"):
        capture.json("record.json")
    with pytest.raises(ValueError, match="number"):
        export_redact({"cost": float("nan")})


@pytest.mark.parametrize("method", ["add_json", "add_lines"])
def test_serialized_record_expansion_has_an_incremental_output_limit(tmp_path, method):
    capture = ExportCapture(tmp_path, limits=replace(EXPORT_LIMITS, file_bytes=50))
    value = {"business_text": "正文" * 50}
    with pytest.raises(ProjectExportConflict, match="record size"):
        getattr(capture, method)("record.json", [value] if method == "add_lines" else value, "data")
    assert capture.files == {}


@pytest.mark.parametrize("child_file", [None, "run.json", "schedule.json", "record.json"])
def test_shared_store_selection_ignores_other_project_activity(tmp_path, child_file):
    root = tmp_path / "records"
    root.mkdir()

    def write(name, project, text):
        path = root / name
        if child_file:
            path.mkdir(exist_ok=True)
            path /= child_file
        path.write_text(json.dumps({"project_id": project, "text": text}))

    write("mine.json", "project-a", "retained")
    write("other.json", "project-b", "original")
    capture = ExportCapture(tmp_path)
    assert capture.select_json("records", "project-a", child_file=child_file) == [
        ("mine.json", {"project_id": "project-a", "text": "retained"}),
    ]
    before = dict(capture.observed)
    write("other.json", "project-b", "changed")
    write("new-other.json", "project-b", "added")
    (root / ".atomic-write.tmp").write_bytes(b"unfinished")
    capture.verify()
    assert capture.observed == before
    fresh = ExportCapture(tmp_path)
    fresh.select_json("records", "project-a", child_file=child_file)
    assert fresh.observed == before
    assert "other.json" not in json.dumps(before)


@pytest.mark.parametrize("change", ["new", "reassign", "remove", "edit"])
def test_shared_store_selection_detects_changed_project_membership(tmp_path, change):
    root = tmp_path / "records"
    root.mkdir()
    mine = root / "mine.json"
    mine.write_text('{"project_id":"project-a"}')
    other = root / "other.json"
    other.write_text('{"project_id":"project-b"}')
    capture = ExportCapture(tmp_path)
    capture.select_json("records", "project-a")
    if change == "new":
        (root / "new.json").write_text('{"project_id":"project-a"}')
    elif change == "reassign":
        other.write_text('{"project_id":"project-a"}')
    elif change == "remove":
        mine.unlink()
    else:
        mine.write_text('{"project_id":"project-a","text":"changed"}')
    with pytest.raises(ProjectExportConflict, match="changed"):
        capture.verify()


def test_selector_does_not_open_skipped_private_browser_subtrees(tmp_path):
    (tmp_path / "shops/private-session").mkdir(parents=True)
    (tmp_path / "shops/private-session/state.json").write_text("malformed private state")
    (tmp_path / "shops/shop.json").write_text('{"project_id":"project-a"}')
    capture = ExportCapture(tmp_path)
    assert len(capture.select_json("shops", "project-a", skip_non_json=True)) == 1
    capture.verify()
    assert all("private-session" not in name for name in capture.observed)
    (tmp_path / "shops/malformed.json").write_text("not a JSON record")
    with pytest.raises(ValueError):
        ExportCapture(tmp_path).select_json("shops", "project-a", skip_non_json=True)


def test_selector_io_remains_globally_bounded(tmp_path):
    (tmp_path / "records").mkdir()
    (tmp_path / "records/other.json").write_text('{"project_id":"other"}')
    capture = ExportCapture(tmp_path, limits=replace(EXPORT_LIMITS, total_bytes=1))
    with pytest.raises(ValueError, match="byte limit"):
        capture.select_json("records", "project-a")


def test_excessive_json_nesting_has_a_safe_validation_failure(tmp_path):
    (tmp_path / "record.json").write_text("[" * 2000 + "null" + "]" * 2000)
    with pytest.raises(ProjectExportConflict, match="deeply nested"):
        ExportCapture(tmp_path).json("record.json")
