#!/usr/bin/env python3
"""L2 behavior-eval driver (PRD §3.11): replay the benchmark task set on a real provider.

Reads evals/tasks/*.yaml, runs each through the same in-process app + Green Path
harness, checks per-task machine assertions (tool coverage subset, ledger writes,
artifact landing), and archives redacted bundles for error analysis.

Usage:
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py --dry-run
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py \
      --task b-product-validation --provider deepseek
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from math import floor, isfinite
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))
sys.path.insert(0, str(REPO_ROOT / "evals" / "tools"))

from snapshot_eval import (  # noqa: E402
    canonical_hash,
    evaluate_minimum_pass,
    load_suite,
)

from ophira_backend.agent_core import SECURITY_PREAMBLE  # noqa: E402
from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import (  # noqa: E402
    READ_ONLY_BUILTIN_TOOLS,
    READ_ONLY_MCP_TOOL_NAMES,
    USER_SCOPE_ID,
    EventType,
    ExternalExecutionStatus,
    MemoryScope,
    MemoryStatus,
)
from ophira_backend.context_protocol import build_system_prompt_append  # noqa: E402
from ophira_backend.greenpath import (  # noqa: E402
    PermissionPolicy,
    drive_run,
    verify_events,
    write_bundle,
)
from ophira_backend.main import create_app  # noqa: E402
from ophira_backend.presentation_tools import presentation_event_response  # noqa: E402
from ophira_backend.telemetry import build_run_telemetry  # noqa: E402

TASKS_DIR = REPO_ROOT / "evals" / "tasks"

MIME_DEFAULTS = {"csv": "text/csv", "md": "text/markdown"}


def load_tasks(task_filter: str | None) -> list[dict]:
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        spec["_path"] = path
        if task_filter and spec.get("id") != task_filter:
            continue
        tasks.append(spec)
    if not tasks:
        raise SystemExit(f"No tasks matched (filter={task_filter!r}) in {TASKS_DIR}")
    return tasks


def validate_task(spec: dict, suite: dict[str, Any] | None = None) -> list[str]:
    problems = []
    for key in ("id", "scenario", "prompt", "fixtures", "expect", "minimum_pass"):
        if key not in spec:
            problems.append(f"missing key: {key}")
    for fixture in spec.get("fixtures", []):
        fixture_path = TASKS_DIR / fixture["path"]
        if not fixture_path.exists():
            problems.append(f"fixture not found: {fixture_path}")
    expect = spec.get("expect", {})
    unknown = set(expect) - {
        "deny_marker",
        "required_coverage",
        "evidence_min",
        "decision_required",
        "artifact_suffix",
    }
    if unknown:
        problems.append(f"unknown expect keys: {sorted(unknown)}")
    minimum_pass = spec.get("minimum_pass")
    if not isinstance(minimum_pass, dict):
        problems.append("minimum_pass must be an executable object")
    else:
        if minimum_pass.get("evaluator") != "snapshot-v1":
            problems.append("minimum_pass.evaluator must be snapshot-v1")
        profile = minimum_pass.get("profile")
        if not isinstance(profile, str) or not profile:
            problems.append("minimum_pass.profile must be a non-empty string")
        elif suite is not None and profile not in suite.get("profiles", {}):
            problems.append(f"minimum_pass.profile is unknown: {profile}")
    return problems


def _model_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _metric_value(metric: Any) -> int | float | bool | str | None:
    return metric.value if getattr(metric, "availability", None) == "observed" else None


def _percentile(values: list[float], quantile: float) -> float | None:
    """Linear percentile over observed values; missing samples remain missing."""

    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build release metrics without converting unavailable observations to zero."""

    passed = sum(item.get("passed") is True for item in results)
    total = len(results)

    def observed(field: str) -> list[float]:
        values = [item.get(field) for item in results]
        return [
            float(value)
            for value in values
            if isinstance(value, int | float)
            and not isinstance(value, bool)
            and isfinite(float(value))
        ]

    def distribution(field: str) -> dict[str, int | float | None]:
        values = observed(field)
        return {
            "observed_runs": len(values),
            "p50": _percentile(values, 0.50),
            "p99": _percentile(values, 0.99),
        }

    costs = observed("observed_cost_usd")
    input_tokens = observed("input_tokens")
    output_tokens = observed("output_tokens")
    cache_read_tokens = observed("cache_read_input_tokens")
    cache_write_tokens = observed("cache_creation_input_tokens")
    cache_hits = [
        item["cache_hit"]
        for item in results
        if isinstance(item.get("cache_hit"), bool)
    ]
    cost_capture_complete = total > 0 and len(costs) == total
    observed_cost_total = sum(costs) if costs else None
    total_cost = observed_cost_total if cost_capture_complete else None
    return {
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": total - passed,
        "pass_rate": (passed / total) if total else None,
        "latency_ms": {
            "total_task": distribution("total_task_duration_ms"),
            "ttft": distribution("ttft_ms"),
            "first_useful_card": distribution("first_useful_typed_card_ms"),
        },
        "tokens": {
            "observed_runs": sum(
                (
                    isinstance(item.get("input_tokens"), int | float)
                    and not isinstance(item.get("input_tokens"), bool)
                )
                or (
                    isinstance(item.get("output_tokens"), int | float)
                    and not isinstance(item.get("output_tokens"), bool)
                )
                for item in results
            ),
            "input_total": sum(input_tokens) if input_tokens else None,
            "output_total": sum(output_tokens) if output_tokens else None,
            "cache_read_total": sum(cache_read_tokens) if cache_read_tokens else None,
            "cache_creation_total": (
                sum(cache_write_tokens) if cache_write_tokens else None
            ),
        },
        "cache_hit": {
            "observed_runs": len(cache_hits),
            "hit_runs": sum(cache_hits),
            "hit_rate": (sum(cache_hits) / len(cache_hits)) if cache_hits else None,
        },
        "cost": {
            "observed_runs": len(costs),
            "capture_complete": cost_capture_complete,
            "observed_total_usd": observed_cost_total,
            "total_usd": total_cost,
            "cost_per_success_usd": (
                total_cost / passed
                if total_cost is not None and passed
                else None
            ),
        },
    }


