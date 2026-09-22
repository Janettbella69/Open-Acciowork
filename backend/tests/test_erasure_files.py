"""Real filesystem/process failure tests for the privacy transaction boundary."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ophira_backend import erasure_files as ef


def fixture(root):
    root.mkdir(exist_ok=True)
    (root / "projects/selected/nested").mkdir(parents=True)
    files = {
        "projects/selected/run.json": b"private-conversation-canary",
        "projects/selected/nested/output.txt": b"private-output-canary",
        "shared.json": b'{"selected":"private-canary","other":"keep-me"}',
    }
    retained = b'{"other":"keep-me"}'
    for path, data in files.items():
        (root / path).write_bytes(data)
    (root / "other-project.txt").write_bytes(b"unrelated-private-canary")
    replacements = {"shared.json": retained}
    plan = ef.ErasurePlan(
        scope={"kind": "project", "project_id": "selected", "actor": "fixture-owner"},
        files=[
            ef.ErasureFile(
                path=path,
                before_size=len(data),
                before_sha256=ef.digest(data),
                after_size=len(retained) if path == "shared.json" else None,
                after_sha256=ef.digest(retained) if path == "shared.json" else None,
            )
            for path, data in files.items()
        ],
        directories=["projects/selected", "projects/selected/nested"],
    )
    return ef.FileErasure(root), plan, replacements


def assert_erased(root):
    assert not (root / "projects/selected").exists()
    assert (root / "shared.json").read_bytes() == b'{"other":"keep-me"}'
    assert (root / "other-project.txt").read_bytes() == b"unrelated-private-canary"
    assert not (root / ef.JOURNAL / "pending").exists()
    for path in (root / ef.JOURNAL).rglob("*"):
        if path.is_file():
            assert b"private-conversation-canary" not in path.read_bytes()
            assert b"private-output-canary" not in path.read_bytes()
            assert b"private-canary" not in path.read_bytes()


def test_exact_targets_are_erased_retained_data_survives_and_receipt_contains_no_content(tmp_path):
    service, plan, replacements = fixture(tmp_path)
    calls = []
    receipt = service.commit(plan, replacements, authorize=lambda: calls.append(True))
    assert len(calls) == 2 and receipt["state"] == "completed"
    assert receipt["fingerprint"] == plan.fingerprint()
    assert receipt["scope"] == plan.scope and receipt["files"] == 3
    assert "plan" not in receipt
    assert_erased(tmp_path)
    assert service.recover() is None
    stored = tmp_path / ef.JOURNAL / f"{receipt['id']}.json"
    assert json.loads(stored.read_bytes()) == receipt
    assert stored.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["source", "extra_child", "replacement", "authority"])
def test_no_source_mutation_before_authorized_verified_commit(tmp_path, change):
    service, plan, replacements = fixture(tmp_path)
    calls = 0

    def authorize():
        nonlocal calls
        calls += 1
        if change == "authority" and calls == 2:
            raise PermissionError("revoked")

    if change == "source":
        (tmp_path / "projects/selected/run.json").write_bytes(b"new content")
    elif change == "extra_child":
        (tmp_path / "projects/selected/new.txt").write_bytes(b"new data")
    elif change == "replacement":
        replacements["shared.json"] = b"wrong retained value"
    with pytest.raises((ef.ErasureError, PermissionError)):
        service.commit(plan, replacements, authorize=authorize)
    assert (
        tmp_path / "projects/selected/nested/output.txt"
    ).read_bytes() == b"private-output-canary"
    assert b"private-canary" in (tmp_path / "shared.json").read_bytes()
    assert not (tmp_path / ef.JOURNAL / "pending").exists()
    service.recover()  # Clean pre-commit staging; this must never commit it.
    assert (tmp_path / "projects/selected/run.json").exists()
    assert not list((tmp_path / ef.JOURNAL).glob("staging-*"))


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "parent_link", "fifo"])
def test_unsafe_targets_fail_without_touching_outside_files(tmp_path, attack):
    service, plan, replacements = fixture(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"private-conversation-canary")
    target = tmp_path / "projects/selected/run.json"
    if attack == "parent_link":
        (tmp_path / "projects/selected").rename(tmp_path / "moved")
        (tmp_path / "projects/selected").symlink_to(tmp_path / "moved", target_is_directory=True)
    else:
        target.unlink()
        if attack == "symlink":
            target.symlink_to(outside)
        elif attack == "hardlink":
            os.link(outside, target)
        else:
            os.mkfifo(target)
    with pytest.raises((ValueError, OSError)):
        service.commit(plan, replacements, authorize=lambda: None)
    assert outside.read_bytes() == b"private-conversation-canary"
    assert not (tmp_path / ef.JOURNAL / "pending").exists()


def child(code, root):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-c", code, str(root)],
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("point", ["published", "unlinked", "replaced", "receipt", "renamed"])
def test_process_exit_at_each_commit_boundary_recovers_without_resurrection(tmp_path, point):
    service, plan, replacements = fixture(tmp_path)
    (tmp_path / "fixture-plan.json").write_bytes(ef.encode(plan.model_dump(mode="json")))
    result = child(
        """
