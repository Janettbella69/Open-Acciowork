"""Offline checks of durable SSE replay and the separate completion watermark."""

import json

import httpx
import pytest

from ophira_backend.constants import STREAM_END_EVENT
from ophira_backend.greenpath import sse_replay_sequences
from ophira_backend.models import EventActor, EventEnvelope

RUN_ID = "run_replay"
PROJECT_ID = "project_replay"


def _event(index, **changes):
    return EventEnvelope(
        id=f"{RUN_ID}:{index}", sequence=index, type="run.completed",
        run_id=RUN_ID, project_id=PROJECT_ID, trace_id="trace_replay",
        actor=EventActor(type="system", name="fixture"), data={},
    ).model_dump(mode="json") | changes


def _frame(payload, *, event_type=None, event_id=None, multiline=False):
    event_type = event_type or payload.get("type", STREAM_END_EVENT)
    parts = [f"event: {event_type}"]
    if event_id is not None:
        parts.append(f"id: {event_id}")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2 if multiline else None)
    parts.extend(f"data: {line}" for line in serialized.splitlines())
    return "\n".join(parts) + "\n\n"


def _durable(index, **changes):
    payload = _event(index, **changes)
    return _frame(payload, event_id=payload["id"])


def _end(final_sequence, **changes):
    return _frame({
        "run_id": RUN_ID, "project_id": PROJECT_ID,
        "last_sequence": final_sequence, **changes,
    })


async def _replay(body, checkpoint=0, *, status_code=200):
    def respond(request):
        assert request.method == "GET"
        assert request.url.path == f"/api/runs/{RUN_ID}/events"
        assert request.headers["Last-Event-ID"] == f"{RUN_ID}:{checkpoint}"
        return httpx.Response(
            status_code, content=body, headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://fixture",
    ) as client:
        return await sse_replay_sequences(client, RUN_ID, checkpoint, project_id=PROJECT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [0, 1, 2, 3])
async def test_valid_replay_from_zero_midpoint_or_final_cursor(checkpoint):
    body = ": heartbeat\n\n" + "".join(
        _durable(sequence, type="future.additive.event")
        for sequence in range(checkpoint + 1, 4)
    ) + _end(3)
    assert await _replay(body, checkpoint) == list(range(checkpoint + 1, 4))


@pytest.mark.asyncio
async def test_empty_ledger_can_complete_at_zero_without_fabricating_an_event():
    assert await _replay(_end(0)) == []


@pytest.mark.asyncio
async def test_terminal_event_does_not_hide_recovered_project_tail():
    body = _durable(1) + _durable(2, type="evidence.added") + _end(2)
    assert await _replay(body) == [1, 2]


@pytest.mark.asyncio
async def test_comments_crlf_and_multiline_data_follow_sse_framing():
    payload = _event(1)
    body = ": keepalive\n\n" + _frame(payload, event_id=payload["id"], multiline=True)
    body += _end(1) + ": closing comment\n\n"
    assert await _replay(body.replace("\n", "\r\n")) == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", ": heartbeat\n\n", _durable(1)])
async def test_eof_without_completion_is_not_a_success(body):
    with pytest.raises(ValueError, match="missing its completion"):
        await _replay(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"run_id": "run_other"}, {"project_id": "project_other"},
    {"last_sequence": 0}, {"last_sequence": 2}, {"last_sequence": -1},
    {"last_sequence": "1"}, {"last_sequence": True}, {"last_sequence": None},
])
async def test_completion_rejects_wrong_identity_and_watermark(changes):
    with pytest.raises(ValueError, match="identity or watermark mismatch"):
        await _replay(_durable(1) + _end(1, **changes))


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", ["", f"{RUN_ID}:2"])
async def test_completion_must_not_carry_even_an_empty_id_field(event_id):
    control = _frame(
        {"run_id": RUN_ID, "project_id": PROJECT_ID, "last_sequence": 1},
        event_id=event_id,
    )
    with pytest.raises(ValueError, match="must not have an event ID"):
        await _replay(_durable(1) + control)


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [_end(1), _durable(2, type="evidence.added")])
async def test_duplicate_completion_or_durable_event_after_completion_fails(tail):
    with pytest.raises(ValueError, match="after completion"):
        await _replay(_durable(1) + _end(1) + tail)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"run_id": "run_other"}, {"project_id": "project_other"},
    {"id": "run_other:1"}, {"sequence": 2}, {"sequence": True},
])
async def test_durable_replay_rejects_wrong_identity_or_noncontiguous_sequence(changes):
    with pytest.raises(ValueError):
        await _replay(_durable(1, **changes) + _end(1))


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    _frame(_event(1)),
    _frame(_event(1), event_id=f"{RUN_ID}:7"),
    _frame(_event(1), event_id=f"{RUN_ID}:1", event_type="evidence.added"),
    "event: run.completed\nid: run_replay:1\ndata: malformed\n\n",
    "event: run.completed\nid: run_replay:1\ndata: []\n\n",
    "event: run.completed\nid: run_replay:1\n\n",
    "id: run_replay:1\ndata: {}\n\n",
])
async def test_invalid_durable_frame_does_not_become_a_sequence(frame):
    with pytest.raises(ValueError):
        await _replay(frame + _end(1))


@pytest.mark.asyncio
async def test_incomplete_frame_and_repeated_identity_fields_are_rejected():
    with pytest.raises(ValueError, match="incomplete frame"):
        await _replay(_durable(1) + _end(1).rstrip("\n"))
    with pytest.raises(ValueError, match="repeats an identity field"):
        await _replay("event: run.completed\n" + _durable(1) + _end(1))


@pytest.mark.asyncio
async def test_http_failure_never_becomes_empty_successful_replay():
    with pytest.raises(httpx.HTTPStatusError):
        await _replay("unavailable", status_code=503)


@pytest.mark.asyncio
async def test_invalid_envelope_does_not_copy_response_prose_into_report_error():
    payload = _event(1, actor={"name": "api_key=private-malformed-payload"})
    with pytest.raises(ValueError, match="invalid envelope") as failure:
        await _replay(_frame(payload, event_id=payload["id"]) + _end(1))
    assert "private-malformed-payload" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [-1, True, "1"])
async def test_invalid_checkpoint_is_rejected_before_http_request(checkpoint):
    with pytest.raises(ValueError, match="nonnegative integer"):
        await _replay(_end(1), checkpoint)