def _project_snapshot(project) -> dict[str, Any]:
    raw = project.model_dump(mode="json")
    return {
        "id": raw["id"],
        "tenant_id": raw.get("tenant_id"),
        "identity_binding": raw.get("identity_binding", "local_only"),
        "revision": raw["revision"],
        "hypotheses": raw.get("hypotheses", []),
        "evidence": raw.get("evidence", []),
        "decisions": raw.get("decisions", []),
        "artifacts": raw.get("artifacts", []),
    }


def _tool_capability(tool_name: str) -> str:
    leaf = tool_name.rsplit("__", 1)[-1]
    if tool_name == "Read":
        return "read"
    if tool_name in {"Write", "Edit"}:
        return "write"
    if tool_name == "Bash":
        return "shell"
    if tool_name == "Skill":
        return "skill"
    if tool_name in {"WebSearch", "WebFetch"} or "ophira_web" in tool_name:
        return "web"
    if "presentation" in tool_name or leaf.startswith("present_"):
        return "presentation"
    if "ledger" in tool_name or leaf.startswith(("record_", "read_project")):
        return "ledger"
    if any(word in leaf for word in ("calculate", "compare", "rank", "score")):
        return "calculate"
    return "mcp" if tool_name.startswith("mcp__") else "read"


def _tool_calls(events) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    statuses = {
        "tool.started": "started",
        "tool.completed": "completed",
        "tool.failed": "failed",
    }
    for event in events:
        status = statuses.get(event.type)
        tool_name = event.data.get("tool_name")
        if status is None or not isinstance(tool_name, str) or not tool_name:
            continue
        tool_input = event.data.get("tool_input")
        rows.append(
            {
                "tool_name": tool_name,
                "capability": _tool_capability(tool_name),
                "status": status,
                "input": tool_input if isinstance(tool_input, dict) else {},
                "tool_use_id": event.tool_use_id,
            }
        )
    return rows


