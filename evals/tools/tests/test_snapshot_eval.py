import copy
import json
import re
import sys
from pathlib import Path

import pytest

from run_snapshot_evals import main as cli_main
from snapshot_eval import (
    DEFAULT_SUITE_PATH,
    apply_patch,
    canonical_hash,
    evaluate_case,
    evaluate_minimum_pass,
    evaluate_snapshot,
    evaluate_suite,
    load_suite,
    materialize_case,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TASK_PROFILE_PATTERN = re.compile(
    r"minimum_pass:\s*\n\s+evaluator:\s+snapshot-v1\s*\n\s+profile:\s+([^\s#]+)"
)


def suite() -> dict:
    return load_suite(DEFAULT_SUITE_PATH)


def test_release_suite_has_at_least_thirty_positive_and_negative_cases() -> None:
    cases = suite()["cases"]

    assert len(cases) >= 30
    assert {case["expected"] for case in cases} == {"accept", "reject"}

    names = " ".join(case["id"] for case in cases)
    for required in (
        "provenance",
        "stale-apply",
        "fake-grounded-metric",
        "empty",
        "timeout",
        "malformed-partial",
        "missing-final-presentation",
        "concurrent",
        "cross-capability",
    ):
        assert required in names


def test_every_static_case_matches_expected_outcome_and_finding() -> None:
    results = evaluate_suite(suite())

    failures = [result.as_dict() for result in results if not result.passed]
    assert failures == []


def test_rejection_cases_are_real_mutations_with_named_findings() -> None:
    active = suite()
    rejection_cases = [case for case in active["cases"] if case["expected"] == "reject"]

    assert all(case.get("expected_findings") for case in rejection_cases)
    for case in rejection_cases:
        result = evaluate_case(active, case)
        assert not result.accepted
        assert set(result.expected_findings) <= {finding.code for finding in result.findings}


def test_valid_partial_frame_must_reconcile_in_its_server_owned_slot() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["trusted-commerce"])
    final = snapshot["output"]["presentations"][0]
    partial = {
        "id": "partial-1",
        "protocol_version": "1",
        "slot_id": final["slot_id"],
        "schema_version": "1",
        "frame_type": "partial",
        "validation_status": "valid",
        "discarded": False,
        "selected": False,
        "project_revision": final["project_revision"],
    }
    snapshot["output"]["presentations"] = [partial, final]

    accepted = evaluate_snapshot(snapshot, "trusted-commerce-v1", suite=active)
    assert accepted.accepted, accepted.as_dict()

    snapshot["output"]["presentations"] = [partial]
    rejected = evaluate_snapshot(snapshot, "trusted-commerce-v1", suite=active)
    assert "presentation.dangling_partial" in {
        finding.code for finding in rejected.findings
    }


def test_partial_cannot_reconcile_against_a_different_slot() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["trusted-commerce"])
    final = snapshot["output"]["presentations"][0]
    partial = {
        "id": "partial-1",
        "protocol_version": "1",
        "slot_id": "slot_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "schema_version": "1",
        "frame_type": "partial",
        "validation_status": "valid",
        "discarded": False,
        "selected": False,
        "project_revision": final["project_revision"],
    }
    snapshot["output"]["presentations"] = [partial, final]

    rejected = evaluate_snapshot(snapshot, "trusted-commerce-v1", suite=active)
    assert "presentation.dangling_partial" in {
        finding.code for finding in rejected.findings
    }


def test_unknown_profile_and_unknown_grader_fail_closed() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["failure"])

    unknown_profile = evaluate_snapshot(snapshot, "does-not-exist", suite=active)
    assert not unknown_profile.accepted
    assert [item.code for item in unknown_profile.findings] == ["profile.unknown"]

    active["profiles"]["bad-profile"] = ["not-a-grader"]
    unknown_grader = evaluate_snapshot(snapshot, "bad-profile", suite=active)
    assert not unknown_grader.accepted
    assert [item.code for item in unknown_grader.findings] == ["profile.unknown_grader"]

    malformed_profile = evaluate_snapshot(snapshot, [], suite=active)  # type: ignore[arg-type]
    assert not malformed_profile.accepted
    assert [item.code for item in malformed_profile.findings] == ["profile.unknown"]


