"""Exact-source cleanup of shared conversation-derived context; saved outputs stay."""

import re
import stat

from .backup_files import parse_json
from .erasure_files import encode
from .memory_erasure import jsonl
from .models import StandingIntent, TaskExperienceLedger
from .project_comments import CommentLedger, ProjectComment
from .skill_proposal_models import SkillProposal


def shared_context(selection, project_id, runs):
    selected = {run.id for run in runs}
    for directory in ("notifications", "feedback", "grants", "channel-deliveries"):
        for name, _ in selection.children(directory):
            if name.endswith(".tmp"):
                raise ValueError("Shared records have an interrupted write. Recover it first.")
            if not name.endswith(".json"):
                continue
            path = f"{directory}/{name}"
            value, _ = selection.record(path)
            if value.get("run_id") in selected or value.get("source_run_id") in selected:
                if value.get("project_id") not in (None, project_id):
                    raise ValueError("Conversation reference crosses project scope.")
                selection.change(path)
    for name, _ in selection.children("intents"):
        if not name.endswith(".json"):
            continue
        path = f"intents/{name}"
        value, _ = selection.record(path)
        if value.get("active_check_run_id") in selected:
            raise ValueError("Wait for the intent watcher to finalize this conversation.")
        if value.get("last_check_run_id") in selected:
            intent = StandingIntent.model_validate(value)
            if intent.project_id != project_id:
                raise ValueError("Intent reference crosses project scope.")
            # Keep counters, reservations and schedule bookmarks; deletion cannot replay a claim.
            value["last_check_error"] = None
            value["last_check_error_at"] = None
            selection.change(path, encode(value))
    for name, _ in selection.children("task-experiences"):
        if name.endswith(".tmp"):
            raise ValueError("Task experience has an interrupted write. Recover it first.")
        if not name.endswith(".json"):
            continue
        path = f"task-experiences/{name}"
        value, _ = selection.record(path)
        if name.endswith(".transaction.json"):
            raise ValueError("Finish pending task experience recovery before deletion.")
        state = TaskExperienceLedger.model_validate(value)
        if state.project_id != project_id:
            if any(
                row.source_run_id in selected for entry in state.entries for row in entry.revisions
            ):
                raise ValueError("Task experience reference crosses project scope.")
            continue
        changed, retained = False, []
        for entry in state.entries:
            # Versions of one lesson refine each other; retaining an older version
            # would silently reactivate a superseded observation.
            if any(row.source_run_id in selected for row in entry.revisions):
                changed = True
            else:
                retained.append(entry.model_dump(mode="json"))
        if changed:
            value["entries"] = retained
            value["revision"] += 1
            selection.change(path, encode(value))
    source_diaries_and_manifest(selection, project_id, runs)
    path = f"collaboration/comments/{project_id}.json"
    if any(
        name.startswith(f".{project_id}.json.") and name.endswith(".tmp")
        for name, _ in selection.children("collaboration/comments")
    ):
        raise ValueError("Project comments have an interrupted write. Recover it first.")
    raw = selection.read(path, optional=True, record=True)
    if raw is not None:
        record = CommentLedger.model_validate_json(raw)
        if (
            record.project_id != project_id
            or record.tenant_id != runs[0].tenant_id
            or record.identity_binding != runs[0].identity_binding
        ):
            raise ValueError("Comment storage does not match the conversation scope.")
        value = record.model_dump(mode="json")
        for item in value["comments"]:
            comment = ProjectComment.model_validate(item)
            anchor = comment.target.reference
            if (anchor.kind == "run" and anchor.id in selected) or (
                anchor.kind == "message" and anchor.run_id in selected
            ):
                item["target"]["label"] = "Deleted conversation"
        selection.change(path, encode(value))
    for name, info in selection.children("skill-proposals"):
        if name.startswith("."):
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Skill proposal storage is invalid.")
        path = f"skill-proposals/{name}/record.json"
        if any(
            child.endswith(".tmp") for child, _ in selection.children(f"skill-proposals/{name}")
        ):
            raise ValueError("Skill proposal has an interrupted write. Recover it first.")
        value, _ = selection.record(path)
        record = SkillProposal.model_validate(value)
        if record.project_id == project_id:
            for row in value["source_runs"]:
                if row["id"] in selected:
                    row["prompt_excerpt"] = ""
            selection.change(path, encode(value))


def source_diaries_and_manifest(selection, project_id, runs, *, whole_project=False):
    selected = {run.id for run in runs}
    directories = set() if whole_project else {f"projects/{project_id}/diary"}
    directories.update(
        f"agents/{run.agent_id}/diary" for run in runs if getattr(run, "agent_id", None)
    )
    heading = re.compile(rb"^## \d{2}:\d{2}:\d{2} \xc2\xb7 ([A-Za-z0-9_-]+)\s*$")
    for directory in sorted(directories):
        for name, _ in selection.children(directory):
            path = f"{directory}/{name}"
            raw = selection.read(path, record=True)
            kept, remove = [], False
            for line in raw.splitlines(keepends=True):
                match = heading.match(line)
                if line.startswith(b"## "):
                    remove = bool(match and match[1].decode() in selected)
                if not remove:
                    kept.append(line)
            selection.change(path, b"".join(kept))
    path = "_manifest.jsonl"
    raw = selection.read(path, optional=True, record=True)
    if raw:
        if not raw.endswith(b"\n"):
            raise ValueError("Manifest has an incomplete tail.")
        rows = [parse_json(line) for line in raw.splitlines() if line.strip()]
        selection.change(
            path,
            jsonl(
                [
                    row
                    for row in rows
                    if row.get("run_id") not in selected
                    and not (whole_project and row.get("project_id") == project_id)
                ]
            ),
        )