def _permission_snapshot(events, grants: list[Any], run) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    for event in events:
        if event.type == "permission.requested":
            request_id = event.data.get("request_id")
            if isinstance(request_id, str):
                requests.append(
                    {
                        "id": request_id,
                        "tool_name": event.data.get("tool_name"),
                        "scope": event.data.get("scope", ""),
                    }
                )
        elif event.type in {"permission.resolved", "permission.timed_out"}:
            request_id = event.data.get("request_id")
            if isinstance(request_id, str):
                resolutions.append(
                    {
                        "request_id": request_id,
                        "decision": event.data.get("decision", "timed_out"),
                    }
                )
    normalized_grants = []
    for grant in grants:
        raw = _model_json(grant)
        normalized_grants.append(
            {
                **raw,
                "source": "user_control_plane",
                "tenant_id": run.tenant_id,
                "principal_id": run.principal_id,
            }
        )
    return {
        "requests": requests,
        "resolutions": resolutions,
        "grants": normalized_grants,
    }


def _record_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key == "id"
                and isinstance(child, str)
                and child.startswith(("ev_", "decision_", "artifact_", "hyp_", "action_"))
            ):
                found.add(child)
            else:
                found.update(_record_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_record_ids(child))
    return found


def _presentations(events) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for event in events:
        if event.type != "ui.presentation":
            continue
        raw_frame = event.data.get("frame") if isinstance(event.data, dict) else None
        frame_type = raw_frame.get("frame_type") if isinstance(raw_frame, dict) else None
        slot_id = raw_frame.get("slot_id") if isinstance(raw_frame, dict) else None
        framed = raw_frame is not None
        frame_valid = (
            isinstance(raw_frame, dict)
            and set(raw_frame)
            == {
                "protocol_version",
                "slot_id",
                "frame_type",
                "schema_version",
                "component",
                "project_id",
                "project_revision",
                "title",
                "emitted_at",
            }
            and raw_frame.get("protocol_version") == "1"
            and isinstance(slot_id, str)
            and re.fullmatch(r"slot_[0-9a-f]{32}", slot_id) is not None
            and frame_type in {"partial", "final", "discarded"}
            and raw_frame.get("schema_version") == "1"
            and isinstance(raw_frame.get("component"), str)
            and raw_frame.get("project_id") == event.project_id
            and isinstance(raw_frame.get("project_revision"), int)
            and not isinstance(raw_frame.get("project_revision"), bool)
            and raw_frame.get("project_revision") >= 1
            and isinstance(raw_frame.get("title"), str)
            and 0 < len(raw_frame.get("title")) <= 160
            and isinstance(raw_frame.get("emitted_at"), str)
        )
        if framed and not frame_valid:
            frames.append(
                {
                    "id": f"malformed-{event.sequence}",
                    "frame_type": frame_type or "partial",
                    "validation_status": "invalid",
                    "discarded": True,
                    "selected": False,
                }
            )
            continue
        if frame_valid and frame_type in {"partial", "discarded"}:
            frames.append(
                {
                    "id": f"{slot_id}-{event.sequence}",
                    "protocol_version": "1",
                    "slot_id": slot_id,
                    "schema_version": raw_frame.get("schema_version"),
                    "frame_type": frame_type,
                    "validation_status": "valid",
                    "discarded": frame_type == "discarded",
                    "selected": False,
                    "project_revision": raw_frame.get("project_revision"),
                }
            )
            continue
        validated = presentation_event_response(event.data, event.project_id)
        presentation = validated.get("presentation") if validated is not None else None
        if (
            not isinstance(presentation, dict)
            or (
                frame_valid
                and (
                    frame_type != "final"
                    or presentation.get("schema_version") != raw_frame.get("schema_version")
                    or presentation.get("component") != raw_frame.get("component")
                    or presentation.get("project_revision")
                    != raw_frame.get("project_revision")
                )
            )
        ):
            frames.append(
                {
                    "id": f"malformed-{event.sequence}",
                    "frame_type": frame_type or "partial",
                    "validation_status": "invalid",
                    "discarded": True,
                    "selected": False,
                }
            )
            continue
        record_ids = sorted(_record_ids(presentation.get("payload")))
        revision = presentation.get("project_revision")
        frame = {
            "id": presentation.get("presentation_id", f"view-{event.sequence}"),
            "schema_version": presentation.get("schema_version"),
            "frame_type": "final",
            "validation_status": "valid",
            "discarded": False,
            "selected": False,
            "origin": presentation.get("origin"),
            "payload_source": "server_hydrated",
            "project_revision": revision,
            "record_ids": record_ids,
            "payload_hash": canonical_hash(
                {"record_ids": record_ids, "project_revision": revision}
            ),
        }
        if frame_valid:
            frame["protocol_version"] = "1"
            frame["slot_id"] = slot_id
        frames.append(frame)
    valid_finals = [
        frame
        for frame in frames
        if frame.get("frame_type") == "final"
        and frame.get("validation_status") == "valid"
        and frame.get("discarded") is False
    ]
    if valid_finals:
        valid_finals[-1]["selected"] = True
    return frames


