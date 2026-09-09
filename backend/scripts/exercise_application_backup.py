"""Synthetic, network-blocked cold backup acceptance in fresh Python processes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))

from exercise_skill_backup import install_offline_guard  # noqa: E402

from ophira_backend import application_backup as backup  # noqa: E402
from ophira_backend.backup_files import (  # noqa: E402
    private_json,
    remove_staging,
    tree_inventory,
)
from ophira_backend.constants import RunStatus  # noqa: E402
from ophira_backend.models import ProjectCreate, utc_now  # noqa: E402
from ophira_backend.skill_proposal_models import SkillProposalReviewRequest  # noqa: E402
from ophira_backend.skill_proposals import SkillProposalService  # noqa: E402
from ophira_backend.skills import SkillRegistry  # noqa: E402
from ophira_backend.storage import FileLedger  # noqa: E402


class InterruptedFixture(BaseException):
    pass


def prepare(root: Path) -> tuple[Path, Path, dict]:
    data, plugin = root / "data", root / "plugin"
    shutil.copytree(ROOT / "backend/ophira-capabilities", plugin)
    ledger, registry = FileLedger(data), SkillRegistry(plugin, data / "skills")
    service = SkillProposalService(ledger, registry)
    project = ledger.create_project(
        ProjectCreate(
            name="Synthetic application recovery",
            objective="Verify offline cold backup",
        )
    )
    run = ledger.create_run(
        project.id, "Synthetic source; no SDK execution or human approval claimed"
    )
    ledger.update_run(run.id, started_at=utc_now(), sdk_session_id="synthetic-offline-fixture")
    workspace = ledger.workspace_for_project(project.id)
    rows = {}
    for kind in ("pending", "rejected", "published", "intent_only", "committed_intent"):
        name = f"recovery-{kind}"
        base = workspace / "skill-proposals" / name
        package = base / "package"
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Synthetic recovery fixture\nversion: 1.0.0\n---\n"
            "Read references/evidence.md and copy templates/report.md.\n",
        )
        (package / "references").mkdir()
        (package / "references/evidence.md").write_text(
            "Synthetic evidence; not a business claim.\n"
        )
        (package / "templates").mkdir()
        (package / "templates/report.md").write_text("# Synthetic report\n")
        private_json(
            base / "manifest.json",
            {
                "schema_version": 1,
                "kind": "ophira.skill-proposal",
                "name": name,
                "version": "1.0.0",
                "package_dir": "package",
                "reason": "Offline acceptance fixture",
            },
        )
        artifact = ledger.register_artifact(
            project.id, f"skill-proposals/{name}/manifest.json", run_id=run.id
        )
        item = service.submit_artifact(run.id, artifact.id)
        request = SkillProposalReviewRequest(
            decision="reject" if kind == "rejected" else "approve",
            revision=item.revision,
            content_hash=item.content_hash,
            expected_target=registry.selection_token(item.skill_id),
            note="Synthetic review binding, not a real user decision",
        )
        if kind in {"intent_only", "committed_intent"}:
            owner, method = (
                (registry, "publish_reviewed_candidate")
                if kind == "intent_only"
                else (service, "_finish")
            )
            with patch.object(owner, method, side_effect=InterruptedFixture):
                try:
                    service.review(item.id, request, None)
                except InterruptedFixture:
                    pass
                else:
                    raise AssertionError("fixture must stop in the intended crash window")
        elif kind != "pending":
            service.review(item.id, request, None)
            if kind == "published":
                registry.set_enabled(item.skill_id, True)
        rows[kind] = item.id
    ledger.update_run(
        run.id,
        status=RunStatus.COMPLETED,
        finished_at=utc_now(),
        skill_snapshot=registry.snapshot(),
    )
    (data / "secrets").mkdir()
    (data / "secrets/synthetic.json").write_text('{"fixture":"synthetic-private-canary"}')
    return data, plugin, {"proposal_ids": rows, "run_id": run.id, "project_id": project.id}


def main() -> int:
    install_offline_guard()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    report = ROOT / "evals/reports" / f"application-backup-{stamp}"
    report.mkdir(mode=0o700)
    summary = {
        "passed": False,
        "scope": "synthetic_local_cold_backup",
        "cases": {},
        "network_blocked": True,
        "provider_invoked": False,
        "live_data_read": False,
        "implementation": backup.implementation(),
        "exercise_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="ophira-application-backup-") as temporary:
            work = Path(temporary).resolve()
            source = work / "source"
            source.mkdir()
            data, plugin, fixture = prepare(source)
            archive = report / "backup"
            summary["created"] = backup.create_backup(data, plugin, archive)
            before = tree_inventory(archive, backup.DEFAULT_LIMITS)
            remove_staging(source)
            summary["source_removed"] = not source.exists()
            expected = {
                "success": None,
                "missing_proposal": "publication_proposal_missing",
                "missing_receipt": "proposal_publication_receipt_missing",
                "bad_source": "proposal_run_mismatch",
                "bytes_changed": "file_inventory_mismatch",
            }
            for name, reason in expected.items():
                case_archive = archive
                if name != "success":
                    case_archive = work / f"backup-{name}"
                    shutil.copytree(archive, case_archive)
                    payload = case_archive / "payload/data"
                    if name == "missing_proposal":
                        remove_staging(
                            payload / "skill-proposals" / fixture["proposal_ids"]["published"]
                        )
                    elif name == "missing_receipt":
                        path = payload / "skills/registry-state.json"
                        value = json.loads(path.read_text())
                        del value["publication_receipts"][fixture["proposal_ids"]["published"]]
                        path.write_text(json.dumps(value))
                    elif name == "bad_source":
                        path = payload / "runs" / fixture["run_id"] / "run.json"
                        value = json.loads(path.read_text())
                        value["sdk_session_id"] = "incorrect-session"
                        path.write_text(json.dumps(value))
                    else:
                        (payload / "secrets/synthetic.json").write_text("modified")
                    if name != "bytes_changed":
                        path = case_archive / "manifest.json"
                        value = json.loads(path.read_text())
                        value["tree"] = tree_inventory(
                            case_archive / "payload", backup.DEFAULT_LIMITS
                        )
                        path.write_text(json.dumps(value))
                target = work / f"restored-{name}"
                command = [
                    sys.executable,
                    "-c",
                    (
                        "from exercise_skill_backup import install_offline_guard; "
                        "install_offline_guard(); "
                        "from ophira_backend.application_backup import main; "
                        "import os,json; status=main(); "
                        "print(json.dumps({'worker_pid':os.getpid()})); raise SystemExit(status)"
                    ),
                    "restore",
                    "--backup",
                    str(case_archive),
                    "--output",
                    str(target),
                ]
                completed = subprocess.run(
                    command,
                    cwd=work,
                    timeout=30,
                    check=False,
                    capture_output=True,
                    text=True,
                    env={
                        "PATH": os.defpath,
                        "PYTHONNOUSERSITE": "1",
                        "PYTHONPATH": os.pathsep.join(
                            [
                                str(ROOT / "backend/src"),
                                str(ROOT / "backend/scripts"),
                            ]
                        ),
                    },
                )
                lines = completed.stdout.splitlines()
                result = json.loads(lines[0])
                result.update(json.loads(lines[1]))
                assert result["worker_pid"] != os.getpid()
                assert "synthetic-private-canary" not in completed.stdout + completed.stderr
                if name == "success":
                    assert completed.returncode == 0 and result["passed"]
                    assert result["proposal_states"] == {
                        "pending": 2,
                        "published": 2,
                        "rejected": 1,
                    }
                    assert result["proposal_recovery"]["published"] == 1
                    assert result["proposal_recovery"]["reset_pending"] == 1
                    assert result["canonical_preserved"]
                    saved = json.loads((archive / "manifest.json").read_text())
                    actual = tree_inventory(target, backup.DEFAULT_LIMITS)
                    backup._check_recovery_changes(saved["tree"], actual, saved["inventory"])
                    result["retained_files_verified"] = len(actual["files"])
                else:
                    assert completed.returncode == 1 and not result["passed"]
                    assert result["reason"] == reason and not target.exists()
                assert not list(work.glob(".application-restore-*"))
                private_json(report / f"{name}.json", {**result, "exit_code": completed.returncode})
                summary["cases"][name] = {"expected_result": True, "report": f"{name}.json"}
                if target.exists():
                    remove_staging(target)
            assert tree_inventory(archive, backup.DEFAULT_LIMITS) == before
            assert backup.implementation() == summary["implementation"]
            summary["backup_unchanged"] = True
            summary["passed"] = True
    except BaseException as error:
        summary["error_type"] = type(error).__name__
    finally:
        private_json(report / "summary.json", summary)
    print(json.dumps({"passed": summary["passed"], "report": str(report / "summary.json")}))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
