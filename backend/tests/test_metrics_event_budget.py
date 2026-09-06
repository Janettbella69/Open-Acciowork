"""Bounded event replay charges actual work, including unsuccessful replay attempts."""

from pathlib import Path

import pytest

from ophira_backend.constants import EventType
from ophira_backend.models import EventActor, EventEnvelope, ProjectCreate, RunRecord
from ophira_backend.storage import EventReplayBudget, EventReplayLimitError, FileLedger


def make_log(tmp_path: Path, sequences=(1, 2), *, text="synthetic"):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(
        name="Event replay budget", objective="Bound synthetic ledger replay work",
    ))
    run = ledger.create_run(project.id, "Measure local synthetic events")
    rows = [make_event(run, sequence, text=text) for sequence in sequences]
    payload = b"".join(encode_event(row) for row in rows)
    path = ledger.runs_dir / run.id / "events.jsonl"
    path.write_bytes(payload)
    return ledger, run, path, rows, payload


def make_event(run: RunRecord, sequence: int, *, text="synthetic") -> EventEnvelope:
    return EventEnvelope(
        id=f"event_{sequence}",
        sequence=sequence,
        type=EventType.RUN_STARTED.value,
        run_id=run.id,
        project_id=run.project_id,
        trace_id=run.trace_id,
        actor=EventActor(type="agent", name="ophira-main"),
        data={"text": text},
    )


def encode_event(event: EventEnvelope) -> bytes:
    return (event.model_dump_json() + "\n").encode("utf-8")


def forbid_event_file_open(monkeypatch, path: Path):
    original = Path.open

    def guarded_open(self, *args, **kwargs):
        if self == path:
            pytest.fail("An exhausted request budget must not reopen the event log")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)


def track_parsing(monkeypatch):
    attempts = []
    original = EventEnvelope.model_validate_json

    def parse(raw, *args, **kwargs):
        attempts.append(raw)
        return original(raw, *args, **kwargs)

    monkeypatch.setattr(EventEnvelope, "model_validate_json", parse)
    return attempts


@pytest.mark.parametrize("field", ["max_bytes", "max_events", "bytes_read", "events_read"])
@pytest.mark.parametrize("invalid", [-1, True, False, 1.5, "1", None])
def test_shared_budget_rejects_non_integer_or_negative_fields(field, invalid):
    arguments = {"max_bytes": 10, "max_events": 10, field: invalid}
    with pytest.raises((TypeError, ValueError)):
        EventReplayBudget(**arguments)


def test_shared_budget_accepts_zero_and_nonnegative_integer_counters():
    empty = EventReplayBudget(max_bytes=0, max_events=0)
    assert empty.bytes_read == empty.events_read == 0
    used = EventReplayBudget(max_bytes=10, max_events=4, bytes_read=11, events_read=4)
    assert used.bytes_read == 11  # The one-byte overflow probe remains charged.
    assert used.events_read == 4