def _provider_was_invoked(events) -> bool:
    """Count only a host-mapped SDK ``ResultMessage`` as provider invocation.

    SDK system/init and assistant-stream events can be emitted before an upstream
    request succeeds, so treating either as proof of a real provider call creates
    a false-positive L2 claim.  ``run.result`` is the durable event mapped from the
    SDK's terminal ``ResultMessage``; require its typed discriminator fields too.
    """

    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        if (
            event.type == EventType.RUN_RESULT.value
            and isinstance(data.get("subtype"), str)
            and bool(data["subtype"].strip())
            and isinstance(data.get("is_error"), bool)
        ):
            return True
    return False


def _prompt_snapshot(ledger, run, initial_memories: list[Any]) -> dict[str, Any]:
    """Verify the exact durable prefix receipt against a locally rebuilt prompt."""

    refresh = run.context_refresh
    durable_hash = refresh.prompt_prefix_hash if refresh is not None else None
    rebuilt = build_system_prompt_append(ledger, run, initial_memories)
    rebuilt_hash = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
    verified = (
        isinstance(durable_hash, str)
        and durable_hash == rebuilt_hash
        and rebuilt.startswith(SECURITY_PREAMBLE)
    )
    return {
        "prompt_hash": durable_hash or rebuilt_hash,
        "security_preamble_pinned": verified,
        "current_user_message_id": "msg_l2_current",
        # This driver creates a fresh project and first run for every task.  If
        # reuse/resume is added later it must capture the SDK transcript before
        # this can remain true.
        "history_complete": run.resume_source_run_id is None,
    }


def _external_effects(actions: list[Any]) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for item in actions:
        action = _model_json(item)
        execution = action.get("execution_request") if isinstance(action, dict) else None
        if not isinstance(execution, dict):
            continue
        receipt = execution.get("receipt")
        if (
            execution.get("external_status") != ExternalExecutionStatus.SUCCEEDED.value
            or not isinstance(receipt, dict)
        ):
            continue
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        effects.append(
            {
                "id": receipt.get("remote_action_id"),
                "action_id": action.get("id"),
                "effect_type": action.get("operation"),
                "status": "succeeded",
                "idempotency_key": receipt.get("idempotency_key"),
                "expected_version": target.get("expected_version"),
                "result_version": receipt.get("remote_version"),
                "execution_receipt_id": receipt.get("id"),
            }
        )
    return effects


