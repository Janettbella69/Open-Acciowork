"""Everything the product keeps is a file (textbook p.16: 记忆就是文件).

    data_dir/projects/<project_id>/
        project.json        name, creation time, the SDK session to resume
        workspace/          the Agent's computer: its cwd, the only place it writes
        claude/             CLAUDE_CONFIG_DIR: the SDK's own transcripts and auto memory
        snapshots.git/      workspace history, kept outside the Agent's reach
        runs/<id>.json      one turn: prompt, status, timestamps
        runs/<id>.jsonl     what happened in that turn, one event per line, in order
        feedback.jsonl      "this is wrong" reports from the person using it (p.73)

There is no database and no derived index; listing reads the folders.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_LISTED_FILES = 1000
_SNAPSHOT_IDENTITY = ["-c", "user.name=TradeMind", "-c", "user.email=snapshots@localhost"]


class NotFoundError(LookupError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotFoundError(path.name) from exc


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip(" .")
    return cleaned[:120] or "file"


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.root = (data_dir / "projects").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- projects ---------------------------------------------------------

    def project_dir(self, project_id: str) -> Path:
        if not re.fullmatch(r"prj_[0-9a-f]{16}", project_id):
            raise NotFoundError(project_id)
        path = self.root / project_id
        if not (path / "project.json").exists():
            raise NotFoundError(project_id)
        return path

    def workspace(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "workspace"

    def config_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "claude"

    def create_project(self, name: str) -> dict[str, Any]:
        project_id = new_id("prj")
        path = self.root / project_id
        for sub in ("workspace", "claude", "runs"):
            (path / sub).mkdir(parents=True)
        (path / "claude").chmod(0o700)
        project = {
            "id": project_id,
            "name": name.strip()[:80] or "新项目",
            "created_at": now(),
            "session_id": None,
        }
        _write_json(path / "project.json", project)
        self._git(project_id, "init", "--quiet")
        self.snapshot(project_id, "project created")
        return project

    def get_project(self, project_id: str) -> dict[str, Any]:
        return _read_json(self.project_dir(project_id) / "project.json")

    def update_project(self, project_id: str, **changes: Any) -> dict[str, Any]:
        project = self.get_project(project_id) | changes
        _write_json(self.project_dir(project_id) / "project.json", project)
        return project

    def list_projects(self) -> list[dict[str, Any]]:
        projects = []
        for path in self.root.glob("prj_*/project.json"):
            projects.append(_read_json(path))
        return sorted(projects, key=lambda p: p["created_at"], reverse=True)

    # -- runs -------------------------------------------------------------

    def _runs_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "runs"

    def create_run(self, project_id: str, prompt: str) -> dict[str, Any]:
        run = {
            "id": new_id("run"),
            "project_id": project_id,
            "prompt": prompt,
            "status": "running",
            "created_at": now(),
            "finished_at": None,
            "error": None,
        }
        _write_json(self._runs_dir(project_id) / f"{run['id']}.json", run)
        (self._runs_dir(project_id) / f"{run['id']}.jsonl").touch()
        return run

    def finish_run(self, project_id: str, run_id: str, status: str, error: str | None) -> dict:
        path = self._runs_dir(project_id) / f"{run_id}.json"
        run = _read_json(path) | {"status": status, "error": error, "finished_at": now()}
        _write_json(path, run)
        return run

    def find_run(self, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"run_[0-9a-f]{16}", run_id):
            raise NotFoundError(run_id)
        for path in self.root.glob(f"prj_*/runs/{run_id}.json"):
            return _read_json(path)
        raise NotFoundError(run_id)

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        runs = [_read_json(p) for p in self._runs_dir(project_id).glob("run_*.json")]
        return sorted(runs, key=lambda r: r["created_at"])

    def close_interrupted_runs(self) -> None:
        """A turn still marked running when the process starts was cut off by a restart.

        Say so in its record and its log, so nobody waits for an ending that will not come.
        """
        reason = "后台重启了，这一轮中断了。"
        for path in self.root.glob("prj_*/runs/run_*.json"):
            run = _read_json(path)
            if run["status"] != "running":
                continue
            events = self.read_events(run["project_id"], run["id"])
            self.finish_run(run["project_id"], run["id"], "failed", reason)
            self.append_event(run["project_id"], run["id"], json.dumps({
                "seq": (events[-1]["seq"] if events else 0) + 1, "at": now(), "source": "host",
                "type": "run.finished", "data": {"status": "failed", "error": reason},
            }, ensure_ascii=False))

    def append_event(self, project_id: str, run_id: str, line: str) -> None:
        with (self._runs_dir(project_id) / f"{run_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_events(self, project_id: str, run_id: str) -> list[dict[str, Any]]:
        path = self._runs_dir(project_id) / f"{run_id}.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [json.loads(line) for line in lines if line.strip()]

    # -- workspace files --------------------------------------------------

    def list_files(self, project_id: str) -> list[dict[str, Any]]:
        workspace = self.workspace(project_id)
        files = []
        for path in sorted(workspace.rglob("*")):
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                files.append({
                    "path": path.relative_to(workspace).as_posix(),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                })
                if len(files) >= MAX_LISTED_FILES:
                    break
        return files

    def resolve_file(self, project_id: str, relative: str) -> Path:
        workspace = self.workspace(project_id).resolve()
        target = (workspace / relative).resolve()
        if not target.is_relative_to(workspace) or not target.is_file():
            raise NotFoundError(relative)
        return target

    def save_upload(self, project_id: str, filename: str, data: bytes) -> list[str]:
        """Store an upload in the workspace; a PDF also gets a Markdown copy of its text.

        Converting before the Agent starts is the textbook's own example of legitimate
        preprocessing (p.20): not every model endpoint accepts PDF input.
        """
        uploads = self.workspace(project_id) / "uploads"
        uploads.mkdir(exist_ok=True)
        target = uploads / _safe_name(filename)
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            n += 1
            target = uploads / f"{stem}-{n}{suffix}"
        target.write_bytes(data)
        saved = [target]
        if target.suffix.lower() == ".pdf":
            saved.append(_pdf_markdown(target))
        workspace = self.workspace(project_id)
        return [p.relative_to(workspace).as_posix() for p in saved]

    # -- feedback ---------------------------------------------------------

    def add_feedback(self, project_id: str, run_id: str | None, text: str) -> dict[str, Any]:
        entry = {"at": now(), "run_id": run_id, "text": text.strip()}
        with (self.project_dir(project_id) / "feedback.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    # -- snapshots (a Hook-style side effect: backup before the Agent acts) ----

    def snapshot(self, project_id: str, label: str) -> None:
        self._git(project_id, "add", "-A")
        status = self._git(project_id, "status", "--porcelain")
        has_head = self._git(project_id, "rev-parse", "--verify", "-q", "HEAD", check=False)
        if status.strip() or not has_head.strip():
            self._git(project_id, *_SNAPSHOT_IDENTITY, "commit", "-q", "--allow-empty",
                      "-m", label)

    def _git(self, project_id: str, *args: str, check: bool = True) -> str:
        path = self.project_dir(project_id)
        result = subprocess.run(
            ["git", f"--git-dir={path / 'snapshots.git'}", f"--work-tree={path / 'workspace'}",
             *args],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout


def _pdf_markdown(source: Path) -> Path:
    from pypdf import PdfReader

    out = source.with_name(source.name + ".md")
    try:
        reader = PdfReader(source)
        pages = [page.extract_text() or "" for page in reader.pages]
        empty = "（这一页没有可提取的文字，可能是扫描件）"
        body = "\n\n".join(f"## 第 {i} 页\n\n{text.strip() or empty}"
                           for i, text in enumerate(pages, 1))
    except Exception as exc:  # a broken PDF still gets an honest companion file
        body = f"（无法从这个 PDF 提取文字：{type(exc).__name__}）"
    out.write_text(f"# {source.name}\n\n{body}\n", encoding="utf-8")
    return out


def require_git() -> None:
    if shutil.which("git") is None:
        raise RuntimeError("git is required for workspace snapshots")