@pytest.mark.parametrize("field", ["max_bytes", "max_events"])
@pytest.mark.parametrize("invalid", [-1, True, False, 1.5, "1"])
def test_per_run_limits_reject_non_integer_or_negative_values(tmp_path, field, invalid):
    ledger, run, *_ = make_log(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        ledger.list_events(run.id, **{field: invalid})


def test_default_replay_preserves_blank_lines_file_order_and_after_sequence(tmp_path):
    ledger, run, path, rows, _ = make_log(tmp_path, (3, 1, 2))
    payload = b" \t\r\n\n" + b"\n \t\n".join(encode_event(row) for row in rows) + b"\n"
    path.write_bytes(payload)
    assert ledger.list_events(run.id) == rows
    assert ledger.list_events(run.id, after_sequence=1) == [rows[0], rows[2]]
    budget = EventReplayBudget(max_bytes=len(payload), max_events=3)
    assert ledger.list_events(run.id, after_sequence=1, budget=budget) == [rows[0], rows[2]]
    assert budget.bytes_read == len(payload)
    assert budget.events_read == 3


def test_utf8_exact_byte_limit_is_inclusive_and_charges_encoded_bytes(tmp_path):
    ledger, run, _, rows, payload = make_log(tmp_path, (1,), text="现金流：€ 200 💶")
    assert len(payload) > len(payload.decode("utf-8"))
    budget = EventReplayBudget(max_bytes=len(payload), max_events=1)
    assert ledger.list_events(
        run.id, max_bytes=len(payload), max_events=1, budget=budget,
    ) == rows
    assert budget.bytes_read == len(payload)
    assert budget.events_read == 1


@pytest.mark.parametrize("newline", [b"\r", b"\r\n"])
def test_bounded_replay_preserves_universal_newlines_without_discounting_bytes(tmp_path, newline):
    ledger, run, path, rows, payload = make_log(tmp_path)
    payload = payload.replace(b"\n", newline)
    path.write_bytes(payload)
    budget = EventReplayBudget(max_bytes=len(payload), max_events=2)
    assert ledger.list_events(run.id) == rows
    assert ledger.list_events(run.id, budget=budget) == rows
    assert budget.bytes_read == len(payload)
    assert budget.events_read == 2


def test_character_count_cannot_bypass_utf8_byte_limit(tmp_path):
    ledger, run, _, _, payload = make_log(tmp_path, (1,), text="现金流" * 30)
    limit = len(payload.decode("utf-8"))
    budget = EventReplayBudget(max_bytes=len(payload) * 2, max_events=10)
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(run.id, max_bytes=limit, budget=budget)
    assert error.value.reason == "run_byte_limit"
    assert budget.bytes_read == limit + 1


@pytest.mark.parametrize("limit", [0, 1, 17])
def test_per_run_byte_limit_charges_only_the_one_byte_overflow_probe(tmp_path, limit):
    ledger, run, _, _, payload = make_log(tmp_path)
    budget = EventReplayBudget(max_bytes=len(payload) * 2, max_events=10)
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(run.id, max_bytes=limit, budget=budget)
    assert error.value.reason == "run_byte_limit"
    assert budget.bytes_read == limit + 1


@pytest.mark.parametrize("limiting_scope", ["run", "request"])
def test_byte_counters_match_actual_io_and_never_read_the_oversized_file(
    tmp_path, monkeypatch, limiting_scope,
):
    ledger, run, path, _, payload = make_log(tmp_path)
    original = Path.open
    bytes_read = []

    class ObservedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, *args):
            chunk = self.stream.read(*args)
            assert isinstance(chunk, bytes)
            bytes_read.append(len(chunk))
            return chunk

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def observed_open(self, *args, **kwargs):
        stream = original(self, *args, **kwargs)
        return ObservedReader(stream) if self == path else stream

    monkeypatch.setattr(Path, "open", observed_open)
    limit = 11
    budget = EventReplayBudget(
        max_bytes=limit if limiting_scope == "request" else len(payload), max_events=10,
    )
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(
            run.id, max_bytes=limit if limiting_scope == "run" else len(payload), budget=budget,
        )
    assert error.value.reason == f"{limiting_scope}_byte_limit"
    assert sum(bytes_read) == budget.bytes_read == limit + 1 < len(payload)


def test_shared_byte_limit_applies_across_runs_and_charges_overflow_probe(tmp_path, monkeypatch):
    ledger, first, _, rows, payload = make_log(tmp_path, (1,))
    second = ledger.create_run(first.project_id, "Another synthetic log")
    second_path = ledger.runs_dir / second.id / "events.jsonl"
    second_path.write_bytes(encode_event(make_event(second, 1)))
    budget = EventReplayBudget(max_bytes=len(payload) + 7, max_events=10)
    assert ledger.list_events(first.id, budget=budget) == rows
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(second.id, budget=budget)
    assert error.value.reason == "request_byte_limit"
    assert budget.bytes_read == budget.max_bytes + 1
    charged_events = budget.events_read
    forbid_event_file_open(monkeypatch, second_path)
    with pytest.raises(EventReplayLimitError) as retry_error:
        ledger.list_events(second.id, budget=budget)
    assert retry_error.value.reason == "request_byte_limit"
    assert budget.bytes_read == budget.max_bytes + 1
    assert budget.events_read == charged_events


@pytest.mark.parametrize("counter", ["bytes_read", "events_read"])
def test_exactly_exhausted_shared_budget_does_not_open_another_log(tmp_path, monkeypatch, counter):
    ledger, run, path, _, _ = make_log(tmp_path)
    budget = EventReplayBudget(max_bytes=1000, max_events=10)
    if counter == "bytes_read":
        budget.bytes_read = budget.max_bytes
        reason = "request_byte_limit"
    else:
        budget.events_read = budget.max_events
        reason = "request_event_limit"
    before = (budget.bytes_read, budget.events_read)
    forbid_event_file_open(monkeypatch, path)
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(run.id, budget=budget)
    assert error.value.reason == reason
    assert (budget.bytes_read, budget.events_read) == before


def test_per_run_event_limit_stops_before_parsing_the_next_line(tmp_path, monkeypatch):
    ledger, run, path, rows, _ = make_log(tmp_path, (1,))
    path.write_bytes(encode_event(rows[0]) + b"not-json\n")
    attempts = track_parsing(monkeypatch)
    budget = EventReplayBudget(max_bytes=10000, max_events=10)
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(run.id, max_events=1, budget=budget)
    assert error.value.reason == "run_event_limit"
    assert len(attempts) == budget.events_read == 1