def _execution_receipts(actions: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in actions:
        action = _model_json(item)
        execution = action.get("execution_request") if isinstance(action, dict) else None
        receipt = execution.get("receipt") if isinstance(execution, dict) else None
        if not isinstance(receipt, dict):
            continue
        rows.append(
            {
                **receipt,
                "kind": "external_execution",
                "issuer": "ophira_host",
                # Reload + Pydantic validation has already checked the durable
                # action/receipt relationship before this serializer is called.
                "integrity_verified": True,
                "subject_id": receipt.get("remote_action_id"),
                "status": "succeeded",
                "result_version": receipt.get("remote_version"),
            }
        )
    return rows


def _capability_writes(events) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    read_only = {*READ_ONLY_BUILTIN_TOOLS, *READ_ONLY_MCP_TOOL_NAMES, "Glob", "Grep", "Skill"}
    for event in events:
        if event.type == EventType.ARTIFACT_REGISTERED.value:
            rows.append(
                {
                    "actor_type": (
                        "subagent" if event.actor.type == "subagent" else "main_agent"
                    ),
                    "actor_name": event.actor.name,
                    "tool_name": "host_artifact_registration",
                    "target": "project_ledger",
                    "event_sequence": event.sequence,
                }
            )
            continue
        if event.type != EventType.TOOL_COMPLETED.value:
            continue
        tool_name = event.data.get("tool_name")
        if not isinstance(tool_name, str) or tool_name in read_only:
            continue
        leaf = tool_name.rsplit("__", 1)[-1]
        if tool_name in {"Write", "Edit"}:
            target = "workspace_artifact"
        elif tool_name in {"Bash", "Delete"}:
            target = "workspace_or_process"
        elif leaf.startswith(("record_", "update_", "invalidate_")):
            target = "project_ledger"
        elif leaf.startswith(("propose_memory",)):
            target = "memory_proposal"
        elif leaf.startswith(
            (
                "create_schedule",
                "pause_schedule",
                "create_standing_intent",
                "trigger_standing_intent",
                "send_notification",
            )
        ):
            target = "automation_commitment"
        elif leaf.startswith(("stage_commercial_action", "apply_commercial_action")):
            target = "commercial_action"
        else:
            # Unknown successful non-read-only tools remain visible and are not
            # silently classified as safe reads.
            target = "unclassified_mutation"
        rows.append(
            {
                "actor_type": (
                    "subagent" if event.actor.type == "subagent" else "main_agent"
                ),
                "actor_name": event.actor.name,
                "tool_name": tool_name,
                "target": target,
                "event_sequence": event.sequence,
            }
        )
    return rows


def _capture_status(
    events,
    *,
    connector_invoked: bool,
    receipts_complete: bool,
) -> dict[str, bool]:
    completed_tools = {
        str(event.data.get("tool_name"))
        for event in events
        if event.type == EventType.TOOL_COMPLETED.value
        and isinstance(event.data.get("tool_name"), str)
    }
    calculation_observed = any(
        _tool_capability(name) == "calculate" for name in completed_tools
    )
    permission_requests = {
        event.data.get("request_id")
        for event in events
        if event.type == EventType.PERMISSION_REQUESTED.value
        and isinstance(event.data.get("request_id"), str)
    }
    permission_resolutions = {
        event.data.get("request_id")
        for event in events
        if event.type in {
            EventType.PERMISSION_RESOLVED.value,
            EventType.PERMISSION_TIMED_OUT.value,
        }
        and isinstance(event.data.get("request_id"), str)
    }
    return {
        # Calculation results are not yet serialized into the grounded metric
        # contract.  A calculation tool therefore makes the snapshot fail closed.
        "metrics": not calculation_observed,
        "receipts": receipts_complete,
        "presentations": True,
        "backend_state": not connector_invoked,
        "authority_uses": permission_requests <= permission_resolutions,
        "capability_writes": True,
        # There is no deterministic observer proving that instructions embedded
        # in file/web data were not followed.  Keep this false for these fixture
        # tasks until a calibrated judge or explicit violation signal exists.
        "followed_external_instructions": False,
    }


def _artifact_snapshots(ledger, project) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact in project.artifacts:
        if artifact.path.startswith("uploads/"):
            continue
        text = ""
        if Path(artifact.name).suffix.casefold() in {".md", ".txt", ".csv"}:
            try:
                _, path = ledger.get_artifact(project.id, artifact.id)
                if path.stat().st_size <= 200_000:
                    text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError, KeyError):
                text = ""
        rows.append(
            {
                "id": artifact.id,
                "name": artifact.name,
                "mime": artifact.mime,
                "text": text,
            }
        )
    return rows


