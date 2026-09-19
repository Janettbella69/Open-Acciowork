"""Native deletion fixtures never touch real configuration or invoke a model."""

import hashlib
import json
import os
import pty
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from ophira_backend.models import RunRecord
from ophira_backend.project_session_cleanup import (
    ProjectSessionCleanup,
    SessionCleanupConflict,
    SessionCleanupPlan,
)


def native_session(root, project="project_a", provider="environment", sid=None, dirname=None):
    sid = sid or str(uuid4())
    cwd = root / "projects" / project / "workspace"
    cwd.mkdir(parents=True, exist_ok=True)
    config = root / "runtime-config" / provider
    directory = config / "projects" / (dirname or re.sub(r"[^a-zA-Z0-9]", "-", str(cwd)))
    directory.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "cwd": str(cwd), "sessionId": sid,
             "uuid": str(uuid4()), "message": {"role": "user", "content": "private fixture"}}]
    (directory / f"{sid}.jsonl").write_text(json.dumps(rows[0]) + "\n")
    run = RunRecord(project_id=project, prompt="temporary fixture", sdk_session_id=sid,
                    provider_profile_id=None if provider == "environment" else provider)
    return run, config, directory


def write(path, data=b"private fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_native_sdk_erases_only_reviewed_project_sessions_and_checkpoints(tmp_path, monkeypatch):
    root = tmp_path / "data"
    run, config, directory = native_session(root)
    other, _, other_dir = native_session(root, "project_b")
    second, other_config, second_dir = native_session(root, provider="provider_b")
    resumed = run.model_copy(update={"id": "run_resume", "resume_session_id": run.sdk_session_id})
    sid = run.sdk_session_id
    write(directory / sid / "subagents" / "agent-fixture.jsonl")
    write(directory / sid / "tool-results" / "result.txt")
    write(directory / f"{sid}.orphaned-20260919-fixture.jsonl",
          (directory / f"{sid}.jsonl").read_bytes())
    write(directory / f"{sid}.jsonl.superseded-20260919",
          (directory / f"{sid}.jsonl").read_bytes())
    write(config / "file-history" / sid / "0123456789abcdef@v1")
    write(config / "debug" / f"{sid}.txt")
    write(config / "tasks" / sid / "1.json")
    write(config / "session-env" / sid / "env")
    write(config / "image-cache" / sid / "image.png")
    write(config / "uploads" / sid / "source.pdf")
    write(config / "todos" / f"{sid}-agent-fixture.json")
    write(config / ".credentials.json", b"must never read or delete")
    outsider = (other_dir / f"{other.sdk_session_id}.jsonl").read_bytes()
    cleanup = ProjectSessionCleanup(root)
    runs = [run, second, resumed]
    plan = cleanup.preview(run.project_id, runs, [*runs, other])
    assert not plan.blockers
    assert len(plan.session_groups) == 2
    assert sorted(plan.session_groups[0].run_ids) == sorted([run.id, resumed.id])
    assert "private fixture" not in plan.model_dump_json()
    restored = SessionCleanupPlan.model_validate_json(plan.model_dump_json())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "unrelated-home"))
    actual_run = subprocess.run
    envs = []

    def inspect(*args, **kwargs):
        envs.append(kwargs["env"])
        return actual_run(*args, **kwargs)

    monkeypatch.setattr("ophira_backend.project_session_cleanup.subprocess.run", inspect)
    result = cleanup.erase(restored)
    assert result.complete and result.erased_files == len(plan.relative_files)
    assert all("ANTHROPIC_API_KEY" not in env for env in envs)
    assert os.environ["ANTHROPIC_API_KEY"] == "must-not-inherit"
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(tmp_path / "unrelated-home")
    assert not directory.exists() and not second_dir.exists()
    assert (other_dir / f"{other.sdk_session_id}.jsonl").read_bytes() == outsider
    assert (config / ".credentials.json").read_bytes() == b"must never read or delete"
    assert other_config.exists()
    assert cleanup.erase(restored).complete


def test_long_cwd_native_hash_and_unrecorded_session_are_discovered(tmp_path):
    root = tmp_path / ("a" * 80) / ("b" * 80) / "data"
    cwd = str(root / "projects" / "project_a" / "workspace")
    dirname = re.sub(r"[^a-zA-Z0-9]", "-", cwd)[:200] + "-bun-native-hash"
    run, _, directory = native_session(root, dirname=dirname)
    orphan_id = str(uuid4())
    native_session(root, sid=orphan_id, dirname=dirname)
    cleanup = ProjectSessionCleanup(root)
    plan = cleanup.preview(run.project_id, [run], [run])
    assert not plan.blockers
    assert {group.session_id for group in plan.session_groups} == {run.sdk_session_id, orphan_id}
    assert cleanup.erase(plan).complete
    assert not directory.exists()