import json, os, sys
from pathlib import Path
from ophira_backend import erasure_files as ef
root = Path(sys.argv[1])
point = POINT
service = ef.FileErasure(root)
plan = ef.ErasurePlan.model_validate_json((root / "fixture-plan.json").read_bytes())
if point == "published":
    service._recover_locked = lambda: os._exit(71)
elif point in {"unlinked", "replaced"}:
    original = service._apply_file
    def apply(*args):
        original(*args)
        if (point == "unlinked" and args[-1].after_sha256 is None) or (
                point == "replaced" and args[-1].after_sha256 is not None):
            os._exit(71)
    service._apply_file = apply
elif point == "receipt":
    original = ef._write_at
    def write(parent, name, raw, **kwargs):
        original(parent, name, raw, **kwargs)
        if name.startswith("erase_"):
            os._exit(71)
    ef._write_at = write
else:
    original = os.rename
    def rename(src, dst, **kwargs):
        original(src, dst, **kwargs)
        if src == "pending":
            os._exit(71)
    os.rename = rename
service.commit(plan, {"shared.json": b'{"other":"keep-me"}'}, authorize=lambda: None)
""".replace("POINT", repr(point)),
        tmp_path,
    )
    assert result.returncode == 71, result.stderr.decode()
    if point != "renamed":
        with pytest.raises(ef.DataOperationBusy, match="committed deletion"):
            with ef.operation_lease(tmp_path):
                pytest.fail("pending erasure allowed data access")
    assert service.recover() is not None if point != "renamed" else service.recover() is None
    assert_erased(tmp_path)
    assert not list((tmp_path / ef.JOURNAL).glob("finished-*"))
    with ef.operation_lease(tmp_path):
        pass


def test_replacement_partial_write_is_recovered_from_the_committed_retained_bytes(tmp_path):
    service, plan, replacements = fixture(tmp_path)
    original = service._recover_locked
    service._recover_locked = lambda: None
    service.commit(plan, replacements, authorize=lambda: None)
    record = json.loads((tmp_path / ef.JOURNAL / "pending/record.json").read_bytes())
    temporary = tmp_path / f".erasure-{record['id']}-2.tmp"
    temporary.write_bytes(replacements["shared.json"][:5])
    service._recover_locked = original
    service.recover()
    assert_erased(tmp_path)
    assert not temporary.exists()


@pytest.mark.parametrize("tamper", ["record", "replacement", "source", "journal_link"])
def test_corrupt_pending_transaction_stays_closed_and_preserves_other_data(tmp_path, tamper):
    service, plan, replacements = fixture(tmp_path)
    original = service._recover_locked
    service._recover_locked = lambda: None
    service.commit(plan, replacements, authorize=lambda: None)
    service._recover_locked = original
    pending = tmp_path / ef.JOURNAL / "pending"
    if tamper == "record":
        record = json.loads((pending / "record.json").read_bytes())
        record["plan"]["scope"]["project_id"] = "another-project"
        (pending / "record.json").write_bytes(ef.encode(record))
    elif tamper == "replacement":
        (pending / "2.bin").write_bytes(b"corrupt")
    elif tamper == "source":
        (tmp_path / "projects/selected/run.json").write_bytes(b"changed after commit")
    else:
        pending.rename(tmp_path / "moved-journal")
        pending.symlink_to(tmp_path / "moved-journal", target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        service.recover()
    with pytest.raises((ValueError, OSError)):
        with ef.operation_lease(tmp_path):
            pytest.fail("corrupt pending transaction was ignored")
    assert (tmp_path / "other-project.txt").read_bytes() == b"unrelated-private-canary"


def test_real_process_readers_exclude_erasure_and_exclusive_owner_excludes_readers(tmp_path):
    fixture(tmp_path)
    code = """
