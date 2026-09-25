"""Authorized, bounded project archives from an explicit durable-source allowlist."""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import zipfile
from contextlib import nullcontext

from .artifact_edits import PENDING_EDIT
from .artifact_files import snapshot_relative
from .auth import ProjectAccessRecord, ProjectPermission
from .channel_delivery import ChannelDeliveryRecord
from .constants import TERMINAL_RUN_STATUSES, MemoryScope
from .conversation_deletion_state import (
    ConversationDeletionStates,
    run_excerpt_hash,
    run_identity_hash,
)
from .gmail_access import GmailAccessRecord
from .marketplace_access import MarketplaceAccessRecord
from .models import (
    CommercialActionRecord,
    EventEnvelope,
    FeedbackItem,
    MemoryItem,
    MemoryTombstone,
    Notification,
    OpportunityProject,
    PermissionGrant,
    RunRecord,
    RunSummary,
    Schedule,
    StandingIntent,
    TaskExperienceLedger,
    ToolReceipt,
    utc_now,
)
from .outlook_access import OutlookAccessRecord
from .project_comments import CommentLedger
from .project_deletion_proofs import proof_hash
from .project_export_capture import ExportCapture, json_bytes
from .project_export_models import (
    EXPORT_FORMAT,
    ProjectExportConflict,
    ProjectExportPreview,
)
from .project_scope import authorize_project_scope, project_actor
from .project_template_models import ProjectTemplate
from .shop_browser_store import ShopIdentity
from .skill_proposal_files import file_inventory
from .skill_proposal_models import SkillProposal, SkillProposalManifest
from .skill_proposals import SkillProposalService
from .task_experience import TaskExperienceStore
from .workspace_checkpoints import PENDING_RESTORE, workspace_lease


def _same_scope(value, project, *, identity=True):
    if value.get("project_id") != project.id:
        raise ValueError("Export record belongs to a different project")
    if "tenant_id" in value and value["tenant_id"] != project.tenant_id:
        raise ValueError("Export record belongs to a different tenant")
    if identity and "identity_binding" in value and (
        value["identity_binding"] != project.identity_binding
    ):
        raise ValueError("Export record has a different identity scope")
    binding = value.get("principal_binding")
    if binding is not None and binding.get("tenant_id") != project.tenant_id:
        raise ValueError("Export automation belongs to a different tenant")


def _names(capture, source):
    # Uncommitted atomic-write staging is not a durable project record. Keep the
    # full directory listing in capture.observed so its changes still invalidate.
    return [name for name in capture.names(source) if not name.startswith(".")]


