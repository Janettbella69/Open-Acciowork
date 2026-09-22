"""Exact project-owned records and shared-device scope removal."""

import stat

from .companion import PairingRecord
from .conversation_erasure_context import source_diaries_and_manifest
from .erasure_files import encode
from .shop_browser_store import ShopIdentity


def project_related(selection, project, runs):
    selected = {run.id for run in runs}
    counts = {"schedules": 0, "intents": 0, "shops": 0, "devices": 0}
    for directory in (
        "notifications",
        "feedback",
        "grants",
        "channel-deliveries",
        "intents",
        "task-experiences",
        "channels/feishu-bindings",
    ):
        for name, _ in selection.children(directory):
            if name.endswith(".tmp"):
                raise ValueError("Shared records have an interrupted write. Recover it first.")
            if not name.endswith(".json"):
                continue
            path = f"{directory}/{name}"
            value, _ = selection.record(path)
            # Task-experience commits retain both snapshots before publication.
            identity = value.get("state", {}) if name.endswith(".transaction.json") else value
            if not isinstance(identity, dict):
                raise ValueError("Shared project record identity is invalid.")
            by_run = identity.get("run_id") in selected or identity.get("source_run_id") in selected
            if identity.get("project_id") == project.id or by_run:
                if by_run and identity.get("project_id") not in (None, project.id):
                    raise ValueError("Project record has a conflicting conversation source.")
                selection.change(path)
                if directory in counts:
                    counts[directory] += 1
    for directory, record in (("schedules", "schedule.json"), ("skill-proposals", "record.json")):
        for name, info in selection.children(directory):
            if name.startswith("."):
                # Never leave unclassified staged business content behind.
                if name.endswith((".tmp", ".stage")):
                    raise ValueError("Project records have unfinished staging. Recover it first.")
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Project record storage is invalid.")
            value, _ = selection.record(f"{directory}/{name}/{record}")
            if value.get("project_id") == project.id:
                if directory == "skill-proposals" and value.get("state") == "publishing":
                    raise ValueError("Finish Skill publication recovery before project deletion.")
                selection.remove_tree(f"{directory}/{name}")
                selection.removed_directories.add(f"{directory}/{name}")
                if directory in counts:
                    counts[directory] += 1
    for name, _ in selection.children("shop-browsers"):
        if not name.endswith(".json"):
            if name.endswith(".tmp"):
                raise ValueError("Shop storage has an interrupted write. Recover it first.")
            continue
        path = f"shop-browsers/{name}"
        _, raw = selection.record(path)
        shop = ShopIdentity.model_validate_json(raw)
        if shop.project_id == project.id:
            if (
                shop.id + ".json" != name
                or shop.tenant_id != project.tenant_id
                or shop.identity_binding != project.identity_binding
            ):
                raise ValueError("Shop identity does not match the project.")
            selection.change(path)
            selection.remove_entry("shop-browsers", shop.id)
            counts["shops"] += 1
    for name, _ in selection.children("companion-devices"):
        if not name.endswith(".json"):
            if name.endswith(".tmp"):
                raise ValueError("Device storage has an interrupted write. Recover it first.")
            continue
        path = f"companion-devices/{name}"
        value, _ = selection.record(path)
        device = PairingRecord.model_validate(value)
        if project.id not in device.project_ids:
            continue
        if device.id + ".json" != name:
            raise ValueError("Device identity is invalid.")
        remaining = [item for item in device.project_ids if item != project.id]
        if remaining:
            value["project_ids"] = remaining
            selection.change(path, encode(value))
        else:
            selection.change(path)
            selection.change(f"companion-push/{name}")
        counts["devices"] += 1
    for directory in ("receipts", "commercial-actions", "auth/projects"):
        selection.remove_entry(directory, project.id)
    for directory in ("auth/marketplaces", "collaboration/comments"):
        filename = f"{project.id}.json"
        selection.change(f"{directory}/{filename}")
        # Atomic writers can leave an unpublished copy after process death.
        # Its exact target prefix binds even an incomplete JSON file to this project.
        for name, _ in selection.children(directory):
            if name.startswith(f".{filename}.") and name.endswith(".tmp"):
                selection.change(f"{directory}/{name}")
    source_diaries_and_manifest(selection, project.id, runs, whole_project=True)
    return counts