def test_missing_config_and_no_sessions_are_idempotent(tmp_path):
    cleanup = ProjectSessionCleanup(tmp_path)
    run = RunRecord(project_id="project_a", prompt="never started")
    plan = cleanup.preview(run.project_id, [run], [run])
    assert not plan.blockers and not plan.relative_files
    assert cleanup.erase(plan).complete


@pytest.mark.parametrize("mutation", ["change", "addition", "foreign_duplicate", "global_copy"])
def test_changed_preview_never_erases_anything(tmp_path, mutation):
    run, config, directory = native_session(tmp_path)
    cleanup = ProjectSessionCleanup(tmp_path)
    plan = cleanup.preview(run.project_id, [run], [run])
    main = directory / f"{run.sdk_session_id}.jsonl"
    if mutation == "change":
        main.write_text(main.read_text() + "{}\n")
    elif mutation == "addition":
        write(directory / run.sdk_session_id / "subagents" / "late.jsonl")
    elif mutation == "foreign_duplicate":
        write(config / "projects" / "foreign-cwd" / main.name, main.read_bytes())
    else:
        write(config / "dump-prompts" / "late.jsonl")
    with pytest.raises(SessionCleanupConflict):
        cleanup.erase(plan)
    assert main.exists()


def test_partial_sdk_deletion_retries_and_verifies_ignored_subtree_errors(tmp_path, monkeypatch):
    run, config, directory = native_session(tmp_path)
    write(directory / run.sdk_session_id / "subagents" / "agent.jsonl")
    write(config / "file-history" / run.sdk_session_id / "0123456789abcdef@v1")
    cleanup = ProjectSessionCleanup(tmp_path)
    plan = cleanup.preview(run.project_id, [run], [run])

    def partial(_group):
        (directory / f"{run.sdk_session_id}.jsonl").unlink(missing_ok=True)

    monkeypatch.setattr(cleanup, "_sdk_delete", partial)
    assert cleanup.erase(plan).complete
    assert not directory.exists()
    assert cleanup.erase(plan).complete


def test_cross_project_native_session_and_wrong_cwd_fail_closed(tmp_path):
    run, _, directory = native_session(tmp_path)
    cleanup = ProjectSessionCleanup(tmp_path)
    other = run.model_copy(update={"id": "foreign", "project_id": "project_b"})
    assert cleanup.preview(run.project_id, [run], [run, other]).blockers == [
        "native_session_shared_with_other_project",
    ]
    path = directory / f"{run.sdk_session_id}.jsonl"
    row = json.loads(path.read_text())
    row["cwd"] = "/foreign/workspace"
    path.write_text(json.dumps(row))
    assert cleanup.preview(run.project_id, [run], [run]).blockers == [
        "native_session_ownership_mismatch",
    ]


def test_links_never_follow_outside_reviewed_scope(tmp_path):
    root = tmp_path / "data"
    run, _, directory = native_session(root)
    outsider = tmp_path / "outside.txt"
    outsider.write_text("outside private content")
    link = directory / run.sdk_session_id / "subagents" / "link.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(outsider)
    cleanup = ProjectSessionCleanup(root)
    plan = cleanup.preview(run.project_id, [run], [run])
    assert not plan.blockers and any(row.kind == "symlink" for row in plan.relative_files)
    assert cleanup.erase(plan).complete
    assert outsider.read_text() == "outside private content"
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert ProjectSessionCleanup(alias).preview(run.project_id, [], []).blockers


def test_recorded_main_transcript_leaf_symlink_is_unlinked_without_sdk_following_it(tmp_path):
    root = tmp_path / "data"
    run, _, directory = native_session(root)
    main = directory / f"{run.sdk_session_id}.jsonl"
    outside = tmp_path / "user-owned.jsonl"
    outside.write_text("not an application-owned transcript")
    main.unlink()
    main.symlink_to(outside)
    cleanup = ProjectSessionCleanup(root)
    plan = cleanup.preview(run.project_id, [run], [run])
    assert not plan.blockers
    assert cleanup.erase(plan).complete
    assert outside.read_text() == "not an application-owned transcript"


def test_hardlink_and_unknown_global_copies_block_complete_erasure(tmp_path):
    run, config, directory = native_session(tmp_path)
    write(config / "shell-snapshots" / "unknown.sh")
    cleanup = ProjectSessionCleanup(tmp_path)
    plan = cleanup.preview(run.project_id, [run], [run])
    assert plan.blockers == [
        "unattributed_native_content:runtime-config/environment/shell-snapshots",
    ]
    with pytest.raises(SessionCleanupConflict):
        cleanup.erase(plan)
    os.link(directory / f"{run.sdk_session_id}.jsonl", tmp_path / "hardlink")
    assert "unsafe_native_storage" in cleanup.preview(run.project_id, [run], [run]).blockers


