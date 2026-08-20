#!/usr/bin/env python3
"""Compact single-run digest for error-analysis open coding (PRD §3.11 step 2).

Prints, for one run: metadata and cost, the ordered tool-call sequence with input
hints, permission traffic, the final assistant text, and the ledger deltas
(evidence kinds/sources, decisions, artifacts) of its project.

Usage:
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/digest_run.py <run_id>
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/digest_run.py <run_id> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.storage import FileLedger  # noqa: E402

INPUT_HINT_KEYS = ("command", "file_path", "query", "url", "skill", "prompt")


def tool_hint(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in INPUT_HINT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{key}={value[:90]}"
    keys = ", ".join(list(tool_input)[:4])
    return f"keys: {keys}" if keys else ""


def digest(ledger: FileLedger, run_id: str) -> dict:
    run = ledger.get_run(run_id)
    events = ledger.list_events(run_id)
    project = ledger.get_project(run.project_id)

    tools = []
    permissions = []
    final_text = None
    result = {}
    for event in events:
        if event.type == "tool.started":
            tools.append(
                {
                    "seq": event.sequence,
                    "tool": event.data.get("tool_name"),
                    "hint": tool_hint(event.data.get("tool_input")),
                }
            )
        elif event.type in {"permission.requested", "permission.resolved", "permission.timed_out"}:
            permissions.append(
                {
                    "seq": event.sequence,
                    "event": event.type,
                    "tool": event.data.get("tool_name"),
                    "decision": event.data.get("decision"),
                }
            )
        elif event.type == "assistant.message.completed":
            text = event.data.get("text")
            if isinstance(text, str) and text.strip():
                final_text = text
        elif event.type == "run.result":
            result = {
                "is_error": event.data.get("is_error"),
                "num_turns": event.data.get("num_turns"),
                "total_cost_usd": event.data.get("total_cost_usd"),
                "duration_ms": event.data.get("duration_ms"),
            }

    return {
        "run": {
            "id": run.id,
            "status": run.status.value,
            "mode": run.mode.value,
            "provider": run.provider_profile_id,
            "prompt_head": " ".join(run.prompt.split())[:160],
            **result,
        },
        "tool_sequence": tools,
        "permissions": permissions,
        "final_text": final_text,
        "ledger": {
            "evidence": [
                {
                    "id": item.id,
                    "kind": item.kind.value,
                    "status": item.status.value,
                    "has_url": bool(item.source_url),
                    "has_path": bool(item.source_path),
                    "claim_head": item.claim[:80],
                }
                for item in project.evidence
            ],
            "hypotheses": [
                {"id": item.id, "statement_head": item.statement[:80]}
                for item in project.hypotheses
            ],
            "decisions": [
                {
                    "id": item.id,
                    "type": item.decision_type,
                    "status": item.status.value,
                    "outcome_head": item.outcome[:80],
                    "evidence_links": len(item.evidence_ids),
                }
                for item in project.decisions
            ],
            "artifacts": [
                {"name": item.name, "path": item.path, "version": item.version}
                for item in project.artifacts
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    ledger = FileLedger(Settings().data_dir)
    payload = digest(ledger, args.run_id)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    run = payload["run"]
    print(f"# run {run['id']} · {run['status']} · {run['provider']} · "
          f"turns={run.get('num_turns')} · cost={run.get('total_cost_usd')}")
    print(f"prompt: {run['prompt_head']}\n")
    print("## tool sequence")
    for item in payload["tool_sequence"]:
        print(f"  {item['seq']:>5}  {item['tool']:<44} {item['hint']}")
    print("\n## permissions")
    for item in payload["permissions"]:
        print(f"  {item['seq']:>5}  {item['event']:<22} {item['tool']} {item['decision'] or ''}")
    ledger_state = payload["ledger"]
    print(f"\n## ledger: {len(ledger_state['evidence'])} evidence · "
          f"{len(ledger_state['hypotheses'])} hypotheses · "
          f"{len(ledger_state['decisions'])} decisions · "
          f"{len(ledger_state['artifacts'])} artifacts")
    for item in ledger_state["evidence"]:
        source = "url" if item["has_url"] else "path" if item["has_path"] else "NONE"
        print(f"  [E] {item['kind']:<17} src={source:<4} {item['claim_head']}")
    for item in ledger_state["decisions"]:
        print(f"  [D] {item['type']:<8} links={item['evidence_links']} {item['outcome_head']}")
    for item in ledger_state["artifacts"]:
        print(f"  [A] v{item['version']} {item['path']}")
    if payload["final_text"]:
        print("\n## final text (head)")
        print("  " + payload["final_text"][:600].replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
