#!/usr/bin/env python3
"""Run OPHIRA's deterministic snapshot release gate.

With no arguments this executes every static accept/reject mutation case.  ``--snapshot``
evaluates one captured runtime snapshot against an explicit profile.  Neither mode
contacts a provider or connector.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from snapshot_eval import (
    DEFAULT_SUITE_PATH,
    evaluate_snapshot,
    evaluate_suite,
    load_suite,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE_PATH)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    try:
        suite = load_suite(args.suite)
    except ValueError as exc:
        print(f"SNAPSHOT GATE INVALID: {exc}", file=sys.stderr)
        return 2

    if args.snapshot is not None:
        if not args.profile:
            print("--profile is required with --snapshot", file=sys.stderr)
            return 2
        try:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"SNAPSHOT INVALID: {exc}", file=sys.stderr)
            return 2
        result = evaluate_snapshot(snapshot, args.profile, suite=suite)
        if args.json:
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"{'PASS' if result.accepted else 'FAIL'} profile={result.profile}")
            for finding in result.findings:
                location = f" [{finding.path}]" if finding.path else ""
                print(f"- {finding.code}{location}: {finding.message}")
        return 0 if result.accepted else 1

    if args.profile:
        print("--profile is only valid with --snapshot", file=sys.stderr)
        return 2
    results = evaluate_suite(suite)
    failed = [result for result in results if not result.passed]
    if args.json:
        print(
            json.dumps(
                {
                    "suite": str(args.suite),
                    "offline_only": True,
                    "case_count": len(results),
                    "passed": len(results) - len(failed),
                    "failed": len(failed),
                    "cases": [result.as_dict() for result in results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for result in results:
            actual = "accept" if result.accepted else "reject"
            mark = "PASS" if result.passed else "FAIL"
            print(f"[{mark}] {result.case_id}: expected={result.expected} actual={actual}")
            if not result.passed:
                for finding in result.findings:
                    print(f"  - {finding.code}: {finding.message}")
                if result.missing_expected_findings:
                    print(
                        "  - missing expected findings: "
                        + ", ".join(result.missing_expected_findings)
                    )
        print(
            f"Snapshot gate: {len(results) - len(failed)}/{len(results)} cases passed "
            "(offline contract/mutation suite; no live E2E claim)."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