def test_native_checkpoint_hardlinks_require_every_link_in_the_project_plan(tmp_path):
    first, config, _ = native_session(tmp_path)
    second, _, _ = native_session(tmp_path)
    first_backup = config / "file-history" / first.sdk_session_id / "0123456789abcdef@v1"
    second_backup = config / "file-history" / second.sdk_session_id / "0123456789abcdef@v1"
    write(first_backup)
    second_backup.parent.mkdir(parents=True)
    os.link(first_backup, second_backup)
    cleanup = ProjectSessionCleanup(tmp_path)
    runs = [first, second]
    plan = cleanup.preview(first.project_id, runs, runs)
    assert not plan.blockers
    outside_link = tmp_path / "outside-copy"
    os.link(first_backup, outside_link)
    assert cleanup.preview(first.project_id, runs, runs).blockers == [
        "native_checkpoint_has_unreviewed_links",
    ]
    with pytest.raises(SessionCleanupConflict, match="native_checkpoint_has_unreviewed_links"):
        cleanup.erase(plan)
    outside_link.unlink()
    assert cleanup.erase(plan).complete
    assert not first_backup.exists() and not second_backup.exists()


def test_reviewed_read_only_native_directory_can_be_erased(tmp_path):
    run, _, directory = native_session(tmp_path)
    child = directory / run.sdk_session_id / "subagents"
    write(child / "agent-fixture.jsonl")
    child.chmod(0o500)
    directory.chmod(0o500)
    cleanup = ProjectSessionCleanup(tmp_path)
    plan = cleanup.preview(run.project_id, [run], [run])
    assert not plan.blockers
    assert cleanup.erase(plan).complete
    assert not directory.exists()


def test_retry_after_real_sdk_success_before_sidecar_cleanup(tmp_path, monkeypatch):
    run, config, directory = native_session(tmp_path)
    backup = config / "file-history" / run.sdk_session_id / "0123456789abcdef@v1"
    write(backup)
    cleanup = ProjectSessionCleanup(tmp_path)
    plan = cleanup.preview(run.project_id, [run], [run])
    actual = cleanup._sdk_delete

    def interrupted(group):
        actual(group)
        raise RuntimeError("simulated interruption after SDK erased main transcript")

    monkeypatch.setattr(cleanup, "_sdk_delete", interrupted)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        cleanup.erase(plan)
    assert not (directory / f"{run.sdk_session_id}.jsonl").exists()
    assert backup.exists()
    fresh = ProjectSessionCleanup(tmp_path)
    assert fresh.erase(SessionCleanupPlan.model_validate_json(plan.model_dump_json())).complete
    assert not backup.exists() and not directory.exists()


def test_plan_cannot_target_another_data_root_or_config_secret(tmp_path):
    run, config, _ = native_session(tmp_path)
    write(config / ".credentials.json")
    cleanup = ProjectSessionCleanup(tmp_path)
    plan = cleanup.preview(run.project_id, [run], [run])
    other = tmp_path / "other-root"
    other.mkdir()
    with pytest.raises(SessionCleanupConflict):
        ProjectSessionCleanup(other).erase(plan)
    plan.scan_roots = ["runtime-config/environment/.credentials.json"]
    with pytest.raises(SessionCleanupConflict):
        cleanup.erase(plan)
    assert (config / ".credentials.json").exists()


@pytest.mark.parametrize("content", [b'{"cwd":[]}', b'{"sessionId":{}}', b'[]', b'{broken'])
def test_malformed_native_transcript_produces_safe_blocker(tmp_path, content):
    run, _, directory = native_session(tmp_path)
    (directory / f"{run.sdk_session_id}.jsonl").write_bytes(content)
    plan = ProjectSessionCleanup(tmp_path).preview(run.project_id, [run], [run])
    assert plan.blockers
    assert "broken" not in plan.model_dump_json()


def test_unrecognized_native_project_file_and_migration_are_not_silently_deleted(tmp_path):
    run, _, directory = native_session(tmp_path)
    write(directory / "unrecognized-private-file")
    cleanup = ProjectSessionCleanup(tmp_path)
    assert cleanup.preview(run.project_id, [run], [run]).blockers == [
        "unknown_native_project_content",
    ]
    (directory / "unrecognized-private-file").unlink()
    directory.rename(directory.parent / "old-unverifiable-workspace")
    assert cleanup.preview(run.project_id, [run], [run]).blockers == [
        "native_cwd_migration_requires_review",
    ]


def test_native_inventory_budget_fails_closed(tmp_path, monkeypatch):
    run, _, _ = native_session(tmp_path)
    monkeypatch.setattr("ophira_backend.project_session_cleanup._MAX_ENTRIES", 1)
    assert ProjectSessionCleanup(tmp_path).preview(run.project_id, [run], [run]).blockers == [
        "native_entry_limit",
    ]