def test_suite_loader_rejects_unknown_grader_even_when_profile_is_unused(tmp_path: Path) -> None:
    active = suite()
    active["profiles"]["unused-bad-profile"] = ["not-a-grader"]
    path = tmp_path / "bad-suite.json"
    path.write_text(json.dumps(active), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown graders"):
        load_suite(path)


@pytest.mark.parametrize(
    "malformed",
    [None, [], "snapshot", 7],
)
def test_malformed_snapshot_root_fails_closed(malformed: object) -> None:
    result = evaluate_snapshot(malformed, "safe-failure-v1", suite=suite())

    assert not result.accepted
    assert result.findings[0].code == "snapshot.not_object"


def test_missing_field_and_bad_type_are_rejected_without_grader_crash() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["failure"])
    del snapshot["output"]["receipts"]
    snapshot["output"]["run"] = "not-an-object"

    result = evaluate_snapshot(snapshot, "safe-failure-v1", suite=active)

    assert not result.accepted
    assert "snapshot.missing_or_malformed" in {item.code for item in result.findings}
    assert "snapshot.run_malformed" in {item.code for item in result.findings}


def test_snapshot_identity_binding_distinguishes_local_from_authenticated() -> None:
    active = suite()
    local = copy.deepcopy(active["bases"]["failure"])
    local["output"]["run"].update(
        {
            "identity_binding": "local_only",
            "tenant_id": None,
            "principal_id": None,
            "session_id": None,
        }
    )
    local["input"]["project_ledger"]["tenant_id"] = None
    local["output"]["project_ledger"]["tenant_id"] = None

    assert evaluate_snapshot(local, "safe-failure-v1", suite=active).accepted

    forged = copy.deepcopy(local)
    forged["output"]["run"]["tenant_id"] = "claimed-tenant"
    result = evaluate_snapshot(forged, "safe-failure-v1", suite=active)
    assert not result.accepted
    assert "snapshot.run_identity_malformed" in {item.code for item in result.findings}


def test_authenticated_snapshot_requires_complete_principal_scope() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["failure"])
    snapshot["output"]["run"]["principal_id"] = None

    result = evaluate_snapshot(snapshot, "safe-failure-v1", suite=active)

    assert not result.accepted
    assert "snapshot.run_identity_malformed" in {item.code for item in result.findings}


@pytest.mark.parametrize("surface", ["metrics", "receipts", "backend_state"])
def test_empty_runtime_sections_cannot_masquerade_as_complete_capture(
    surface: str,
) -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["failure"])
    snapshot["output"]["capture_status"][surface] = False

    result = evaluate_snapshot(snapshot, "safe-failure-v1", suite=active)

    assert not result.accepted
    assert f"capture.{surface}_incomplete" in {item.code for item in result.findings}


def test_missing_capture_status_key_fails_closed_in_completeness_grader() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["failure"])
    del snapshot["output"]["capture_status"]["receipts"]

    result = evaluate_snapshot(snapshot, "safe-failure-v1", suite=active)

    codes = {item.code for item in result.findings}
    assert "snapshot.capture_status_malformed" in codes
    assert "capture.receipts_incomplete" in codes


def test_completed_calculation_without_serialized_metrics_fails_closed() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["task"])
    snapshot["output"]["tool_calls"] = [
        {
            "tool_name": "mcp__ophira_commerce_math__calculate_unit_economics",
            "capability": "calculate",
            "status": "completed",
            "input": {},
        }
    ]

    result = evaluate_snapshot(snapshot, "task-product-validation-v1", suite=active)

    assert "capture.metrics_unaccounted" in {item.code for item in result.findings}


def test_completed_external_read_without_host_receipt_fails_closed() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["failure"])
    snapshot["output"]["tool_calls"] = [
        {
            "tool_name": "WebSearch",
            "capability": "web",
            "status": "completed",
            "input": {"query": "market"},
        }
    ]

    result = evaluate_snapshot(snapshot, "safe-failure-v1", suite=active)

    assert "capture.receipts_unaccounted" in {item.code for item in result.findings}


def test_connector_effect_without_backend_call_capture_fails_closed() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["trusted-commerce"])
    snapshot["input"]["backend_state"] = {
        "kind": "none",
        "before": {},
        "after": {},
        "calls": [],
    }

    result = evaluate_snapshot(snapshot, "trusted-commerce-v1", suite=active)

    assert "capture.backend_state_unaccounted" in {
        item.code for item in result.findings
    }