class ProjectExports:
    def __init__(self, ledger, access, settings):
        self.ledger, self.access, self.settings = ledger, access, settings

    def authorize(self, project_id, principal):
        return authorize_project_scope(
            self.ledger, self.access, self.settings, project_id, principal,
            ProjectPermission.PROJECT_EXPORT,
        )

    def preview(self, project_id, principal) -> ProjectExportPreview:
        preview, _capture = self._capture(project_id, principal)
        return preview

    def download(self, project_id, principal, fingerprint: str) -> bytes:
        preview, capture = self._capture(project_id, principal)
        if not hmac.compare_digest(preview.fingerprint, fingerprint):
            raise ProjectExportConflict("Project data changed. Review a fresh export.")
        manifest = {
            **preview.model_dump(mode="json"), "exported_at": utc_now().isoformat(),
            "inventory_scope": "Payload files; this manifest is not counted in its own inventory.",
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            output.writestr("manifest.json", json_bytes(manifest))
            for directory in sorted(capture.directories):
                output.writestr(directory + "/", b"")
            for name, content in sorted(capture.files.items()):
                output.writestr(name, content)
        self.authorize(project_id, principal)
        return archive.getvalue()

    def _capture(self, project_id, principal):
        self.authorize(project_id, principal)
        authority = self.access._transaction(project_id) if principal else nullcontext()
        with authority, workspace_lease(self.ledger, project_id), self.ledger._lock_for(project_id):
            project = self.authorize(project_id, principal)
            capture = ExportCapture(self.ledger.root)
            source = f"projects/{project.id}"
            for pending in (PENDING_EDIT, PENDING_RESTORE):
                if capture.read(f"{source}/{pending}", optional=True) is not None:
                    raise ProjectExportConflict(
                        "Finish the pending project recovery before export.",
                    )
            raw_project = capture.json(source + "/project.json")
            saved = OpportunityProject.model_validate_json(json_bytes(raw_project))
            if saved != project:
                raise ProjectExportConflict("Project data changed. Review a fresh export.")
            capture.add_json("project.json", raw_project, "project")
            runs = self._runs(capture, project)
            deletion_root = f"conversation-deletions/{project.id}"
            for name in _names(capture, deletion_root):
                # Observe only; never export deletion requester data.
                capture.json(f"{deletion_root}/{name}")
            origins = ConversationDeletionStates(self.ledger.lifecycle).origins(project.id)
            if set(origins) & set(runs):
                raise ValueError("Deleted conversation Run has reappeared")
            if origins:
                capture.add_json("deleted-conversations/run-origins.json", [
                    origin.model_dump(mode="json") for origin in origins.values()
                ], "conversations")
                runs.update(origins)
            capture.tree(source + "/workspace", "workspace", "workspace")
            for artifact in project.artifacts:
                if artifact.project_id != project.id:
                    raise ValueError("Artifact belongs to a different project")
                if artifact.run_id is not None and artifact.run_id not in runs:
                    raise ValueError("Artifact references an unavailable project Run")
                location = (
                    snapshot_relative(artifact.checksum)
                    if artifact.content_storage == "snapshot" else "workspace/" + artifact.path
                )
                self._bytes(
                    capture, source + "/" + location,
                    "artifact-versions/" + artifact.checksum, "artifacts",
                    artifact.checksum, artifact.size_bytes,
                )
            receipts = self._receipts(capture, project, runs)
            self._memory(capture, project)
            self._project_records(capture, project)
            self._automation(capture, project, runs, receipts)
            self._templates(capture, project)
            self._proposals(capture, project, runs)
            # Every captured source and enumerated membership must still match.
            # This also catches out-of-process writes in adjacent record stores.
            capture.verify()
            self.authorize(project_id, principal)
            entries = sorted(capture.entries, key=lambda item: item.path)
            identity = {
                "project_id": project.id, "actor_id": project_actor(principal),
                "auth_mode": self.settings.auth_mode,
                "deployment_tenant": self.settings.trusted_gateway_tenant_id,
                "principal": None if principal is None else {
                    "tenant_id": principal.tenant_id, "principal_id": principal.principal_id,
                    "role": principal.role.value, "session_id": principal.session_id,
                },
            }
            digest = hashlib.sha256(json_bytes({
                "format": EXPORT_FORMAT, "identity": identity,
                "sources": capture.observed, "directories": sorted(capture.directories),
                "entries": [item.model_dump() for item in entries],
            })).hexdigest()
            preview = ProjectExportPreview(
                project_id=project.id, actor_id=project_actor(principal), revision=project.revision,
                fingerprint=digest, file_count=len(entries), total_bytes=capture.total_bytes,
                directories=sorted(capture.directories), entries=entries,
            )
            return preview, capture

    @staticmethod
    def _bytes(capture, source, target, category, checksum, size):
        if target in capture.files:
            raw = capture.files[target]
        else:
            raw = capture.read(source, limit=capture.limits.file_bytes)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != checksum:
            raise ProjectExportConflict("A saved project file failed its integrity check.")
        if target not in capture.files:
            capture.add(target, raw, category)

    def _runs(self, capture, project):
        runs = {}
        for identifier, value in capture.select_json("runs", project.id, child_file="run.json"):
            self.ledger._safe_id(identifier)
            source = f"runs/{identifier}"
            run = RunRecord.model_validate_json(json_bytes(value))
            _same_scope(value, project)
            if run.id != identifier:
                raise ValueError("Run path does not match its identity")
            if run.status not in TERMINAL_RUN_STATUSES:
                raise ProjectExportConflict("Wait for this project's runs to finish before export.")
            runs[run.id] = run
            capture.add_json(source + "/run.json", value, "conversations")
            summary = capture.json(source + "/summary.json", optional=True)
            if summary is not None:
                record = RunSummary.model_validate_json(json_bytes(summary))
                if record.run_id != run.id or record.project_id != project.id:
                    raise ValueError("Run summary has a different scope")
                capture.add_json(source + "/summary.json", summary, "conversations")
            events = capture.lines(source + "/events.jsonl")
            if summary is not None and record.last_sequence > len(events):
                raise ValueError("Run conversation history is incomplete")
            for sequence, row in enumerate(events, start=1):
                event = EventEnvelope.model_validate_json(json_bytes(row))
                if (event.run_id != run.id or event.project_id != project.id
                        or event.sequence != sequence or event.id != f"{run.id}:{sequence}"
                        or event.trace_id != run.trace_id):
                    raise ValueError("Run event has a different scope")
            capture.add_lines(source + "/events.jsonl", events, "conversations")
            context = capture.json(source + "/task-experience-context.json", optional=True)
            if context is not None:
                TaskExperienceStore._check_snapshot(context, run)
                capture.add_json(source + "/task-experience-context.json", context, "automation")
        return runs

    def _receipts(self, capture, project, runs):
        root = f"receipts/{project.id}"
        receipts = {}
        for identifier in _names(capture, root):
            self.ledger._safe_id(identifier)
            source = root + "/" + identifier
            value = capture.json(source + "/receipt.json")
            receipt = ToolReceipt.model_validate_json(json_bytes(value))
            _same_scope(value, project, identity=False)
            run = runs.get(receipt.run_id)
            if (receipt.id != identifier or run is None
                    or receipt.session_id != run.sdk_session_id
                    or run_identity_hash(run) != proof_hash([
                        receipt.tenant_id, receipt.principal_id, receipt.principal_role,
                        receipt.principal_session_id,
                    ])
                    or receipt.sanitized_payload_ref != source + "/payload.json"):
                raise ValueError("Source receipt has a different project or Run")
            payload = capture.json(source + "/payload.json")
            actual = hashlib.sha256(self.ledger._canonical_payload_bytes(payload)).hexdigest()
            if actual != receipt.content_hash or (
                receipt.object_version.startswith("sha256:")
                and receipt.object_version != "sha256:" + actual
            ):
                raise ValueError("Source receipt payload is inconsistent")
            target = f"receipts/{identifier}"
            capture.add_json(target + "/receipt.json", value, "sources")
            capture.add_json(target + "/payload.json", payload, "sources")
            receipts[receipt.id] = receipt
        return receipts

    @staticmethod
    def _memory(capture, project):
        source = f"memory/project/{project.id}"
        tombstones = capture.lines(source + "/tombstones.jsonl")
        deleted = set()
        for raw in tombstones:
            tombstone = MemoryTombstone.model_validate_json(json_bytes(raw))
            if (tombstone.scope != MemoryScope.PROJECT or tombstone.scope_id != project.id
                    or tombstone.tenant_id != project.tenant_id):
                raise ValueError("Deleted memory belongs to a different project")
            deleted.add(tombstone.memory_id)

        def retained(raw):
            item = MemoryItem.model_validate_json(json_bytes(raw))
            if (item.scope != MemoryScope.PROJECT or item.scope_id != project.id
                    or item.tenant_id != project.tenant_id
                    or item.identity_binding != project.identity_binding):
                raise ValueError("Memory belongs to a different project")
            return item.id not in deleted

        items = capture.json(source + "/items.json", optional=True)
        if items is not None:
            if not isinstance(items, list):
                raise ValueError("Invalid project memory")
            capture.add_json("memory/items.json", [row for row in items if retained(row)], "memory")
        history = capture.lines(source + "/history.jsonl")
        if history:
            if any(not isinstance(row, dict) or not isinstance(row.get("item"), dict)
                   for row in history):
                raise ValueError("Invalid project memory history")
            capture.add_lines(
                "memory/history.jsonl", [row for row in history if retained(row["item"])], "memory",
            )

    @staticmethod
    def _project_records(capture, project):
        for source, target, category, model in (
            (f"auth/projects/{project.id}/access.json", "access/members.json", "access",
             ProjectAccessRecord),
            (f"auth/marketplaces/{project.id}.json", "access/marketplaces.json", "access",
             MarketplaceAccessRecord),
            (f"auth/gmail/{project.id}.json", "access/gmail.json", "access",
             GmailAccessRecord),
            (f"auth/outlook/{project.id}.json", "access/outlook.json", "access",
             OutlookAccessRecord),
            (f"collaboration/comments/{project.id}.json", "comments.json", "collaboration",
             CommentLedger),
        ):
            value = capture.json(source, optional=True)
            if value is not None:
                model.model_validate_json(json_bytes(value))
                _same_scope(value, project)
                capture.add_json(target, value, category)

    def _automation(self, capture, project, runs, receipts):
        for identifier, value in capture.select_json(
            "schedules", project.id, child_file="schedule.json",
        ):
            self.ledger._safe_id(identifier)
            source = f"schedules/{identifier}/schedule.json"
            schedule = Schedule.model_validate_json(json_bytes(value))
            _same_scope(value, project)
            if schedule.id != identifier:
                raise ValueError("Schedule identity mismatch")
            capture.add_json(source, value, "automation")
            pending = capture.read(f"task-experiences/{identifier}.transaction.json", optional=True)
            if pending is not None:
                raise ProjectExportConflict(
                    "Finish pending task experience recovery before export.",
                )
            experience_path = f"task-experiences/{identifier}.json"
            experience = capture.json(experience_path, optional=True)
            if experience is not None:
                record = TaskExperienceLedger.model_validate_json(json_bytes(experience))
                _same_scope(experience, project)
                if record.schedule_id != schedule.id:
                    raise ValueError("Task experience belongs to a different schedule")
                capture.add_json(experience_path, experience, "automation")
        for root, model, category in (
            ("intents", StandingIntent, "automation"),
            ("notifications", Notification, "notifications"),
            ("grants", PermissionGrant, "access"),
            ("feedback", FeedbackItem, "feedback"),
            ("channel-deliveries", ChannelDeliveryRecord, "conversations"),
            ("shop-browsers", ShopIdentity, "access"),
            (f"commercial-actions/{project.id}/actions", CommercialActionRecord, "commercial"),
        ):
            if root.startswith("commercial-actions/"):
                selected = [(name, capture.json(root + "/" + name))
                            for name in _names(capture, root)]
            else:
                selected = capture.select_json(
                    root, project.id, skip_non_json=root == "shop-browsers",
                )
            for filename, value in selected:
                source = root + "/" + filename
                # Strict models (notably ShopIdentity) intentionally accept ISO
                # dates only through Pydantic's JSON validation boundary.
                record = model.model_validate_json(json_bytes(value))
                _same_scope(value, project)
                if record.id + ".json" != filename:
                    raise ValueError("Project record path identity mismatch")
                if value.get("run_id") is not None and value["run_id"] not in runs:
                    raise ValueError("Project record refers to a different Run")
                if isinstance(record, CommercialActionRecord):
                    run = runs.get(record.source_run_id)
                    source_actor = (record.staged_by
                                    if project.identity_binding == "authenticated_principal"
                                    else None)
                    if (run is None or record.source_session_id != run.sdk_session_id
                            or run_identity_hash(run) != proof_hash([
                                record.tenant_id, source_actor,
                                record.staged_role, record.staged_principal_session_id,
                            ])):
                        raise ValueError("Commercial action source belongs to a different Run")
                    if record.source_receipt_id is not None:
                        receipt = receipts.get(record.source_receipt_id)
                        if receipt is None or receipt.run_id != record.source_run_id:
                            raise ValueError("Commercial action source receipt is inconsistent")
                target = ("commercial-actions/" + filename if root.startswith("commercial-actions/")
                          else source)
                capture.add_json(target, value, category)

    def _templates(self, capture, project):
        root = f"collaboration/project-templates/{project.id}"
        for identifier in _names(capture, root):
            self.ledger._safe_id(identifier)
            source = root + "/" + identifier
            value = capture.json(source + "/template.json")
            template = ProjectTemplate.model_validate_json(json_bytes(value))
            _same_scope(value, project)
            if template.id != identifier:
                raise ValueError("Template path identity mismatch")
            target = "templates/" + identifier
            capture.add_json(target + "/template.json", value, "templates")
            for version in template.versions:
                for reference in version.references:
                    path = snapshot_relative(reference.checksum)
                    self._bytes(capture, source + "/" + path, target + "/" + path,
                                "templates", reference.checksum, reference.size_bytes)

    def _proposals(self, capture, project, runs):
        artifacts = {item.id: item for item in project.artifacts}
        for identifier, value in capture.select_json(
            "skill-proposals", project.id, child_file="record.json",
        ):
            if not re.fullmatch(r"skill_proposal_[0-9a-f]{64}", identifier):
                raise ValueError("Invalid proposal identity")
            source = "skill-proposals/" + identifier
            proposal = SkillProposal.model_validate_json(json_bytes(value))
            SkillProposalService.validate_metadata(proposal, identifier)
            run = runs.get(proposal.source_run_id)
            artifact = artifacts.get(proposal.source_artifact_id)
            if (proposal.id != identifier or run is None or artifact is None
                    or artifact.run_id != run.id or artifact.checksum != proposal.manifest_checksum
                    or run.started_at is None
                    or proposal.source_sdk_session_id != run.sdk_session_id):
                raise ValueError("Skill proposal has inconsistent project provenance")
            if [item.id for item in proposal.source_runs] != proposal.manifest.related_run_ids:
                raise ValueError("Skill proposal related Runs are inconsistent")
            for source_run in proposal.source_runs:
                related = runs.get(source_run.id)
                if (related is None or related.id == run.id or related.started_at is None
                        or source_run.started_at != related.started_at
                        or source_run.finished_at != related.finished_at
                        or source_run.status != related.status
                        or proof_hash(source_run.prompt_excerpt) != run_excerpt_hash(related)):
                    raise ValueError("Skill proposal includes another project's Run history")
            capture.add_json(source + "/record.json", value, "skill-proposals")
            manifest = capture.read(source + "/manifest.json")
            if (hashlib.sha256(manifest).hexdigest() != proposal.manifest_checksum
                    or SkillProposalManifest.model_validate_json(manifest) != proposal.manifest):
                raise ValueError("Skill proposal manifest is inconsistent")
            capture.add(source + "/manifest.json", manifest, "skill-proposals")
            # Only frozen, recorded files belong to the package. Additional files
            # are detected by the inventory and cause a conflict instead of export.
            capture.tree(source + "/package", source + "/package", "skill-proposals")
            package = {path.removeprefix(source + "/package/"): raw
                       for path, raw in capture.files.items()
                       if path.startswith(source + "/package/")}
            if file_inventory(package) != proposal.files:
                raise ValueError("Skill proposal file inventory is inconsistent")
            # Same content framing as SkillRegistry._hash_directory, calculated
            # from the bounded descriptor-captured bytes rather than reopening paths.
            digest = hashlib.sha256()
            for name, raw in sorted(package.items()):
                digest.update(name.encode())
                digest.update(b"\0")
                digest.update(raw)
                digest.update(b"\0")
            if (digest.hexdigest() != proposal.content_hash
                    or proposal.scan_content_hash != proposal.content_hash):
                raise ValueError("Skill proposal package or scan is inconsistent")
