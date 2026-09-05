from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sample_feedback import build_feedback_samples, write_new_batch


def message_feedback(**overrides) -> dict:
    row = {
        "id": "feedback_1",
        "project_id": "project_1",
        "run_id": "run_1",
        "message_id": "run_1:3",
        "evidence_id": None,
        "kind": "conclusion_wrong",
        "user_note": "The fixture says 20, not 25.",
        "status": "open",
        "source": "user",
        "context": {
            "trace_id": "trace_1",
            "run_status": "completed",
            "event_id": "run_1:3",
            "event_type": "assistant.message.completed",
            "message_text": "Synthetic quote is 25 USD.",
            "evidence": None,
        },
    }
    row.update(overrides)
    return row


def test_feedback_reuses_gold_sampler_but_never_creates_labels_or_copies_identity():
    row = message_feedback(
        label=False, principal_id="private@example.test", principal_session_id="private-session"
    )
    samples = build_feedback_samples([row])
    assert samples["labels_produced"] is False
    assert samples["feedback_candidates"][0]["candidate_state"] == "unlabeled"
    assert samples["feedback_candidates"][0]["event_id"] == "run_1:3"
    assert samples["items"]
    assert all("label" not in item for item in samples["items"])
    assert {item["split"] for item in samples["items"]} <= {"train", "dev", "test"}
    dumped = json.dumps(samples)
    assert "private@example.test" not in dumped and "private-session" not in dumped
    assert samples == build_feedback_samples([row])


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failures_without_claims_remain_candidates_not_negative_gold(status):
    row = message_feedback(kind="run_failure", message_id=None, source="system")
    row["context"] = {
        "trace_id": "trace_1",
        "run_status": status,
        "event_id": "run_1:9",
        "event_type": f"run.{status}",
        "message_text": "Transient failure code 503",
    }
    samples = build_feedback_samples([row])
    assert len(samples["feedback_candidates"]) == 1
    assert samples["feedback_candidates"][0]["review_status"] == "open"
    assert samples["items"] == []
    assert samples["runs"]["run_1"]["final_text"] == ""


def test_evidence_feedback_preserves_source_and_exact_ledger_anchor():
    row = message_feedback(kind="evidence_issue", message_id=None, evidence_id="ev_1")
    row["context"]["evidence"] = {
        "id": "ev_1",
        "kind": "user_statement",
        "claim": "Synthetic quote is 25 USD.",
        "status": "active",
        "source_title": "Fixture",
        "source_path": "uploads/fixture.csv",
    }
    samples = build_feedback_samples([row])
    assert samples["feedback_candidates"][0]["evidence_id"] == "ev_1"
    assert samples["runs"]["run_1"]["evidence"][0]["source"] == "path"
    assert samples["items"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"status": "rejected"}, "human_rejected"),
        ({"context": None}, "missing_trace_anchor"),
        ({"message_id": "different"}, "missing_message_anchor"),
        ({"kind": "evidence_issue", "evidence_id": "ev_missing"}, "missing_evidence_anchor"),
    ],
)
def test_rejected_and_unanchored_reports_are_explicitly_skipped(changes, reason):
    result = build_feedback_samples([message_feedback(**changes)])
    assert not result["feedback_candidates"] and not result["items"]
    assert result["skipped_feedback"] == [{"feedback_id": "feedback_1", "reason": reason}]


def test_conflicting_identity_and_duplicate_ids_fail_instead_of_mixing_runs():
    first = message_feedback()
    second = copy.deepcopy(first)
    second["id"] = "feedback_2"
    second["project_id"] = "other_project"
    with pytest.raises(ValueError, match="conflicting project/trace"):
        build_feedback_samples([first, second])
    with pytest.raises(ValueError, match="duplicate"):
        build_feedback_samples([first, first])


def test_candidate_writer_is_atomic_and_never_overwrites_human_data(tmp_path: Path):
    path = tmp_path / "batch.json"
    samples = build_feedback_samples([message_feedback()])
    write_new_batch(path, samples)
    with pytest.raises(FileExistsError):
        write_new_batch(path, {"unwanted": True})
    assert json.loads(path.read_text()) == samples
    with pytest.raises(ValueError, match="never a labels.jsonl"):
        write_new_batch(tmp_path / "labels.jsonl", samples)
    assert not list(tmp_path.glob(".feedback-batch-*"))


def test_real_cli_produces_an_unlabeled_batch_and_no_label_files(tmp_path: Path):
    source = tmp_path / "export.json"
    source.write_text(json.dumps([message_feedback()]))
    output = tmp_path / "gold" / "samples" / "fixture.json"
    script = Path(__file__).resolve().parents[1] / "sample_feedback.py"
    result = subprocess.run(
        [sys.executable, str(script), "--input", str(source), "--out", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "No labels or release verdicts were produced" in result.stdout
    assert json.loads(output.read_text())["feedback_candidates"]
    assert not list(tmp_path.rglob("labels.jsonl"))
