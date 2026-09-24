"""Project durable user messages from a Run's ordered event log."""

from __future__ import annotations

import hashlib

from .constants import EventType, RunInterjectionMode
from .models import EventEnvelope, RunInterjectionReceipt


class RunInterjectionConflict(RuntimeError):
    """A message is stale or its idempotency key names different content."""


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def interjection_receipts(
    run_id: str, events: list[EventEnvelope],
) -> list[RunInterjectionReceipt]:
    receipts: dict[str, RunInterjectionReceipt] = {}
    for event in events:
        if not event.type.startswith("run.interjection."):
            continue
        request_id = event.data.get("resource_id")
        if not isinstance(request_id, str):
            continue
        if event.type == EventType.RUN_INTERJECTION_REQUESTED:
            prompt = event.data.get("prompt")
            mode = event.data.get("mode")
            if isinstance(prompt, str) and mode in RunInterjectionMode._value2member_map_:
                receipts[request_id] = RunInterjectionReceipt(
                    request_id=request_id, run_id=run_id, prompt=prompt,
                    mode=RunInterjectionMode(mode), status="queued",
                )
            continue
        previous = receipts.get(request_id)
        if previous is None:
            continue
        if event.type == EventType.RUN_INTERJECTION_DISPATCHING:
            receipts[request_id] = previous.model_copy(update={"status": "dispatching"})
        elif event.type == EventType.RUN_INTERJECTION_INJECTED:
            receipts[request_id] = previous.model_copy(update={"status": "injected"})
        elif event.type == EventType.RUN_INTERJECTION_UNCERTAIN:
            receipts[request_id] = previous.model_copy(update={"status": "uncertain"})
        elif event.type == EventType.RUN_INTERJECTION_BLOCKED:
            receipts[request_id] = previous.model_copy(update={"status": "blocked"})
        elif event.type == EventType.RUN_INTERJECTION_CARRIED:
            destination = event.data.get("next_run_id")
            if isinstance(destination, str):
                receipts[request_id] = previous.model_copy(
                    update={"status": "carried", "next_run_id": destination},
                )
    return list(receipts.values())


def queued_for_next_turn(
    run_id: str, events: list[EventEnvelope],
) -> list[RunInterjectionReceipt]:
    # A dispatch attempt can have crossed the process boundary. Never replay it
    # blindly after a crash, since a repeated instruction could repeat effects.
    return [item for item in interjection_receipts(run_id, events)
            if item.status == "queued"]


def carry_prompt(
    prompt: str, queued: list[RunInterjectionReceipt],
) -> str:
    if not queued:
        return prompt
    lines = ["User messages queued during the previous run, in arrival order:"]
    for item in queued:
        intent = (
            "Replace the prior direction"
            if item.mode == RunInterjectionMode.INTERRUPT else "Continue with"
        )
        lines.append(f"- {intent} this user message: {item.prompt}")
    lines.extend(["", "Current user message:", prompt])
    return "\n".join(lines)