def _receipt_snapshots(
    ledger,
    project_id: str,
    run_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    loader = getattr(ledger, "list_tool_receipts", None)
    replayer = getattr(ledger, "replay_tool_receipt", None)
    if not callable(loader) or not callable(replayer):
        return [], False
    try:
        rows = []
        for item in loader(project_id, run_id=run_id):
            replay = replayer(project_id, item.id)
            raw = _model_json(item)
            rows.append(
                {
                    **raw,
                    "kind": "source_observation",
                    "integrity_verified": replay.integrity_verified,
                    "expired": replay.expired,
                    # replay_tool_receipt recomputes and verifies this hash before return.
                    "replayed_content_hash": item.content_hash,
                }
            )
        return rows, True
    except (OSError, ValueError, KeyError):
        # Empty data and failed capture are different states.  The caller stores
        # this as an explicit incomplete surface so the gate fails closed.
        return [], False


def _last_assistant_text(events) -> str:
    text = ""
    for event in events:
        if event.type == "assistant.message.completed":
            candidate = event.data.get("text")
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
    return text


def build_task_snapshot(
    *,
    spec: dict,
    provider_id: str,
    run_payload: dict[str, Any],
    events,
    ledger,
    initial_project,
    initial_memories: list[Any],
    initial_grants: list[Any],
    policy_version: str,
    actions: list[Any],
) -> dict[str, Any]:
    """Serialize all L2 grading inputs without importing eval logic into the product."""

    project = ledger.get_project(run_payload["project_id"])
    run = ledger.get_run(run_payload["id"])
    status = str(run_payload.get("status", "failed"))
    session_id = run.sdk_session_id
    event_rows = [
        {
            "sequence": event.sequence,
            "type": event.type,
            "actor_type": event.actor.type,
            "actor_name": event.actor.name,
        }
        for event in events
    ]
    capabilities = [
        "read",
        "write",
        "calculate",
        "ledger",
        "presentation",
        "web",
        "skill",
        "shell",
        "mcp",
    ]
    output_receipts, receipts_complete = _receipt_snapshots(
        ledger,
        project.id,
        run_payload["id"],
    )
    action_rows = [_model_json(item) for item in actions]
    effects = _external_effects(actions)
    provider_invoked = _provider_was_invoked(events)
    tool_rows = _tool_calls(events)
    connector_invoked = bool(effects) or any(
        str(row.get("tool_name", "")).endswith("read_commercial_target")
        and row.get("status") == "completed"
        for row in tool_rows
    )
    # Commercial actions are captured even though the four current benchmark tasks
    # prohibit side effects. Their presence remains visible to task graders.
    return {
        "schema_version": "1",
        "captured_at": datetime.now(UTC).isoformat(),
        "case_context": {
            "task_id": spec["id"],
            "scenario": spec["scenario"],
            "synthetic": True,
            "live_provider_invoked": provider_invoked,
            "live_connector_invoked": connector_invoked,
        },
        "input": {
            "system": _prompt_snapshot(ledger, run, initial_memories),
            "config": {
                "provider_mode": "real" if provider_invoked else "uninvoked",
                "connector_mode": "real" if connector_invoked else "none",
                "provider_id": provider_id,
                "policy_version": policy_version,
                "allowed_capabilities": capabilities,
                "allowed_effects": [],
            },
            "messages": [
                {
                    "id": "msg_l2_current",
                    "role": "user",
                    "trust": "user",
                    "content": spec["prompt"],
                }
            ],
            "project_ledger": _project_snapshot(initial_project),
            "memory": {
                "items": [_model_json(item) for item in initial_memories],
                "capture_complete": True,
            },
            "receipts": [],
            "permissions": _permission_snapshot(events, initial_grants, run),
            "backend_state": {
                "kind": "none",
                "before": {},
                "after": {},
                "calls": [],
            },
        },
        "output": {
            "run": {
                "id": run_payload["id"],
                "status": status,
                "outcome": "success" if status == "completed" else "failure",
                "timed_out": status == "cancelled"
                and "exceeded" in str(run_payload.get("error", "")).casefold(),
                "error_code": run_payload.get("error_code"),
                "identity_binding": run.identity_binding,
                "tenant_id": run.tenant_id,
                "principal_id": run.principal_id,
                "session_id": session_id,
                "reported_external_success": bool(effects),
            },
            "events": event_rows,
            "tool_calls": tool_rows,
            "project_ledger": _project_snapshot(project),
            "artifacts": _artifact_snapshots(ledger, project),
            "metrics": [],
            "presentations": _presentations(events),
            "actions": action_rows,
            "receipts": [*output_receipts, *_execution_receipts(actions)],
            "external_effects": effects,
            "authority_uses": [
                {
                    "kind": "task",
                    "message_id": "msg_l2_current",
                }
            ],
            "capability_writes": _capability_writes(events),
            "followed_external_instructions": [],
            "capture_status": _capture_status(
                events,
                connector_invoked=connector_invoked,
                receipts_complete=receipts_complete,
            ),
            "assistant_text": _last_assistant_text(events),
        },
    }


async def run_task(
    client,
    app,
    ledger,
    spec: dict,
    provider_id: str,
    report_dir: Path,
    timeout_s: float,
    suite: dict[str, Any],
) -> dict:
    expect = spec["expect"]
    response = await client.post(
        "/api/projects",
        json={
            "name": f"L2 {spec['id']}",
            "objective": f"L2 基准任务 {spec['id']}（场景 {spec['scenario']}，合成 fixture）",
        },
    )
    response.raise_for_status()
    project_id = response.json()["id"]

    for fixture in spec["fixtures"]:
        source = TASKS_DIR / fixture["path"]
        mime = fixture.get("mime") or MIME_DEFAULTS.get(source.suffix.lstrip("."), "text/plain")
        upload = await client.post(
            f"/api/projects/{project_id}/files",
            params={"filename": fixture["upload_as"], "mime": mime},
            content=source.read_bytes(),
        )
        upload.raise_for_status()

    initial_project = ledger.get_project(project_id)
    initial_memories = [
        *app.state.memory.list(
            scope=MemoryScope.USER,
            scope_id=USER_SCOPE_ID,
            status=MemoryStatus.APPROVED,
        ),
        *app.state.memory.list(
            scope=MemoryScope.PROJECT,
            scope_id=project_id,
            status=MemoryStatus.APPROVED,
        ),
    ]
    initial_grants = [
        grant for grant in app.state.grants.list() if grant.project_id in {None, project_id}
    ]
    policy_version = app.state.policy.get().updated_at.isoformat()

    policy = PermissionPolicy()
    run_payload = await drive_run(
        client,
        ledger,
        project_id,
        spec["prompt"],
        provider_id=provider_id,
        policy=policy,
        timeout_s=timeout_s,
    )
    events = ledger.list_events(run_payload["id"])
    verdict = verify_events(
        events,
        expect_deny=bool(expect.get("deny_marker")),
        required_coverage=tuple(expect.get("required_coverage", [])) or None,
    )

    project = ledger.get_project(project_id)
    task_missing: list[str] = []
    if len(project.evidence) < int(expect.get("evidence_min", 0)):
        task_missing.append(f"evidence:{len(project.evidence)}<{expect.get('evidence_min')}")
    if expect.get("decision_required") and not project.decisions:
        task_missing.append("decision:absent")
    suffix = expect.get("artifact_suffix")
    if suffix:
        generated = [
            artifact
            for artifact in project.artifacts
            if artifact.name.endswith(suffix) and not artifact.path.startswith("uploads/")
        ]
        if not generated:
            task_missing.append(f"artifact:*{suffix} not produced")

    snapshot = build_task_snapshot(
        spec=spec,
        provider_id=provider_id,
        run_payload=run_payload,
        events=events,
        ledger=ledger,
        initial_project=initial_project,
        initial_memories=initial_memories,
        initial_grants=initial_grants,
        policy_version=policy_version,
        actions=app.state.commercial_actions.list(project_id),
    )
    profile = spec["minimum_pass"]["profile"]
    snapshot_verdict = evaluate_minimum_pass(
        snapshot,
        spec["minimum_pass"],
        suite=suite,
    )
    task_missing.extend(
        f"minimum_pass:{finding.code}:{finding.path or '-'}"
        for finding in snapshot_verdict.findings
    )

    bundle_dir = write_bundle(
        report_dir,
        spec["id"],
        run_payload,
        verdict,
        events,
        task_missing,
        notes={
            "scenario": spec["scenario"],
            "minimum_pass": spec["minimum_pass"],
            "snapshot_profile": profile,
            "snapshot_accepted": snapshot_verdict.accepted,
        },
    )
    (bundle_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "snapshot-eval.json").write_text(
        json.dumps(snapshot_verdict.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    passed = verdict.ok and not task_missing
    telemetry = build_run_telemetry(ledger.get_run(run_payload["id"]), events)
    return {
        "task": spec["id"],
        "passed": passed,
        "verdict_missing": verdict.missing,
        "task_missing": task_missing,
        "cost_usd_list_price": verdict.total_cost_usd,
        "num_turns": verdict.num_turns,
        "run_id": run_payload["id"],
        "snapshot_profile": profile,
        "snapshot_accepted": snapshot_verdict.accepted,
        "snapshot_findings": [finding.code for finding in snapshot_verdict.findings],
        "total_task_duration_ms": _metric_value(
            telemetry.latency.total_task_duration_ms
        ),
        "ttft_ms": _metric_value(telemetry.latency.ttft_ms),
        "first_useful_typed_card_ms": _metric_value(
            telemetry.latency.first_useful_typed_card_ms
        ),
        "input_tokens": _metric_value(telemetry.tokens.input_tokens),
        "output_tokens": _metric_value(telemetry.tokens.output_tokens),
        "cache_read_input_tokens": _metric_value(
            telemetry.tokens.cache_read_input_tokens
        ),
        "cache_creation_input_tokens": _metric_value(
            telemetry.tokens.cache_creation_input_tokens
        ),
        "cache_hit": _metric_value(telemetry.tokens.cache_hit),
        "observed_cost_usd": _metric_value(telemetry.cost.estimated_cost_usd),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--task", default=None, help="run only this task id")
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--timeout-s", type=float, default=900)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate task specs and fixtures without touching an endpoint",
    )
    args = parser.parse_args()

    try:
        suite = load_suite()
    except ValueError as exc:
        print(f"Snapshot evaluator is invalid: {exc}")
        return 2
    tasks = load_tasks(args.task)
    problems = {spec["id"]: validate_task(spec, suite) for spec in tasks}
    for task_id, issues in problems.items():
        status = "OK" if not issues else f"INVALID: {issues}"
        print(f"[spec] {task_id}: {status}")
    if any(problems.values()):
        return 2
    if args.dry_run:
        print(f"Dry run complete: {len(tasks)} task spec(s) valid.")
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir = REPO_ROOT / "evals" / "reports" / f"l2-{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(Settings(max_budget_usd=args.budget_usd))
    ledger = app.state.ledger
    transport = httpx.ASGITransport(app=app)
    results = []
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://ophira-l2",
        timeout=httpx.Timeout(60),
    ) as client:
        for spec in tasks:
            print(f"\n=== L2 task {spec['id']} on {args.provider} ===")
            try:
                result = await run_task(
                    client,
                    app,
                    ledger,
                    spec,
                    args.provider,
                    report_dir,
                    args.timeout_s,
                    suite,
                )
            except Exception as exc:  # noqa: BLE001
                result = {"task": spec["id"], "passed": False, "error": str(exc)}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, indent=2))

    all_passed = all(item.get("passed") for item in results)
    (report_dir / "overall.json").write_text(
        json.dumps(
            {
                "provider": args.provider,
                "generated_at": datetime.now(UTC).isoformat(),
                "all_passed": all_passed,
                "aggregate": aggregate_results(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nL2 overall: {'PASS' if all_passed else 'FAIL'} · bundles in {report_dir}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