def test_trusted_commerce_requires_a_selected_final_presentation() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["trusted-commerce"])
    snapshot["output"]["presentations"] = []

    result = evaluate_snapshot(snapshot, "trusted-commerce-v1", suite=active)

    assert "presentation.final_required" in {item.code for item in result.findings}


def test_incomplete_message_history_fails_closed() -> None:
    active = suite()
    snapshot = copy.deepcopy(active["bases"]["failure"])
    snapshot["input"]["system"]["history_complete"] = False

    result = evaluate_snapshot(snapshot, "safe-failure-v1", suite=active)

    assert not result.accepted
    assert "capture.message_history_incomplete" in {item.code for item in result.findings}


def test_patch_materialization_is_deterministic_and_does_not_mutate_base() -> None:
    active = suite()
    case = next(item for item in active["cases"] if item.get("patch"))
    base_before = json.dumps(active["bases"][case["base"]], sort_keys=True)

    first = materialize_case(active, case)
    second = materialize_case(active, case)

    assert canonical_hash(first) == canonical_hash(second)
    assert json.dumps(active["bases"][case["base"]], sort_keys=True) == base_before


def test_invalid_case_patch_cannot_masquerade_as_expected_rejection() -> None:
    active = suite()
    case = {
        "id": "broken-fixture",
        "profile": "safe-failure-v1",
        "base": "failure",
        "expected": "reject",
        "expected_findings": ["snapshot.missing_or_malformed"],
        "patch": [{"op": "delete", "path": "does.not.exist"}],
    }

    result = evaluate_case(active, case)

    assert not result.passed
    assert result.findings[0].code == "case.materialization_failed"


def test_apply_patch_rejects_unknown_operations_and_out_of_range_paths() -> None:
    with pytest.raises(ValueError, match="unsupported patch operation"):
        apply_patch({"a": {}}, [{"op": "move", "path": "a.b"}])
    with pytest.raises(ValueError, match="out of range"):
        apply_patch({"a": [1]}, [{"op": "set", "path": "a.2", "value": 3}])


def test_all_task_minimum_pass_entries_reference_executable_profiles() -> None:
    active = suite()
    expected = {
        "a1-demand-entry.yaml": "task-demand-entry-v1",
        "a2-supply-entry.yaml": "task-supply-entry-v1",
        "b-product-validation.yaml": "task-product-validation-v1",
        "c-supplier-inquiry.yaml": "task-supplier-inquiry-v1",
    }

    for filename, profile in expected.items():
        text = (REPO_ROOT / "evals" / "tasks" / filename).read_text(encoding="utf-8")
        match = TASK_PROFILE_PATTERN.search(text)
        assert match is not None, filename
        assert match.group(1) == profile
        assert profile in active["profiles"]
        matching_cases = [case for case in active["cases"] if case["profile"] == profile]
        assert any(case["expected"] == "accept" for case in matching_cases)
        accept_case = next(case for case in matching_cases if case["expected"] == "accept")
        snapshot = materialize_case(active, accept_case)
        evaluation = evaluate_minimum_pass(
            snapshot,
            {"evaluator": "snapshot-v1", "profile": match.group(1)},
            suite=active,
        )
        assert evaluation.accepted


@pytest.mark.parametrize(
    "minimum_pass",
    [None, [], {}, {"evaluator": "snapshot-v0", "profile": "safe-failure-v1"}],
)
def test_minimum_pass_reference_fails_closed_when_malformed(minimum_pass: object) -> None:
    result = evaluate_minimum_pass(suite()["bases"]["failure"], minimum_pass, suite=suite())

    assert not result.accepted
    assert result.findings[0].code == "minimum_pass.malformed"


def test_cli_command_runs_the_offline_release_gate(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["run_snapshot_evals.py"])

    assert cli_main() == 0
    captured = capsys.readouterr()
    assert "offline contract/mutation suite; no live E2E claim" in captured.out


def test_single_snapshot_cli_requires_profile(monkeypatch, capsys, tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(suite()["bases"]["failure"]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["run_snapshot_evals.py", "--snapshot", str(snapshot)])

    assert cli_main() == 2
    assert "--profile is required" in capsys.readouterr().err