def test_zero_per_run_event_limit_does_not_parse_any_event(tmp_path, monkeypatch):
    ledger, run, *_ = make_log(tmp_path)
    attempts = track_parsing(monkeypatch)
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(run.id, max_events=0)
    assert error.value.reason == "run_event_limit"
    assert attempts == []


def test_shared_event_limit_stops_before_parsing_a_corrupt_next_line(tmp_path, monkeypatch):
    ledger, run, path, rows, _ = make_log(tmp_path, (1,))
    path.write_bytes(encode_event(rows[0]) + b"not-json\n")
    attempts = track_parsing(monkeypatch)
    budget = EventReplayBudget(max_bytes=10000, max_events=1)
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(run.id, max_events=5, budget=budget)
    assert error.value.reason == "request_event_limit"
    assert len(attempts) == budget.events_read == 1


def test_shared_event_limit_across_runs_and_filtered_events_are_still_charged(tmp_path):
    ledger, first, _, _, payload = make_log(tmp_path)
    second = ledger.create_run(first.project_id, "Second log")
    (ledger.runs_dir / second.id / "events.jsonl").write_bytes(b"".join(
        encode_event(make_event(second, sequence)) for sequence in (1, 2)
    ))
    budget = EventReplayBudget(max_bytes=len(payload) * 4, max_events=3)
    assert ledger.list_events(first.id, after_sequence=999, budget=budget) == []
    assert budget.events_read == 2
    with pytest.raises(EventReplayLimitError) as error:
        ledger.list_events(second.id, after_sequence=999, budget=budget)
    assert error.value.reason == "request_event_limit"
    assert budget.events_read == 3


def test_json_parse_failure_charges_both_valid_and_failed_lines(tmp_path, monkeypatch):
    ledger, run, path, rows, _ = make_log(tmp_path, (1,))
    payload = b"\n" + encode_event(rows[0]) + b" \t\n{broken-json}\n"
    path.write_bytes(payload)
    attempts = track_parsing(monkeypatch)
    budget = EventReplayBudget(max_bytes=10000, max_events=5)
    with pytest.raises(ValueError) as error:
        ledger.list_events(run.id, budget=budget)
    assert not isinstance(error.value, EventReplayLimitError)
    assert budget.bytes_read == len(payload)
    assert len(attempts) == budget.events_read == 2
    path.write_bytes(encode_event(rows[0]))
    assert ledger.list_events(run.id, budget=budget) == rows
    assert budget.events_read == 3
    assert budget.bytes_read == len(payload) + len(encode_event(rows[0]))


@pytest.mark.parametrize("suffix", [b"", b" "])
def test_incomplete_tail_is_rejected_and_does_not_refund_bytes(tmp_path, suffix):
    ledger, run, path, _, payload = make_log(tmp_path, (1,))
    incomplete = payload.rstrip(b"\n") + suffix
    path.write_bytes(incomplete)
    budget = EventReplayBudget(max_bytes=10000, max_events=10)
    with pytest.raises(ValueError, match="incomplete tail"):
        ledger.list_events(run.id, budget=budget)
    assert budget.bytes_read == len(incomplete)
    with pytest.raises(ValueError, match="incomplete tail"):
        ledger.list_events(run.id)


def test_invalid_utf8_is_rejected_and_does_not_refund_bytes(tmp_path):
    ledger, run, path, _, _ = make_log(tmp_path, ())
    payload = b"\xff\xfe\n"
    path.write_bytes(payload)
    budget = EventReplayBudget(max_bytes=10000, max_events=10)
    with pytest.raises(ValueError) as error:
        ledger.list_events(run.id, budget=budget)
    assert not isinstance(error.value, EventReplayLimitError)
    assert budget.bytes_read == len(payload)


@pytest.mark.parametrize("payload", [b"", b"\n \t\n\r\n"])
def test_empty_and_whitespace_only_logs_charge_no_events(tmp_path, payload):
    ledger, run, path, _, _ = make_log(tmp_path, ())
    path.write_bytes(payload)
    budget = EventReplayBudget(max_bytes=10000, max_events=10)
    assert ledger.list_events(run.id, budget=budget) == []
    assert ledger.list_events(run.id) == []
    assert budget.events_read == 0
    assert budget.bytes_read == len(payload)


def test_missing_event_log_still_replays_as_empty(tmp_path):
    ledger, run, path, _, _ = make_log(tmp_path, ())
    path.unlink()
    budget = EventReplayBudget(max_bytes=10000, max_events=10)
    assert ledger.list_events(run.id) == []
    assert ledger.list_events(run.id, budget=budget) == []
    assert budget.bytes_read == budget.events_read == 0
