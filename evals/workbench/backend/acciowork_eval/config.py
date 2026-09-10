"""Paths owned by this workbench, independent of the working directory and product."""

import os
from pathlib import Path

WORKBENCH_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8101


def database_path(explicit: Path | None = None) -> Path:
    value = explicit if explicit is not None else os.getenv("ACCIOWORK_EVAL_DB")
    return Path(value).expanduser().resolve() if value else WORKBENCH_ROOT / "data/archive.sqlite3"
