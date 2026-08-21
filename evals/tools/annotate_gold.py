#!/usr/bin/env python3
"""Local annotation UI for judge gold labeling (gold/README.md step 2).

Serves a single-page interface on 127.0.0.1 for the project owner to label the
candidate items sampled by sample_gold.py. Labels land in
evals/gold/<judge>/labels.jsonl (idempotent upsert per item). Owner-only local
tool: binds to loopback, no auth, do not expose. Pure stdlib — no main-system
imports.

Usage:
  python3 evals/tools/annotate_gold.py
  python3 evals/tools/annotate_gold.py \
      --samples evals/gold/samples/batch-20260820T190000Z.json --port 8765
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gold_annotator import GoldAnnotationService

HTML_PATH = Path(__file__).with_name("annotate_gold.html")


def latest_batch(samples_dir: Path) -> Path:
    batches = sorted(samples_dir.glob("batch-*.json"))
    if not batches:
        raise SystemExit(
            f"No batch files in {samples_dir}; run evals/tools/sample_gold.py first."
        )
    return batches[-1]


def make_handler(service: GoldAnnotationService):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:
            if self.path in {"/", "/index.html"}:
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send_json(200, service.state())
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
                if self.path == "/api/label":
                    record = service.save_label(
                        payload["item_id"],
                        label=bool(payload["label"]),
                        rationale=str(payload.get("rationale", "")),
                    )
                    self._send_json(200, {"saved": record})
                elif self.path == "/api/defer":
                    deferred = service.toggle_defer(payload["item_id"])
                    self._send_json(200, {"deferred": deferred})
                else:
                    self._send_json(404, {"error": "not found"})
            except KeyError as exc:
                self._send_json(400, {"error": f"unknown or missing key: {exc}"})
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})

        def log_message(self, fmt: str, *args) -> None:  # quiet
            pass

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=None,
                        help="batch file (default: latest in evals/gold/samples/)")
    parser.add_argument("--gold-dir", type=Path,
                        default=REPO_ROOT / "evals" / "gold")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    samples = args.samples or latest_batch(REPO_ROOT / "evals" / "gold" / "samples")
    service = GoldAnnotationService(samples, args.gold_dir)
    total = len(service.samples["items"])
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(service))
    print(f"Gold annotation UI: http://127.0.0.1:{args.port}/")
    print(f"Batch: {samples.name} · {total} items · labels -> {args.gold_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