def test_orphan_native_checkpoint_with_unknown_owner_is_a_blocker(tmp_path):
    run, config, _ = native_session(tmp_path)
    orphan_id = str(uuid4())
    checkpoint = config / "file-history" / orphan_id / "0123456789abcdef@v1"
    write(checkpoint)
    cleanup = ProjectSessionCleanup(tmp_path)
    assert cleanup.preview(run.project_id, [run], [run]).blockers == [
        "unproven_native_sidecar_owner:runtime-config/environment/file-history",
    ]
    other = RunRecord(project_id="project_b", prompt="other project", sdk_session_id=orphan_id)
    plan = cleanup.preview(run.project_id, [run], [run, other])
    assert not plan.blockers
    assert cleanup.erase(plan).complete
    assert checkpoint.exists()


def test_native_cli_dry_run_identifies_checkpoint_without_model_or_credentials(tmp_path):
    import claude_agent_sdk

    run, config, directory = native_session(tmp_path / "data")
    backup = config / "file-history" / run.sdk_session_id / "0123456789abcdef@v1"
    write(backup)
    write(directory / run.sdk_session_id / "subagents" / "agent-fixture.jsonl")
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    cwd = tmp_path / "data" / "projects" / run.project_id / "workspace"
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    result = subprocess.run(
        [str(cli), "project", "purge", str(cwd), "--dry-run"],
        env={"HOME": str(isolated_home), "CLAUDE_CONFIG_DIR": str(config),
             "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
             "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"},
        cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert str(backup.parent) in result.stdout
    assert "file edit history" in result.stdout
    assert backup.exists() and (directory / f"{run.sdk_session_id}.jsonl").exists()
    plan = ProjectSessionCleanup(tmp_path / "data").preview(run.project_id, [run], [run])
    item = next(row for row in plan.relative_files if row.relative_path.endswith(backup.name))
    assert item.sha256 == hashlib.sha256(backup.read_bytes()).hexdigest()


def test_native_cli_rewinds_synthetic_checkpoint_then_cleanup_erases_both_layers(tmp_path):
    """Exercise the bundled CLI's real checkpoint reader without entering its model loop."""
    import claude_agent_sdk

    root = tmp_path / "data"
    run, config, directory = native_session(root)
    cwd = root / "projects" / run.project_id / "workspace"
    target = cwd / "quote.txt"
    target.write_text("modified fixture\n")
    first, second = str(uuid4()), str(uuid4())
    stamp = "2026-09-19T10:00:00.000Z"
    prefix = hashlib.sha256(str(target).encode()).hexdigest()[:16]
    backups = config / "file-history" / run.sdk_session_id
    rows = []
    for version, message_id, content in [(1, first, b"original fixture\n"),
                                         (2, second, b"modified fixture\n")]:
        filename = f"{prefix}@v{version}"
        write(backups / filename, content)
        rows.extend([
            {"parentUuid": None if version == 1 else first, "isSidechain": False,
             "userType": "external", "cwd": str(cwd), "sessionId": run.sdk_session_id,
             "version": "2.1.238", "gitBranch": "", "type": "user",
             "message": {"role": "user", "content": "synthetic checkpoint fixture"},
             "uuid": message_id, "timestamp": stamp},
            {"type": "file-history-snapshot", "messageId": message_id,
             "snapshot": {"messageId": message_id, "trackedFileBackups": {
                 str(target): {"backupFileName": filename, "version": version,
                               "backupTime": stamp, "realParentDir": str(cwd)},
             }, "timestamp": stamp}, "isSnapshotUpdate": False},
        ])
    transcript = directory / f"{run.sdk_session_id}.jsonl"
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
    # The CLI's unchanged-file shortcut checks destination mtime against backup.
    # Match actual checkpoint ordering: capture the backup, then edit the file.
    target.write_text("modified fixture after checkpoint\n")
    home = tmp_path / "isolated-home"
    home.mkdir()
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    # The native CLI treats non-TTY stdin as a prompt, even /dev/null, and
    # rejects that with standalone --rewind-files. A local PTY supplies no input.
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            [str(cli), "--resume", run.sdk_session_id, "--rewind-files", first],
            env={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config),
                 "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                 "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                 "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "true",
                 "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"},
            cwd=cwd, stdin=slave, capture_output=True, text=True, timeout=30,
        )
    finally:
        os.close(slave)
        os.close(master)
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "original fixture\n"
    assert transcript.exists() and backups.exists()  # rewind is not session erasure
    cleanup = ProjectSessionCleanup(root)
    plan = cleanup.preview(run.project_id, [run], [run])
    assert not plan.blockers
    assert cleanup.erase(plan).complete
    assert not transcript.exists() and not backups.exists()
    assert target.read_text() == "original fixture\n"  # project service owns workspace erasure
