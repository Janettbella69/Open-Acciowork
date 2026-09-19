"""Frozen, unpublished Skill candidates with human-bound publication receipts.

The candidate store is independent of SkillRegistry. Only an explicit artifact
submission creates a candidate; recovery never retries a publication decision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from .auth import AuthenticationError, AuthorizationError, ProjectAccessStore, ProjectPermission
from .constants import TERMINAL_RUN_STATUSES, PrincipalRole
from .events import redact
from .models import (
    PrincipalContext,
    RunRecord,
    SkillCheck,
    SkillPublicationReceipt,
    SkillTestResult,
    utc_now,
)
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .skill_proposal_files import (
    MAX_MANIFEST_BYTES,
    capture_submission,
    capture_tree,
    directory_fd,
    file_inventory,
    fsync_directory,
    read_relative,
    relative_parts,
    write_frozen,
)
from .skill_proposal_models import (
    SkillProposal,
    SkillProposalFile,
    SkillProposalManifest,
    SkillProposalRecoveryReport,
    SkillProposalReview,
    SkillProposalReviewer,
    SkillProposalReviewRequest,
    SkillProposalSourceRun,
)
from .skills import SkillRegistry
from .storage import FileLedger

logger = logging.getLogger(__name__)
MAX_SKILL_PROPOSALS = 128
MAX_SKILL_PROPOSAL_STORE_BYTES = 128_000_000
MAX_SKILL_PROPOSAL_RECORD_BYTES = 512_000
MAX_SKILL_PROPOSAL_REVIEWS = 64
_PROPOSAL_ID = re.compile(r"^skill_proposal_[0-9a-f]{64}$")


class SkillProposalNotFoundError(KeyError):
    pass


class SkillProposalConflictError(ValueError):
    pass


class SkillProposalIntegrityError(RuntimeError):
    pass


class SkillProposalQuotaError(ValueError):
    pass


def _proposal_id(project_id: str, run_id: str, artifact_id: str) -> str:
    key = json.dumps([project_id, run_id, artifact_id], separators=(",", ":")).encode()
    return "skill_proposal_" + hashlib.sha256(key).hexdigest()


def _review_binding(proposal_id: str, review: SkillProposalReview) -> str:
    data = review.model_dump(mode="json", exclude={"outcome", "reviewer_binding"})
    data["proposal_id"] = proposal_id
    return hashlib.sha256(
        json.dumps(
            data,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _remove_staging(path: Path) -> None:
    # Only host-created staging trees are removed; never traverse a symlink.
    for directory, children, _files in os.walk(path, followlinks=False):
        Path(directory).chmod(0o700)
        for child in children:
            candidate = Path(directory) / child
            if not candidate.is_symlink():
                candidate.chmod(0o700)
    shutil.rmtree(path)


class SkillProposalService:
    def __init__(
        self,
        ledger: FileLedger,
        registry: SkillRegistry,
        *,
        access: ProjectAccessStore | None = None,
        auth_mode: Literal["local_only", "trusted_gateway"] = "local_only",
    ) -> None:
        if auth_mode not in {"local_only", "trusted_gateway"}:
            raise ValueError("invalid Skill proposal authentication mode")
        self.ledger = ledger
        self.registry = registry
        self.access = access
        self.auth_mode = auth_mode
        self.root = ledger.root / "skill-proposals"
        self.root.mkdir(mode=0o700, exist_ok=True)
        with directory_fd(self.root):
            pass

    def _path(self, proposal_id: str) -> Path:
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id):
            raise SkillProposalNotFoundError("Skill proposal not found")
        return self.root / proposal_id

    def _load(self, proposal_id: str) -> SkillProposal:
        path = self._path(proposal_id)
        try:
            raw = read_relative(path, "record.json", MAX_SKILL_PROPOSAL_RECORD_BYTES)
        except FileNotFoundError as exc:
            raise SkillProposalNotFoundError("Skill proposal not found") from exc
        except (OSError, ValueError) as exc:
            raise SkillProposalIntegrityError("Skill proposal metadata is unreadable") from exc
        try:
            item = SkillProposal.model_validate_json(raw)
            return self.validate_metadata(item, proposal_id)
        except ValueError as exc:
            raise SkillProposalIntegrityError("Skill proposal metadata is invalid") from exc

    @staticmethod
    def validate_metadata(item: SkillProposal, proposal_id: str) -> SkillProposal:
        """Validate captured metadata without reopening sources or a shared registry."""
        if (
            item.id != proposal_id
            or item.id != _proposal_id(item.project_id, item.source_run_id, item.source_artifact_id)
            or item.scan_content_hash != item.content_hash
            or item.scan.skill_id != item.skill_id
        ):
            raise ValueError("candidate metadata identity mismatch")
        if (
            any(
                _review_binding(item.id, review) != review.reviewer_binding
                for review in item.history
            )
            or (item.history[-1] if item.history else None) != item.review
        ):
            raise ValueError("candidate human review binding mismatch")
        if item.state == "publishing" and (
            item.review is None
            or item.review.outcome != "publishing"
            or item.review.review_revision != item.revision
        ):
            raise ValueError("candidate publication intent is incoherent")
        if item.state == "published" and (
            item.publication_receipt is None
            or not SkillProposalService._receipt_matches(item, item.publication_receipt)
            or item.review is None
            or item.review.outcome != "published"
        ):
            raise ValueError("candidate publication receipt is incoherent")
        for row in item.files:
            relative_parts(row.path)
        if len({row.path.casefold() for row in item.files}) != len(item.files):
            raise ValueError("candidate file paths are ambiguous")
        return item

    def _save(self, item: SkillProposal, *, directory: Path | None = None) -> None:
        item = SkillProposal.model_validate(item.model_dump())
        if len((self.ledger._dump(item) + "\n").encode()) > MAX_SKILL_PROPOSAL_RECORD_BYTES:
            raise SkillProposalQuotaError("Skill proposal review metadata exceeds its limit")
        target = directory or self._path(item.id)
        with self.ledger.lifecycle.lease(item.project_id), directory_fd(target):
            self.ledger._atomic_write(target / "record.json", item)

    def _ids(
        self,
        *,
        clean_staging: bool = True,
        skip_invalid: bool = False,
    ) -> list[str]:
        identifiers = []
        with directory_fd(self.root) as fd, os.scandir(fd) as children:
            for child in children:
                if child.name.startswith(".staging-"):
                    # An interrupted pre-publication snapshot was never accepted.
                    if not clean_staging:
                        continue
                    if not child.is_dir(follow_symlinks=False):
                        if skip_invalid:
                            logger.warning("Skipped invalid Skill proposal staging entry")
                            continue
                        raise SkillProposalIntegrityError("Invalid Skill proposal staging entry")
                    with directory_fd(self.root / child.name):
                        _remove_staging(self.root / child.name)
                    continue
                if not _PROPOSAL_ID.fullmatch(child.name) or not child.is_dir(
                    follow_symlinks=False,
                ):
                    if skip_invalid:
                        logger.warning("Skipped invalid Skill proposal store entry")
                        continue
                    raise SkillProposalIntegrityError(
                        "Skill proposal store contains invalid entries"
                    )
                identifiers.append(child.name)
                if len(identifiers) > MAX_SKILL_PROPOSALS:
                    raise SkillProposalQuotaError("Skill proposal count exceeds its limit")
        return sorted(identifiers)

    def _source_run(self, run_id: str) -> RunRecord:
        run = self.ledger.get_run(run_id)
        project = self.ledger.get_project(run.project_id)
        if not run.sdk_session_id or not run.started_at:
            raise SkillProposalConflictError("Skill proposals require a started native SDK Run")
        if run.identity_binding != project.identity_binding or run.tenant_id != project.tenant_id:
            raise AuthorizationError("Skill proposal source identity does not match its project")
        if run.identity_binding == "authenticated_principal":
            if self.access is None:
                raise AuthorizationError("Skill proposal source authorization is unavailable")
            self.access.authorize_bound(
                run.project_id,
                tenant_id=run.tenant_id,
                principal_id=run.principal_id,
                role=run.principal_role,
                permission=ProjectPermission.PROJECT_WRITE,
            )
        elif self.auth_mode != "local_only":
            raise AuthorizationError("Local Skill proposal sources require local-only mode")
        return run

    def _related_runs(
        self,
        run: RunRecord,
        manifest: SkillProposalManifest,
    ) -> list[SkillProposalSourceRun]:
        summaries = []
        for identifier in manifest.related_run_ids:
            related = self.ledger.get_run(identifier)
            if (
                related.id == run.id
                or related.project_id != run.project_id
                or related.status not in TERMINAL_RUN_STATUSES
                or related.started_at is None
            ):
                raise SkillProposalConflictError(
                    "Related Runs must be other executed, finished Runs in the source project"
                )
            summaries.append(
                SkillProposalSourceRun(
                    id=related.id,
                    status=related.status,
                    prompt_excerpt=str(redact(related.prompt))[:240],
                    started_at=related.started_at,
                    finished_at=related.finished_at,
                )
            )
        return summaries

    def submit_artifact(self, run_id: str, artifact_id: str) -> SkillProposal:
        with (
            self.ledger._lock_for("skill-proposals"),
            self.ledger.lifecycle.lease(self.ledger.get_run(run_id).project_id),
        ):
            run = self._source_run(run_id)
            # Metadata lookup deliberately does not resolve the mutable Artifact path.
            project = self.ledger.get_project(run.project_id)
            artifact = next((row for row in project.artifacts if row.id == artifact_id), None)
            if artifact is None or artifact.project_id != run.project_id or not artifact.run_id:
                raise SkillProposalConflictError(
                    "Skill proposal requires a Run-bound source Artifact"
                )
            identifier = _proposal_id(run.project_id, artifact.run_id, artifact.id)
            try:
                existing = self._load(identifier)
            except SkillProposalNotFoundError:
                existing = None
            if existing is not None:
                return self._recover_one(existing)
            if artifact.run_id != run.id:
                raise SkillProposalConflictError(
                    "A source Artifact from another Run can only reference its existing proposal"
                )
            ids = self._ids()
            if len(ids) >= MAX_SKILL_PROPOSALS:
                raise SkillProposalQuotaError("Skill proposal count limit reached")
            raw, manifest, files = capture_submission(
                self.ledger.workspace_for_project(run.project_id),
                artifact.path,
                artifact.checksum,
            )
            related = self._related_runs(run, manifest)
            # Reserve the bounded metadata allowance now, so human review cannot
            # overrun the store quota after a publication receipt has committed.
            used = sum(
                MAX_SKILL_PROPOSAL_RECORD_BYTES
                + sum(row.size_bytes for row in self._load(key).files)
                + MAX_MANIFEST_BYTES
                for key in ids
            )
            needed = (
                MAX_SKILL_PROPOSAL_RECORD_BYTES + sum(map(len, files.values())) + MAX_MANIFEST_BYTES
            )
            if used + needed > MAX_SKILL_PROPOSAL_STORE_BYTES:
                raise SkillProposalQuotaError("Skill proposal byte limit reached")
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
            try:
                write_frozen(staging, raw, files)
                skill, scan = self.registry.inspect_candidate(staging / "package")
                metadata = self.registry._frontmatter(staging / "package" / "SKILL.md")
                if (
                    skill.name != manifest.name
                    or skill.version != manifest.version
                    or metadata.get("version") != manifest.version
                ):
                    raise SkillProposalConflictError("Manifest and Skill name/version must match")
                if (
                    capture_tree(staging / "package") != files
                    or read_relative(
                        staging,
                        "manifest.json",
                        MAX_MANIFEST_BYTES,
                    )
                    != raw
                ):
                    raise SkillProposalIntegrityError(
                        "Frozen Skill proposal changed during inspection"
                    )
                self._source_run(run.id)
                now = utc_now()
                proposal = SkillProposal(
                    id=identifier,
                    revision=1,
                    state="pending",
                    project_id=run.project_id,
                    source_run_id=run.id,
                    source_artifact_id=artifact.id,
                    source_sdk_session_id=run.sdk_session_id,
                    manifest_checksum=artifact.checksum,
                    manifest=manifest,
                    skill_id=skill.id,
                    description=skill.description,
                    content_hash=skill.content_hash,
                    files=file_inventory(files),
                    scan=scan,
                    scan_content_hash=skill.content_hash,
                    source_runs=related,
                    created_at=now,
                    updated_at=now,
                )
                self._save(proposal, directory=staging)
                os.replace(staging, self._path(identifier))
                fsync_directory(self.root)
                return proposal
            finally:
                if staging.exists():
                    _remove_staging(staging)

    def _reviewer(
        self,
        item: SkillProposal,
        principal: PrincipalContext | None,
    ) -> SkillProposalReviewer:
        project = self.ledger.get_project(item.project_id)
        if principal is None:
            if self.auth_mode != "local_only" or project.identity_binding != "local_only":
                raise AuthorizationError(
                    "Skill publication requires an authenticated human reviewer"
                )
            return SkillProposalReviewer(kind="local_owner", id="local-owner", role="owner")
        if (
            principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
            or self.access is None
            or project.identity_binding != "authenticated_principal"
            or principal.tenant_id != project.tenant_id
        ):
            raise AuthorizationError("Only a project owner or administrator can review Skills")
        self.access.authorize(item.project_id, principal, ProjectPermission.PROJECT_WRITE)
        return SkillProposalReviewer(
            kind="principal", id=principal.principal_id, role=principal.role
        )

    def _verified_files(self, item: SkillProposal) -> dict[str, bytes]:
        try:
            root = self._path(item.id)
            raw = read_relative(root, "manifest.json", MAX_MANIFEST_BYTES)
            if (
                hashlib.sha256(raw).hexdigest() != item.manifest_checksum
                or SkillProposalManifest.model_validate_json(raw) != item.manifest
            ):
                raise ValueError("manifest integrity mismatch")
            files = capture_tree(root / "package")
            if file_inventory(files) != item.files:
                raise ValueError("file inventory integrity mismatch")
            if self.registry._hash_directory(root / "package") != item.content_hash:
                raise ValueError("package content hash integrity mismatch")
            return files
        except (ValueError, OSError) as exc:
            raise SkillProposalIntegrityError(
                "Frozen Skill proposal integrity check failed"
            ) from exc

    @staticmethod
    def _receipt_matches(item: SkillProposal, receipt: SkillPublicationReceipt) -> bool:
        intent = item.review
        return bool(
            intent
            and intent.decision == "approve"
            and receipt.proposal_id == item.id
            and receipt.skill_id == item.skill_id
            and receipt.content_hash == item.content_hash
            and receipt.review_revision == intent.review_revision
            and receipt.reviewer_binding == intent.reviewer_binding
            and receipt.previous_selection == intent.expected_target
        )

    def _finish(
        self, item: SkillProposal, receipt: SkillPublicationReceipt | None
    ) -> SkillProposal:
        if item.review is None or item.review.outcome != "publishing":
            raise SkillProposalIntegrityError("Skill publication intent is missing")
        if receipt is not None and not self._receipt_matches(item, receipt):
            raise SkillProposalIntegrityError(
                "Skill publication receipt does not match its human review"
            )
        review = item.review.model_copy(
            update={"outcome": "published" if receipt else "interrupted"}
        )
        updated = item.model_copy(
            update={
                "state": "published" if receipt else "pending",
                "revision": item.revision + 1,
                "review": review,
                "history": [*item.history[:-1], review],
                "publication_receipt": receipt,
                "updated_at": utc_now(),
            }
        )
        self._save(updated)
        return updated

    def _recover_one(self, item: SkillProposal) -> SkillProposal:
        with self.ledger.lifecycle.lease(item.project_id):
            if item.state != "publishing":
                return item
            # Absence of a receipt is not consent to repeat an interrupted decision.
            receipt = self.registry.publication_receipt(item.id)
            return self._finish(item, receipt)

    def get(self, proposal_id: str) -> SkillProposal:
        with self.ledger._lock_for("skill-proposals"):
            return self._recover_one(self._load(proposal_id))

    def peek(self, proposal_id: str) -> SkillProposal:
        """Read metadata for a scope check without recovering any publication."""
        item = self._load(proposal_id)
        with self.ledger.lifecycle.lease(item.project_id):
            return item

    def list(
        self,
        *,
        recover: bool = True,
        skip_invalid: bool = False,
    ) -> list[SkillProposal]:
        with self.ledger._lock_for("skill-proposals"):
            items = []
            for identifier in self._ids(clean_staging=recover, skip_invalid=skip_invalid):
                try:
                    item = self._load(identifier)
                    with self.ledger.lifecycle.lease(item.project_id):
                        items.append(self._recover_one(item) if recover else item)
                except (ProjectUnavailableError, ProjectLifecycleBusyError):
                    # A project erasure must not block the shared listing or
                    # let background recovery recreate its private candidate.
                    continue
                except (OSError, ValueError, RuntimeError, KeyError) as exc:
                    if not skip_invalid:
                        raise
                    logger.warning("Skipped unreadable Skill proposal: %s", type(exc).__name__)
            return items

    def list_for_project(self, project_id: str) -> list[SkillProposal]:
        return [item for item in self.list() if item.project_id == project_id]

    def recover(self) -> SkillProposalRecoveryReport:
        report = SkillProposalRecoveryReport()
        with self.ledger._lock_for("skill-proposals"):
            for identifier in self._ids():
                report.inspected += 1
                try:
                    item = self._load(identifier)
                    with self.ledger.lifecycle.lease(item.project_id):
                        if item.state == "publishing":
                            updated = self._recover_one(item)
                            if updated.state == "published":
                                report.published += 1
                            else:
                                report.reset_pending += 1
                except (ProjectUnavailableError, ProjectLifecycleBusyError):
                    continue
                except (OSError, ValueError, RuntimeError, KeyError) as exc:
                    report.failed += 1
                    logger.warning("Skill proposal recovery failed: %s", type(exc).__name__)
        return report

    def review(
        self,
        proposal_id: str,
        request: SkillProposalReviewRequest,
        principal: PrincipalContext | None,
    ) -> SkillProposal:
        request = SkillProposalReviewRequest.model_validate(request.model_dump())
        with (
            self.ledger._lock_for("skill-proposals"),
            self.ledger.lifecycle.lease(self._load(proposal_id).project_id),
        ):
            item = self._recover_one(self._load(proposal_id))
            reviewer = self._reviewer(item, principal)
            if (
                item.state != "pending"
                or request.revision != item.revision
                or request.content_hash != item.content_hash
            ):
                raise SkillProposalConflictError("Skill proposal changed; reload before reviewing")
            if len(item.history) >= MAX_SKILL_PROPOSAL_REVIEWS:
                raise SkillProposalQuotaError("Skill proposal review history limit reached")
            if request.decision == "approve":
                try:
                    self._verified_files(item)
                except SkillProposalIntegrityError:
                    item = item.model_copy(
                        update={
                            "scan": SkillTestResult(
                                skill_id=item.skill_id,
                                ok=False,
                                checks=[
                                    SkillCheck(
                                        name="package_integrity",
                                        status="failed",
                                        detail="Frozen package differs from its recorded files",
                                    ),
                                ],
                            ),
                            "revision": item.revision + 1,
                            "updated_at": utc_now(),
                        }
                    )
                    self._save(item)
                    raise
                skill, scan = self.registry.inspect_candidate(self._path(item.id) / "package")
                if (
                    skill.content_hash != item.content_hash
                    or skill.id != item.skill_id
                    or skill.version != item.manifest.version
                ):
                    raise SkillProposalIntegrityError("Skill proposal inspection identity changed")
                item = item.model_copy(
                    update={"scan": scan, "scan_content_hash": skill.content_hash}
                )
                if not scan.ok:
                    item = item.model_copy(
                        update={"revision": item.revision + 1, "updated_at": utc_now()}
                    )
                    self._save(item)
                    raise SkillProposalConflictError(
                        "Frozen Skill proposal failed its current safety scan"
                    )
            # The preceding filesystem and scan work must not extend a request's
            # identity assertion or preserve membership revoked during the scan.
            self._reviewer(item, principal)
            reviewed_at = utc_now()
            revision = item.revision + 1
            binding = {
                "proposal_id": item.id,
                "review_revision": revision,
                "decision": request.decision,
                "content_hash": item.content_hash,
                "expected_target": (
                    request.expected_target.model_dump(mode="json")
                    if request.expected_target is not None
                    else None
                ),
                "reviewer": reviewer.model_dump(mode="json"),
                "note": request.note,
                "reviewed_at": reviewed_at.isoformat(),
            }
            intent = SkillProposalReview(
                **{key: value for key, value in binding.items() if key != "proposal_id"},
                reviewer_binding="0" * 64,
                outcome="publishing" if request.decision == "approve" else "rejected",
            )
            intent = intent.model_copy(
                update={"reviewer_binding": _review_binding(item.id, intent)}
            )
            item = item.model_copy(
                update={
                    "state": intent.outcome,
                    "revision": revision,
                    "review": intent,
                    "history": [*item.history, intent],
                    "updated_at": reviewed_at,
                }
            )
            if (
                request.decision == "approve"
                and len(self.ledger._dump(item).encode()) > MAX_SKILL_PROPOSAL_RECORD_BYTES - 8_192
            ):
                raise SkillProposalQuotaError(
                    "Skill proposal metadata has insufficient room for its publication receipt"
                )
            self._save(item)
            if request.decision == "reject":
                return item
            try:
                receipt = self.registry.publish_reviewed_candidate(
                    self._path(item.id) / "package",
                    proposal_id=item.id,
                    content_hash=item.content_hash,
                    review_revision=intent.review_revision,
                    reviewer_binding=intent.reviewer_binding,
                    expected_token=intent.expected_target,
                    before_commit=lambda: self._reviewer(item, principal),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                # A failing fsync can occur after the canonical receipt becomes
                # visible. Reconcile that receipt, never roll back a newer choice.
                receipt = self.registry.publication_receipt(item.id)
                if receipt is not None:
                    return self._finish(item, receipt)
                if isinstance(exc, AuthenticationError | AuthorizationError):
                    self._finish(item, None)
                    raise
                # The registry scans again under its publication lock. Preserve
                # its now-current blocking scan for the next authorized reviewer.
                skill, scan = self.registry.inspect_candidate(self._path(item.id) / "package")
                if skill.content_hash == item.content_hash:
                    item = item.model_copy(update={"scan": scan})
                self._finish(item, None)
                raise SkillProposalConflictError(
                    "Skill publication did not commit; reload and review the current target"
                ) from exc
            return self._finish(item, receipt)

    def read_file(
        self,
        proposal_id: str,
        path: str,
        *,
        content_hash: str,
    ) -> tuple[SkillProposalFile, bytes]:
        relative_parts(path)
        with (
            self.ledger._lock_for("skill-proposals"),
            self.ledger.lifecycle.lease(self._load(proposal_id).project_id),
        ):
            item = self._recover_one(self._load(proposal_id))
            if content_hash != item.content_hash:
                raise SkillProposalConflictError("Skill proposal content hash does not match")
            row = next((row for row in item.files if row.path == path), None)
            if row is None:
                raise SkillProposalNotFoundError("Skill proposal file not found")
            files = self._verified_files(item)
            return row, files[path]
