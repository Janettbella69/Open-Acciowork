"""Explicit local archive import and serving; no product or model execution."""

import argparse
import json
import os
from pathlib import Path

from .config import DEFAULT_PORT, database_path
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, help="Archive database; defaults to this workbench's data directory"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import", help="Copy selected archives into an independent database")
    importer.add_argument("--report", action="append", required=True, type=Path)
    importer.add_argument(
        "--runs-dir", type=Path, help="Explicit source for missing archived run/event files"
    )
    importer.add_argument(
        "--samples", type=Path, help="Existing Gold candidate batch; never creates labels"
    )
    server = sub.add_parser(
        "serve", help="Serve local archived data without loading the model runtime"
    )
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    labels = sub.add_parser(
        "import-labels", help="Import an explicitly selected existing Gold JSONL"
    )
    labels.add_argument("--file", required=True, type=Path)
    args = parser.parse_args()
    db_path = database_path(args.db)
    if args.command == "import":
        store = Store(db_path)
        for report in args.report:
            print(
                json.dumps(
                    store.import_report(report, args.runs_dir, args.samples), ensure_ascii=False
                )
            )
    elif args.command == "import-labels":
        print(json.dumps(Store(db_path).import_labels(args.file), ensure_ascii=False))
    else:
        import uvicorn

        os.environ["ACCIOWORK_EVAL_DB"] = str(db_path)
        uvicorn.run("acciowork_eval.api:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
