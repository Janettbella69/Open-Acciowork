import pytest

from ophira_backend.conversations import conversation_runs
from ophira_backend.models import RunRecord
from ophira_backend.project_deletion_capture import ProjectDeletionConflict
from ophira_backend.storage import LedgerNotFoundError


def run(identifier, **values):
    return RunRecord(id=identifier, project_id="project_a", prompt="fixture", **values)


def test_chain_includes_transitive_resumes_and_preinitialization_failures():
    first = run("run_first", sdk_session_id="session_a")
    second = run(
        "run_second",
        sdk_session_id="session_b",
        resume_session_id="session_a",
        resume_source_run_id=first.id,
    )
    failed = run("run_failed", resume_session_id="session_b", resume_source_run_id=second.id)
    fresh = run("run_fresh")
    provider = run("run_provider", sdk_session_id="session_a", provider_profile_id="other")
    records = [failed, fresh, provider, second, first]
    for anchor in (first, second, failed):
        assert {item.id for item in conversation_runs("project_a", anchor.id, records)} == {
            first.id,
            second.id,
            failed.id,
        }
    assert conversation_runs("project_a", fresh.id, records) == [fresh]
    assert conversation_runs("project_a", provider.id, records) == [provider]


@pytest.mark.parametrize("kind", ["session", "source", "missing", "duplicate"])
def test_unverifiable_or_foreign_lineage_is_rejected(kind):
    first = run("run_first", sdk_session_id="session_a")
    other = run("run_other")
    if kind == "session":
        other = other.model_copy(update={"project_id": "project_b", "sdk_session_id": "session_a"})
    elif kind == "source":
        other = other.model_copy(
            update={"project_id": "project_b", "resume_source_run_id": first.id}
        )
    elif kind == "missing":
        first.resume_source_run_id = "run_missing"
    else:
        other = first
    with pytest.raises(ProjectDeletionConflict):
        conversation_runs("project_a", first.id, [first, other])
    with pytest.raises(LedgerNotFoundError):
        conversation_runs("project_b", first.id, [first])
