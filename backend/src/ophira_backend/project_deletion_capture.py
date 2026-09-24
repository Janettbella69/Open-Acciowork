"""Strict metadata-only inventory of application-owned project deletion sources."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .backup_files import BackupLimits, parse_json
from .models import OpportunityProject, RunRecord, Schedule, SkillPublicationReceipt
from .project_deletion_proofs import (
    ProjectDeletionProofs,
    TemplateDeletionProof,
    proof_hash,
    template_deletion_proofs,
)
from .skill_proposal_files import child_directory, directory_fd, read_relative, relative_parts

LIMITS = BackupLimits(file_bytes=256 * 1024 * 1024)
JSON_LIMIT = 16 * 1024 * 1024
ID = r"^[A-Za-z0-9_-]{1,200}$"


class ProjectDeletionConflict(ValueError):
    """Safe, stable failure without private source contents."""


class PurgeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    kind: Literal["file", "symlink"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    domain: str

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        relative_parts(value)
        return value


class DiaryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]+$")
    size_bytes: int = Field(ge=1, le=JSON_LIMIT)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PurgeRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    kind: Literal["manifest", "run-manifest", "diary", "pairing", "push"]
    selectors: list[str]
    selected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    diary_blocks: list[DiaryBlock] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        relative_parts(value)
        return value


class PurgePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    project_id: str = Field(pattern=ID)
    project_revision: int = Field(ge=1)
    run_ids: list[str]
    schedule_ids: list[str]
    intent_ids: list[str] = Field(default_factory=list)
    shop_ids: list[str]
    companion_ids: list[str] = Field(default_factory=list)
    extra_project_ids: list[str]
    trees: list[str]
    entries: list[PurgeEntry]
    directories: list[str]
    rewrites: list[PurgeRewrite]
    proofs: ProjectDeletionProofs
    retained: list[str]
    blockers: list[str] = Field(default_factory=list)

    @property
    def fingerprint(self):
        return proof_hash(self)

    @property
    def total_bytes(self):
        return sum(item.size_bytes for item in self.entries)


class DeletionCapture:
    def __init__(self, root: Path, limits: BackupLimits = LIMITS):
        self.root = root.absolute()
        self.limits = limits
        self.entries: dict[str, PurgeEntry] = {}
        self.directories: set[str] = set()
        self.trees: set[str] = set()
        self.scanned = 0
        self.read_bytes = 0
        self.total_bytes = 0

    def names(self, relative):
        relative_parts(relative)
        try:
            with directory_fd(self.root / relative) as fd:
                result = []
                with os.scandir(fd) as children:
                    for child in children:
                        self.scanned += 1
                        if self.scanned > self.limits.entries:
                            raise ProjectDeletionConflict("Project deletion inventory is too large")
                        result.append(child.name)
        except FileNotFoundError:
            return []
        if len({name.casefold() for name in result}) != len(result):
            raise ProjectDeletionConflict("Project deletion inventory is ambiguous or too large")
        for name in result:
            relative_parts(name)
        return sorted(result)

    def json(self, path, *, optional=False):
        try:
            raw = self.raw(path, JSON_LIMIT)
        except FileNotFoundError:
            if optional:
                return None
            raise ProjectDeletionConflict("A project deletion source is missing") from None
        try:
            return parse_json(raw)
        except (ValueError, RecursionError) as exc:
            raise ProjectDeletionConflict("A project deletion record is invalid") from exc

    def raw(self, path, max_bytes):
        raw = read_relative(self.root, path, min(max_bytes, self.limits.total_bytes * 4))
        self.read_bytes += len(raw)
        if self.read_bytes > self.limits.total_bytes * 4:
            raise ProjectDeletionConflict("Project deletion scan exceeds its byte limit")
        return raw

    def entry(self, path, domain, *, optional=False):
        if path in self.entries:
            return
        parts = relative_parts(path)
        try:
            with directory_fd(self.root) as root_fd, child_directory(root_fd, parts[:-1]) as fd:
                info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raw = os.fsencode(os.readlink(parts[-1], dir_fd=fd))
                    kind, size, digest = "symlink", len(raw), hashlib.sha256(raw).hexdigest()
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    descriptor = os.open(
                        parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                    )
                    try:
                        before = os.fstat(descriptor)
                        if (before.st_dev, before.st_ino) != (
                            info.st_dev,
                            info.st_ino,
                        ) or before.st_nlink != 1:
                            raise ProjectDeletionConflict(
                                "A deletion source changed during capture"
                            )
                        size, checksum = 0, hashlib.sha256()
                        while chunk := os.read(descriptor, 1024 * 1024):
                            size += len(chunk)
                            if (
                                size > self.limits.file_bytes
                                or self.total_bytes + size > self.limits.total_bytes
                            ):
                                raise ProjectDeletionConflict(
                                    "Project deletion exceeds its byte limit"
                                )
                            checksum.update(chunk)
                        after = os.fstat(descriptor)
                        if (
                            (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                            != (
                                after.st_size,
                                after.st_mtime_ns,
                                after.st_ctime_ns,
                            )
                            or size != after.st_size
                            or after.st_nlink != 1
                        ):
                            raise ProjectDeletionConflict(
                                "A deletion source changed during capture"
                            )
                        kind, digest = "file", checksum.hexdigest()
                    finally:
                        os.close(descriptor)
                else:
                    raise ProjectDeletionConflict(
                        "A deletion source is a hard-linked or special file"
                    )
        except FileNotFoundError:
            if optional:
                return
            raise ProjectDeletionConflict("A deletion source is missing") from None
        if len(self.entries) >= self.limits.files:
            raise ProjectDeletionConflict("Project deletion exceeds its file limit")
        if size > self.limits.file_bytes or self.total_bytes + size > self.limits.total_bytes:
            raise ProjectDeletionConflict("Project deletion exceeds its byte limit")
        self.total_bytes += size
        self.entries[path] = PurgeEntry(
            path=path, kind=kind, sha256=digest, size_bytes=size, domain=domain
        )

    def tree(self, path, domain):
        relative_parts(path)
        self.trees.add(path)
        try:
            with directory_fd(self.root / path):
                pass
        except FileNotFoundError:
            return

        def walk(relative, depth):
            if depth > 40:
                raise ProjectDeletionConflict("Project deletion path depth exceeds its limit")
            self.directories.add(relative)
            for name in self.names(relative):
                child = relative + "/" + name
                with directory_fd(self.root / relative) as fd:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    walk(child, depth + 1)
                else:
                    self.entry(child, domain)

        walk(path, 0)

    def records(self, directory, project_id, *, child=None, mixed=False, links=None):
        selected = []
        for name in self.names(directory):
            if name.endswith(".lock") or name == ".lock":
                continue
            source = f"{directory}/{name}" + ("/" + child if child else "")
            if child:
                if name.startswith("."):
                    raise ProjectDeletionConflict(
                        "Unknown staging exists in a shared project store"
                    )
            elif not (name.endswith(".json") or name.endswith(".tmp")):
                if mixed and re.fullmatch(r"shop_[a-f0-9]{32}", name):
                    continue
                raise ProjectDeletionConflict("Unknown record exists in a shared project store")
            value = self.json(source)
            if not isinstance(value, dict):
                raise ProjectDeletionConflict("Invalid shared project record")
            linked = any(
                value.get(key) in identifiers for key, identifiers in (links or {}).items()
            )
            if linked and value.get("project_id") not in {None, project_id}:
                raise ProjectDeletionConflict("A shared record has conflicting project references")
            if value.get("project_id") != project_id and not (
                linked and value.get("project_id") is None
            ):
                continue
            if not child and (name.startswith(".") or name.endswith(".tmp")):
                self.entry(source, directory)
                continue
            identifier = name if child else name.removesuffix(".json")
            if not re.fullmatch(ID, identifier) or value.get("id") != identifier:
                raise ProjectDeletionConflict("Project record identity does not match its path")
            selected.append((identifier, value))
        return selected


def _model(model, value):
    return model.model_validate_json(json.dumps(value))


def _scope(value, project):
    if not isinstance(value, dict):
        raise ProjectDeletionConflict("Invalid project deletion record")
    if value.get("project_id", project.id) != project.id:
        raise ProjectDeletionConflict("A deletion record belongs to another project")
    if "tenant_id" in value and value["tenant_id"] != project.tenant_id:
        raise ProjectDeletionConflict("A deletion record belongs to another tenant")


def capture_project_deletion(root: Path, project_id: str, *, limits=LIMITS) -> PurgePlan:
    """Caller holds lifecycle leases; never invokes repair or reads secret stores."""
    try:
        return _capture_project_deletion(root, project_id, limits=limits)
    except ProjectDeletionConflict:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
        raise ProjectDeletionConflict("Application deletion sources cannot be verified") from exc


def _capture_project_deletion(root: Path, project_id: str, *, limits=LIMITS) -> PurgePlan:
    if not re.fullmatch(ID, project_id):
        raise ProjectDeletionConflict("Invalid project identity")
    capture = DeletionCapture(root, limits)
    project = _model(OpportunityProject, capture.json(f"projects/{project_id}/project.json"))
    if project.id != project_id:
        raise ProjectDeletionConflict("Project identity does not match its path")
    runs = {}
    for identifier, value in capture.records("runs", project_id, child="run.json"):
        _scope(value, project)
        runs[identifier] = _model(RunRecord, value)
        capture.tree(f"runs/{identifier}", "conversations")
    schedules = {}
    experience_names = capture.names("task-experiences")
    for identifier, value in capture.records("schedules", project_id, child="schedule.json"):
        _scope(value, project)
        schedules[identifier] = _model(Schedule, value)
        capture.tree(f"schedules/{identifier}", "automation")
        for suffix in (".json", ".transaction.json"):
            path = f"task-experiences/{identifier}{suffix}"
            stored = capture.json(path, optional=True)
            if stored is not None:
                if not isinstance(stored, dict):
                    raise ProjectDeletionConflict("Invalid task experience source")
                scope = stored.get("state", stored)
                if (
                    not isinstance(scope, dict)
                    or scope.get("project_id") != project_id
                    or scope.get("schedule_id") != identifier
                ):
                    raise ProjectDeletionConflict("Task experience has a different project scope")
                capture.entry(path, "automation")
        for name in experience_names:
            if any(
                name.startswith(f".{identifier}{suffix}.") and name.endswith(".tmp")
                for suffix in (".json", ".transaction.json")
            ):
                capture.entry(f"task-experiences/{name}", "staging")
    for name in experience_names:
        if name.endswith(".lock"):
            continue
        value = capture.json("task-experiences/" + name)
        scope = value.get("state", value) if isinstance(value, dict) else None
        if not isinstance(scope, dict):
            raise ProjectDeletionConflict("Invalid task experience source")
        if scope.get("project_id") == project_id:
            identifier = scope.get("schedule_id")
            if identifier not in schedules:
                raise ProjectDeletionConflict("Task experience has no matching project schedule")
            if f"task-experiences/{name}" not in capture.entries:
                raise ProjectDeletionConflict("Unknown project task experience source")
    notifications, shops, intents = {}, {}, {}
    for directory in (
        "intents",
        "notifications",
        "grants",
        "feedback",
        "channel-deliveries",
        "shop-browsers",
        "channels/feishu-bindings",
    ):
        for identifier, value in capture.records(
            directory,
            project_id,
            mixed=directory == "shop-browsers",
            links={"run_id": set(runs), "schedule_id": set(schedules), "intent_id": set(intents)},
        ):
            if directory != "notifications" or value.get("project_id") is not None:
                _scope(value, project)
            capture.entry(f"{directory}/{identifier}.json", directory)
            if directory == "intents":
                intents[identifier] = value
            if directory == "notifications":
                notifications[identifier] = value
            if directory == "shop-browsers":
                shops[identifier] = value
                capture.tree(f"shop-browsers/{identifier}", "browser")
    for path, domain in (
        (f"projects/{project_id}", "project"),
        (f"receipts/{project_id}", "sources"),
        (f"commercial-actions/{project_id}", "commercial"),
        (f"auth/projects/{project_id}", "access"),
        (f"memory/project/{project_id}", "memory"),
        (f"browser-profiles/{project_id}", "browser"),
        (f"collaboration/project-templates/{project_id}", "templates"),
        (f"secrets/event-triggers/{project_id}", "event-triggers"),
    ):
        capture.tree(path, domain)
    for path in (
        f"auth/marketplaces/{project_id}.json",
        f"collaboration/comments/{project_id}.json",
    ):
        value = capture.json(path, optional=True)
        if value is not None:
            _scope(value, project)
            capture.entry(path, "access")
    # Known project-specific atomic staging beside flat canonical records.
    for directory in ("auth/marketplaces", "collaboration/comments"):
        for name in capture.names(directory):
            if name.startswith(f".{project_id}.json.") and name.endswith(".tmp"):
                capture.entry(f"{directory}/{name}", "staging")
    publications = []
    from .conversation_deletion_state import ConversationDeletionStates
    from .project_lifecycle import ProjectLifecycle

    deleted_runs = ConversationDeletionStates(ProjectLifecycle(root)).origins(project_id)
    canonical = capture.json("skills/registry-state.json", optional=True) or {}
    if not isinstance(canonical, dict) or not isinstance(
        canonical.get("publication_receipts", {}), dict
    ):
        raise ProjectDeletionConflict("Invalid Skill publication registry")
    for identifier, value in capture.records("skill-proposals", project_id, child="record.json"):
        from .skill_proposal_models import SkillProposal
        from .skill_proposals import SkillProposalService

        proposal = _model(SkillProposal, value)
        SkillProposalService.validate_metadata(proposal, identifier)
        if proposal.source_run_id not in runs | deleted_runs or any(
            row.id not in runs | deleted_runs for row in proposal.source_runs
        ):
            raise ProjectDeletionConflict("Skill proposal has foreign source runs")
        raw_receipt = canonical.get("publication_receipts", {}).get(identifier)
        if raw_receipt is not None:
            receipt = _model(SkillPublicationReceipt, raw_receipt)
            if not SkillProposalService._receipt_matches(proposal, receipt):
                raise ProjectDeletionConflict("Skill publication has inconsistent provenance")
            publications.append(receipt)
        elif proposal.state == "published":
            raise ProjectDeletionConflict("Published Skill receipt is missing")
        capture.tree(f"skill-proposals/{identifier}", "skill-proposals")
    links, extra = _capture_templates(capture, project)
    rewrites, pair_ids = _capture_shared(capture, project, runs, notifications)
    retained = [
        "Shared Provider profiles, credentials, connectors and organization policy are retained.",
        "User and Agent scope memories, other projects and their independent copied artifacts "
        "are retained.",
        "External messages, actions, downloaded exports and operator backups require "
        "separate management.",
        "Content-free deletion records and stable coordination locks are retained.",
    ]
    if publications:
        retained.append("Published shared Skills and their publication receipts are retained.")
    if links:
        retained.append(
            "Content-free template provenance is retained; other projects keep their own "
            "origin and artifacts."
        )
    if any(item.kind == "symlink" for item in capture.entries.values()):
        retained.append(
            "Links inside owned trees are removed without following or erasing their "
            "external targets."
        )
    return PurgePlan(
        project_id=project.id,
        project_revision=project.revision,
        run_ids=sorted(runs),
        schedule_ids=sorted(schedules),
        intent_ids=sorted(intents),
        shop_ids=sorted(shops),
        companion_ids=sorted(pair_ids),
        extra_project_ids=sorted(extra),
        trees=sorted(capture.trees),
        entries=sorted(capture.entries.values(), key=lambda row: row.path),
        directories=sorted(capture.directories),
        rewrites=rewrites,
        proofs=ProjectDeletionProofs(
            project_id=project.id, publications=publications, templates=links
        ),
        retained=retained,
    )


def _capture_templates(capture, project):
    from .project_template_apply import TemplateApplications
    from .project_template_models import ProjectTemplate, TemplateApplication

    previous = template_deletion_proofs(capture.root)
    links, extra = {}, set()
    for source_id in capture.names("collaboration/project-templates"):
        if not re.fullmatch(ID, source_id):
            raise ProjectDeletionConflict("Invalid template source identity")
        for template_id in capture.names(f"collaboration/project-templates/{source_id}"):
            family = f"collaboration/project-templates/{source_id}/{template_id}"
            if template_id.startswith(".") and template_id.endswith(".stage"):
                if source_id != project.id:
                    continue
                capture.tree(family, "templates")
                continue
            template = _model(ProjectTemplate, capture.json(family + "/template.json"))
            if template.project_id != source_id or template.id != template_id:
                raise ProjectDeletionConflict("Template source identity mismatch")
            for name in capture.names(family + "/applications"):
                if name.startswith(".") or name.endswith(".tmp"):
                    continue  # Owned-source tree captures all staging; target staging below.
                record = _model(TemplateApplication, capture.json(family + "/applications/" + name))
                if (
                    record.source_project_id != source_id
                    or record.template_id != template_id
                    or name != record.id + ".json"
                ):
                    raise ProjectDeletionConflict("Template application source mismatch")
                if project.id not in {source_id, record.project_id}:
                    continue
                source_project = _model(
                    OpportunityProject,
                    capture.json(f"projects/{source_id}/project.json"),
                )
                if (
                    source_project.id != source_id
                    or template.identity_binding != source_project.identity_binding
                    or template.tenant_id != source_project.tenant_id
                ):
                    raise ProjectDeletionConflict("Template source has a different tenant")
                target_value = capture.json(
                    f"projects/{record.project_id}/project.json", optional=True
                )
                target = _model(OpportunityProject, target_value) if target_value else None
                version = template.versions[record.template_version - 1]
                if version.content_hash != record.template_hash:
                    raise ProjectDeletionConflict("Template version does not match its application")
                if target:
                    TemplateApplications.validate_origin(record, version, target)
                    if (
                        target.identity_binding != template.identity_binding
                        or target.tenant_id != template.tenant_id
                        or target.owner_principal_id
                        != (
                            record.actor_id
                            if template.identity_binding == "authenticated_principal"
                            else None
                        )
                    ):
                        raise ProjectDeletionConflict("Template target has a different tenant")
                elif record.state == "applied":
                    raise ProjectDeletionConflict("Completed template target is missing")
                link = TemplateDeletionProof(
                    application_id=record.id,
                    source_project_id=source_id,
                    target_project_id=record.project_id,
                    template_id=template_id,
                    template_version=record.template_version,
                    template_hash=record.template_hash,
                    origin_hash=proof_hash(target.template_origin) if target else None,
                    identity_binding=template.identity_binding,
                    tenant_id=template.tenant_id,
                    target_owner_principal_id=target.owner_principal_id if target else None,
                )
                links[record.id] = link
                extra.update({source_id, record.project_id} - {project.id})
                if source_id != project.id:
                    capture.entry(family + "/applications/" + name, "template-applications")
                    capture.tree(
                        family + f"/applications/.{record.id}.stage", "template-applications"
                    )
                    for temporary in capture.names(family + "/applications"):
                        if temporary.startswith(f".{record.id}.json.") and temporary.endswith(
                            ".tmp"
                        ):
                            capture.entry(
                                family + "/applications/" + temporary, "template-applications"
                            )
    for identifier, link in previous.items():
        if project.id in {link.source_project_id, link.target_project_id}:
            links.setdefault(identifier, link)
    if project.template_origin and project.template_origin.application_id not in links:
        raise ProjectDeletionConflict("Project template origin has no verifiable application")
    if project.template_origin:
        link = links[project.template_origin.application_id]
        if link.target_project_id != project.id or link.origin_hash != proof_hash(
            project.template_origin
        ):
            raise ProjectDeletionConflict("Project template origin differs from its deletion proof")
    return list(links.values()), extra


def diary_selection(raw: bytes, run_ids: set[str], *, runs=None, blocks=None):
    """Remove exact generated entries, with lengths/hashes rather than Markdown boundaries."""
    text = raw.decode("utf-8")
    pattern = re.compile(r"(?m)^## \d{2}:\d{2}:\d{2} · (run_[A-Za-z0-9_-]+)\r?$")
    removed, kept, metadata, cursor = [], [], [], 0
    for match in pattern.finditer(text):
        if match.start() < cursor or match.group(1) not in run_ids:
            continue
        identifier = match.group(1)
        suffix = text[match.start() :]
        if runs is not None:
            run = runs[identifier]
            lines = [
                match.group(0),
                f"- 目标：{' '.join(run.prompt.split())[:200]}",
                f"- 状态：{run.status.value}",
            ]
            if run.schedule_id:
                lines.append(f"- 来源：定时任务 {run.schedule_id}")
            if run.standing_intent_id:
                lines.append(f"- 来源：前瞻意图 {run.standing_intent_id}")
            notes = [run.error[:300], " ".join(run.error.split())[:300]] if run.error else [""]
            variants = [
                "\n".join(lines + ([f"- 备注：{note}"] if note else [])) + "\n\n" for note in notes
            ]
            block = next((value for value in variants if suffix.startswith(value)), None)
        else:
            block = None
            for descriptor in blocks or []:
                if descriptor.run_id != identifier:
                    continue
                candidate = suffix.encode()[: descriptor.size_bytes]
                if hashlib.sha256(candidate).hexdigest() == descriptor.sha256:
                    block = candidate.decode("utf-8")
                    break
        if block is None:
            raise ProjectDeletionConflict("Project diary entry cannot be verified")
        kept.append(text[cursor : match.start()])
        removed.append(block)
        metadata.append(
            {
                "run_id": identifier,
                "size_bytes": len(block.encode()),
                "sha256": hashlib.sha256(block.encode()).hexdigest(),
            }
        )
        cursor = match.start() + len(block)
    kept.append(text[cursor:])
    return removed, "".join(kept).encode(), metadata


def _capture_shared(capture, project, runs, notifications, *, conversation=False):
    rewrites = []
    manifest = (
        capture.raw("_manifest.jsonl", min(capture.limits.file_bytes, 64 * 1024 * 1024))
        if (capture.root / "_manifest.jsonl").exists()
        or (capture.root / "_manifest.jsonl").is_symlink()
        else b""
    )
    rows = [parse_json(line) for line in manifest.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ProjectDeletionConflict("Invalid project manifest")
    selected = [row for row in rows if (
        row.get("run_id") in runs if conversation else row.get("project_id") == project.id
    )]
    if selected:
        rewrites.append(
            PurgeRewrite(
                path="_manifest.jsonl",
                kind="run-manifest" if conversation else "manifest",
                selectors=sorted(runs) if conversation else [project.id],
                selected_hash=proof_hash(selected),
            )
        )
    diary_scopes = {}
    for agent_id in sorted({row.agent_id for row in runs.values() if row.agent_id}):
        if not re.fullmatch(ID, agent_id):
            raise ProjectDeletionConflict("Invalid project Agent identity")
        diary_scopes[f"agents/{agent_id}/diary"] = {
            run.id for run in runs.values() if run.agent_id == agent_id
        }
    if conversation:
        diary_scopes[f"projects/{project.id}/diary"] = {
            run.id for run in runs.values() if not run.agent_id
        }
    for directory, run_ids in sorted(diary_scopes.items()):
        for name in capture.names(directory):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", name):
                raise ProjectDeletionConflict("Unknown shared Agent diary file")
            path = f"{directory}/{name}"
            selected, _retained, blocks = diary_selection(
                capture.raw(path, JSON_LIMIT),
                run_ids,
                runs=runs,
            )
            if selected:
                rewrites.append(
                    PurgeRewrite(
                        path=path,
                        kind="diary",
                        selectors=sorted(run_ids),
                        selected_hash=proof_hash(selected),
                        diary_blocks=blocks,
                    )
                )
    anchors = {
        hashlib.sha256(f"notification:{identifier}".encode()).hexdigest()
        for identifier in notifications
    }
    anchors.update(
        hashlib.sha256(f"permission:{wait.id}".encode()).hexdigest()
        for run in runs.values()
        for wait in run.permission_waits
    )
    pair_ids = set()
    for name in capture.names("companion-devices"):
        if name == ".transaction.lock":
            continue
        value = capture.json("companion-devices/" + name)
        if not isinstance(value, dict) or not isinstance(value.get("project_ids"), list):
            raise ProjectDeletionConflict("Invalid Companion pairing record")
        if project.id not in value["project_ids"]:
            continue
        from .companion import PairingRecord

        pairing = _model(PairingRecord, value)
        pair_ids.add(pairing.id)
        if name != value.get("id", "") + ".json":
            if not name.startswith(".pair-"):
                raise ProjectDeletionConflict("Unknown Companion pairing staging record")
            # Interrupted shared staging cannot retain a deleted project's access copy.
            if not conversation:
                capture.entry("companion-devices/" + name, "companion")
            continue
        if not conversation:
            rewrites.append(
                PurgeRewrite(
                    path="companion-devices/" + name,
                    kind="pairing",
                    selectors=[project.id],
                    selected_hash=proof_hash([project.id]),
                )
            )
        push = capture.json("companion-push/" + name, optional=True)
        if push:
            from .push_store import PushRecord

            if _model(PushRecord, push).id != pairing.id:
                raise ProjectDeletionConflict("Companion push identity mismatch")
            rewrites.append(
                PurgeRewrite(
                    path="companion-push/" + name,
                    kind="push",
                    selectors=sorted(anchors),
                    selected_hash=proof_hash(sorted(anchors)),
                )
            )
    if not conversation:
        capture_companion_staging(capture, project.id, pair_ids)
    return rewrites, pair_ids


def capture_companion_staging(capture, project_id, pair_ids):
    """Only private, uncommitted copies with a validated owner may be erased."""
    from .companion import PairingRecord
    from .push_store import PushRecord

    for name in capture.names("companion-devices"):
        if name.startswith(".pair-"):
            record = _model(PairingRecord, capture.json("companion-devices/" + name))
            if project_id in record.project_ids:
                capture.entry("companion-devices/" + name, "companion")
    for name in capture.names("companion-push"):
        if name.endswith(".lock") or re.fullmatch(r"pair_[a-f0-9]{32}\.json", name):
            continue
        if not name.startswith(".push-"):
            raise ProjectDeletionConflict("Unknown Companion push staging record")
        record = _model(PushRecord, capture.json("companion-push/" + name))
        if record.id in pair_ids:
            capture.entry("companion-push/" + name, "companion-push")
