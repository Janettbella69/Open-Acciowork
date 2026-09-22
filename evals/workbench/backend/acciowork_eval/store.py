"""Separate SQLite archive, event index and owner review records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .contract import COMPARISON_CONTROLS, COMPARISON_FIELDS, DELTA_TYPES, JUDGES
from .importer import Capture, encode, normalize_trace, prepare_report


def now():
    return datetime.now(UTC).isoformat()


class Conflict(ValueError):
    """A concurrent review changed; the owner must reload before saving."""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS archives (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files (sha TEXT PRIMARY KEY, content BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, archive_id TEXT NOT NULL REFERENCES archives(id),
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    trace_id TEXT NOT NULL REFERENCES runs(id), position INTEGER NOT NULL,
                    sequence INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(trace_id, position));
                CREATE INDEX IF NOT EXISTS event_sequence ON events(trace_id, sequence);
                CREATE INDEX IF NOT EXISTS event_type ON events(trace_id, type);
                CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_items (
                    trace_id TEXT NOT NULL REFERENCES runs(id),
                    item_id TEXT NOT NULL REFERENCES items(id),
                    PRIMARY KEY(trace_id, item_id));
                CREATE TABLE IF NOT EXISTS labels (
                    item_id TEXT PRIMARY KEY REFERENCES items(id), payload TEXT NOT NULL,
                    revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (
                    trace_id TEXT PRIMARY KEY REFERENCES runs(id), payload TEXT NOT NULL,
                    revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS review_history (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_report(self, report: Path, runs_dir=None, samples=None):
        prepared = prepare_report(report, runs_dir, samples)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM archives WHERE id=?", (prepared["id"],)).fetchone():
                return {"id": prepared["id"], "name": prepared["name"], "created": False}
            archive = {k: v for k, v in prepared.items() if k not in {"capture", "tasks", "batch"}}
            archive.update(imported_at=now(), case_count=len(prepared["tasks"]))
            db.execute("INSERT INTO archives VALUES (?, ?)", (archive["id"], encode(archive)))
            for sha, content in prepared["capture"].files.items():
                db.execute("INSERT OR IGNORE INTO files VALUES (?, ?)", (sha, content))
            for task in prepared["tasks"]:
                trace, events, items = normalize_trace(prepared, task)
                db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?)", (trace["id"], archive["id"], encode(trace))
                )
                db.executemany(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                    (
                        (trace["id"], i, event["sequence"], event["type"], encode(event))
                        for i, event in enumerate(events)
                    ),
                )
                for item in items:
                    existing = db.execute(
                        "SELECT payload FROM items WHERE id=?", (item["item_id"],)
                    ).fetchone()
                    if existing:
                        old = json.loads(existing["payload"])
                        for key in ("run_id", "judge", "item", "split", "source"):
                            if old.get(key) != item.get(key):
                                raise ValueError(
                                    f"Conflicting Gold identity/split: {item['item_id']}"
                                )
                    db.execute(
                        "INSERT OR IGNORE INTO items VALUES (?, ?)", (item["item_id"], encode(item))
                    )
                    db.execute(
                        "INSERT INTO run_items VALUES (?, ?)", (trace["id"], item["item_id"])
                    )
        return {
            "id": archive["id"],
            "name": archive["name"],
            "created": True,
            "case_count": archive["case_count"],
        }

    def _items(self, db, trace_id=None):
        where = "WHERE i.id IN (SELECT item_id FROM run_items WHERE trace_id=?)" if trace_id else ""
        rows = db.execute(
            f"""SELECT i.payload, l.payload AS label, l.revision
            FROM items i LEFT JOIN labels l ON l.item_id=i.id {where} ORDER BY i.id""",
            (trace_id,) if trace_id else (),
        )
        return [
            {
                **json.loads(r["payload"]),
                "annotation": json.loads(r["label"]) if r["label"] else None,
                "revision": r["revision"] or 0,
            }
            for r in rows
        ]

    def items(self):
        with self.connect() as db:
            items = self._items(db)
            links = {}
            for row in db.execute("SELECT item_id, trace_id FROM run_items ORDER BY trace_id"):
                links.setdefault(row["item_id"], []).append(row["trace_id"])
            return [{**i, "trace_ids": links.get(i["item_id"], [])} for i in items]

    def _trace(self, db, row, detail=False):
        trace = json.loads(row["payload"])
        items = self._items(db, trace["id"])
        labeled = sum(i["annotation"] is not None for i in items)
        trace.update(item_count=len(items), labeled_count=labeled)
        trace["quality_status"] = (
            "no_items"
            if not items
            else "labeled"
            if labeled == len(items)
            else "partial"
            if labeled
            else "unlabeled"
        )
        review = db.execute(
            "SELECT payload, revision FROM reviews WHERE trace_id=?", (trace["id"],)
        ).fetchone()
        trace["review"] = (
            {**json.loads(review["payload"]), "revision": review["revision"]}
            if review
            else {
                "status": "pending",
                "open_codes": [],
                "notes": "",
                "revision": 0,
            }
        )
        trace["issues"] = [
            {"file": name, **ref}
            for name, ref in trace["sources"].items()
            if ref["status"] != "available" or ref.get("sequence_warning")
        ]
        if detail:
            trace["items"] = items
            return trace
        keys = {
            "id",
            "experiment_id",
            "experiment_name",
            "case_key",
            "run_id",
            "trace_id",
            "execution_status",
            "duration_ms",
            "cost_usd",
            "cost_basis",
            "snapshot_accepted",
            "legacy_passed",
            "quality_status",
            "event_count",
            "versions",
            "item_count",
            "labeled_count",
            "review",
            "issues",
            "ordinal",
        }
        return {key: trace[key] for key in keys}

    def traces(self, archive_id=None):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM runs" + (" WHERE archive_id=?" if archive_id else ""),
                (archive_id,) if archive_id else (),
            ).fetchall()
            return sorted(
                [self._trace(db, row) for row in rows],
                key=lambda t: (t["experiment_name"], -t["ordinal"]),
                reverse=True,
            )

    def trace(self, trace_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (trace_id,)).fetchone()
            if not row:
                raise KeyError(trace_id)
            return self._trace(db, row, detail=True)

    def experiments(self):
        traces = self.traces()
        with self.connect() as db:
            archives = [
                json.loads(row["payload"]) for row in db.execute("SELECT payload FROM archives")
            ]
        for archive in archives:
            runs = [t for t in traces if t["experiment_id"] == archive["id"]]
            archive["counts"] = {
                "completed": sum(t["execution_status"] == "completed" for t in runs),
                "snapshot_accepted": sum(t["snapshot_accepted"] is True for t in runs),
                "snapshot_rejected": sum(t["snapshot_accepted"] is False for t in runs),
                "snapshot_unknown": sum(t["snapshot_accepted"] is None for t in runs),
                "items": sum(t["item_count"] for t in runs),
                "labeled": sum(t["labeled_count"] for t in runs),
            }
        return sorted(archives, key=lambda a: a.get("generated_at") or "", reverse=True)

    def events(self, trace_id, *, offset=0, limit=100, kind="key", query="", sequence=None):
        clauses, params = ["trace_id=?"], [trace_id]
        if sequence is not None:
            clauses.append("sequence=?")
            params.append(sequence)
        elif kind == "key":
            clauses.append(f"type NOT IN ({','.join('?' for _ in DELTA_TYPES)})")
            params.extend(DELTA_TYPES)
        elif kind != "all":
            clauses.append("type=?")
            params.append(kind)
        if query:
            clauses.append("payload LIKE ? ESCAPE '\\'")
            params.append(
                "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
        where = " AND ".join(clauses)
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (trace_id,)).fetchone():
                raise KeyError(trace_id)
            total = db.execute(f"SELECT COUNT(*) FROM events WHERE {where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT payload FROM events WHERE {where} "
                "ORDER BY sequence, position LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
            return {
                "total": total,
                "offset": offset,
                "events": [json.loads(r["payload"]) for r in rows],
            }

    def save_label(self, item_id, *, label, rationale, revision, owner_confirmed):
        if type(label) is not bool or owner_confirmed is not True:
            raise ValueError("A boolean verdict must be entered by the project owner")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("A rationale is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
            if not item:
                raise KeyError(item_id)
            current = db.execute(
                "SELECT revision FROM labels WHERE item_id=?", (item_id,)
            ).fetchone()
            if (current[0] if current else 0) != revision:
                raise Conflict("Label changed; reload before saving")
            record = {
                **json.loads(item["payload"]),
                "label": label,
                "rationale": rationale.strip(),
                "labeled_at": now(),
                "annotator": "project-owner",
                "origin": "owner-ui",
            }
            db.execute(
                "INSERT OR REPLACE INTO labels VALUES (?, ?, ?)",
                (item_id, encode(record), revision + 1),
            )
            db.execute(
                "INSERT INTO review_history(kind,target,payload,created_at) VALUES ('label',?,?,?)",
                (item_id, encode(record), now()),
            )
            return {"annotation": record, "revision": revision + 1}

    def save_review(self, trace_id, *, status, open_codes, notes, revision):
        if status not in {"pending", "reviewed", "flagged"}:
            raise ValueError("Invalid review status")
        if not isinstance(open_codes, list) or any(
            not isinstance(c, str) or not c.strip() for c in open_codes
        ):
            raise ValueError("Open codes must be nonempty strings")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (trace_id,)).fetchone():
                raise KeyError(trace_id)
            current = db.execute(
                "SELECT revision FROM reviews WHERE trace_id=?", (trace_id,)
            ).fetchone()
            if (current[0] if current else 0) != revision:
                raise Conflict("Review changed; reload before saving")
            record = {
                "status": status,
                "open_codes": sorted(set(c.strip() for c in open_codes)),
                "notes": notes,
                "reviewed_at": now(),
                "annotator": "local-reviewer",
            }
            db.execute(
                "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?)",
                (trace_id, encode(record), revision + 1),
            )
            db.execute(
                "INSERT INTO review_history(kind,target,payload,created_at) "
                "VALUES ('review',?,?,?)",
                (trace_id, encode(record), now()),
            )
            return {**record, "revision": revision + 1}

    def export_labels(self, judge):
        if judge not in JUDGES:
            raise KeyError(judge)
        with self.connect() as db:
            records = [
                json.loads(r[0]) for r in db.execute("SELECT payload FROM labels ORDER BY item_id")
            ]
        return "".join(encode(r) + "\n" for r in records if r["judge"] == judge)

    def import_labels(self, path: Path):
        """Import explicitly selected existing human records, never overwrite a changed label."""
        capture = Capture()
        ref, raw = capture.read(path, path.parent)
        if raw is None:
            raise ValueError(f"Unreadable labels: {ref}")
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        imported = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for record in records:
                if not isinstance(record, dict) or type(record.get("label")) is not bool:
                    raise ValueError("Existing Gold records require a boolean label")
                if not record.get("rationale") or not record.get("labeled_at"):
                    raise ValueError("Existing Gold records require rationale and labeled_at")
                item_id = record.get("item_id")
                item = db.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
                if item is None:
                    raise ValueError(f"Import the candidate batch before its labels: {item_id}")
                candidate = json.loads(item["payload"])
                for field in ("run_id", "item", "judge", "split", "source"):
                    if candidate.get(field) != record.get(field):
                        raise ValueError(f"Gold record changes candidate {field}: {item_id}")
                previous = db.execute(
                    "SELECT payload FROM labels WHERE item_id=?", (item_id,)
                ).fetchone()
                if previous:
                    if json.loads(previous["payload"]) != record:
                        raise Conflict(
                            f"Existing label differs; review it in the interface: {item_id}"
                        )
                    continue
                db.execute("INSERT INTO labels VALUES (?, ?, 1)", (item_id, encode(record)))
                db.execute(
                    "INSERT INTO review_history(kind,target,payload,created_at) "
                    "VALUES ('label-import',?,?,?)",
                    (item_id, encode({"record": record, "source": ref}), now()),
                )
                imported += 1
            db.execute("INSERT OR IGNORE INTO files VALUES (?, ?)", (ref["sha256"], raw))
        return {"imported": imported, "source": ref}

    def file(self, sha):
        with self.connect() as db:
            row = db.execute("SELECT content FROM files WHERE sha=?", (sha,)).fetchone()
            if not row:
                raise KeyError(sha)
            return row[0]

    def compare(self, left_id, right_id):
        known = {a["id"] for a in self.experiments()}
        if left_id not in known or right_id not in known:
            raise KeyError("Unknown experiment")
        left, right = self.traces(left_id), self.traces(right_id)
        rows = []
        for key in sorted({t["case_key"] for t in left + right}):
            a, b = (
                [t for t in left if t["case_key"] == key],
                [t for t in right if t["case_key"] == key],
            )
            versions = []
            if len(a) == len(b) == 1:
                for field in COMPARISON_FIELDS:
                    av, bv = a[0]["versions"].get(field), b[0]["versions"].get(field)
                    state = (
                        "unknown" if av is None or bv is None else "same" if av == bv else "changed"
                    )
                    versions.append({"field": field, "left": av, "right": bv, "state": state})
            comparable = (
                bool(versions)
                and all(v["state"] != "unknown" for v in versions)
                and all(v["state"] == "same" for v in versions if v["field"] in COMPARISON_CONTROLS)
            )
            change = (
                "added"
                if not a
                else "removed"
                if not b
                else "multiple_trials"
                if len(a) != 1 or len(b) != 1
                else "unknown"
            )
            if a and b and len(a) == len(b) == 1:
                ap, bp = a[0]["snapshot_accepted"], b[0]["snapshot_accepted"]
                if comparable and ap is not None and bp is not None:
                    change = "unchanged" if ap == bp else "improved" if bp else "regressed"
            rows.append(
                {
                    "case_key": key,
                    "left": a,
                    "right": b,
                    "versions": versions,
                    "comparable": comparable,
                    "change": change,
                }
            )
        return {"left_id": left_id, "right_id": right_id, "cases": rows}