import sys
from pathlib import Path
from ophira_backend.erasure_files import operation_lease, DataOperationBusy
try:
    with operation_lease(Path(sys.argv[1]), exclusive=EXCLUSIVE):
        sys.exit(2)
except DataOperationBusy:
    sys.exit(0)
"""
    with ef.operation_lease(tmp_path):
        assert child(code.replace("EXCLUSIVE", "True"), tmp_path).returncode == 0
        with ef.operation_lease(tmp_path):
            pass
    with ef.operation_lease(tmp_path, exclusive=True):
        assert child(code.replace("EXCLUSIVE", "False"), tmp_path).returncode == 0


@pytest.mark.parametrize(
    "paths",
    [
        ["../outside"],
        ["/absolute"],
        [".data-erasure/pending/record.json"],
        [".data-operation.lock"],
        [".DATA-ERASURE/pending/record.json"],
        ["same", "same"],
        ["Report/a", "report/b"],
        ["file", "file/child"],
        ["caf\u00e9", "cafe\u0301"],
    ],
)
def test_invalid_or_ambiguous_target_plans_are_rejected(paths):
    with pytest.raises((ValueError, ValidationError)):
        ef.ErasurePlan(
            scope={},
            files=[
                ef.ErasureFile(
                    path=path,
                    before_size=0,
                    before_sha256=ef.digest(b""),
                )
                for path in paths
            ],
            directories=[],
        )


def test_empty_pending_directory_is_not_treated_as_success(tmp_path):
    (tmp_path / ef.JOURNAL / "pending").mkdir(parents=True)
    with pytest.raises(ef.ErasureError, match="incomplete"):
        ef.FileErasure(tmp_path).recover()


def test_cold_backup_lease_excludes_commit_and_recovery(tmp_path):
    from ophira_backend.maintenance import DataDirectoryBusyError, data_directory_lease

    service, plan, replacements = fixture(tmp_path)
    with data_directory_lease(tmp_path, exclusive=True):
        with pytest.raises(DataDirectoryBusyError):
            service.commit(plan, replacements, authorize=lambda: None)
        with pytest.raises(DataDirectoryBusyError):
            service.recover()
    assert (tmp_path / "projects/selected/run.json").exists()


def test_journal_envelope_limit_is_checked_before_commit(tmp_path, monkeypatch):
    service, plan, replacements = fixture(tmp_path)
    monkeypatch.setattr(ef, "MAX_JOURNAL_BYTES", len(ef.encode(plan.model_dump(mode="json"))) + 10)
    with pytest.raises(ef.ErasureError, match="journal exceeds"):
        service.commit(plan, replacements, authorize=lambda: None)
    assert (tmp_path / "projects/selected/run.json").exists()
    assert not (tmp_path / ef.JOURNAL / "pending").exists()


@pytest.mark.parametrize("kind", ["sources", "replacements"])
def test_aggregate_byte_limits_reject_oversized_plans_before_source_reads(kind):
    with pytest.raises(ValueError, match="byte limit"):
        ef.ErasurePlan(
            scope={},
            directories=[],
            files=[
                ef.ErasureFile(
                    path=f"files/{index}",
                    before_size=ef.MAX_SOURCE_BYTES,
                    before_sha256=ef.digest(b""),
                    after_size=ef.MAX_REPLACEMENT_BYTES if kind == "replacements" else None,
                    after_sha256=ef.digest(b"") if kind == "replacements" else None,
                )
                for index in range(9 if kind == "sources" else 5)
            ],
        )


def test_retained_files_cannot_be_planned_inside_a_removed_directory():
    with pytest.raises(ValueError, match="retained files"):
        ef.ErasurePlan(
            scope={},
            files=[
                ef.ErasureFile(
                    path="project/retained.json",
                    before_size=0,
                    before_sha256=ef.digest(b""),
                    after_size=0,
                    after_sha256=ef.digest(b""),
                )
            ],
            directories=["project"],
        )
