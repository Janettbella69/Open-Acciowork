"""Organization-scoped curation over the existing immutable SkillRegistry."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .auth import AuthAssertionExpiredError, AuthorizationError
from .backup_files import (
    BackupLimits,
    parse_json,
    publish_exclusive,
    remove_staging,
    tree_inventory,
)
from .constants import PrincipalRole
from .events import redact
from .models import SkillVersionResponse, utc_now
from .project_scope import LOCAL_OPERATOR_ID
from .skill_import_models import (
    IMPORT_ID,
    IMPORT_REQUEST_ID,
    MAX_SKILL_IMPORT_BYTES,
    MAX_SKILL_IMPORT_RECORD_BYTES,
    MAX_SKILL_IMPORT_REVIEWS,
    MAX_SKILL_IMPORTS,
    SkillImport,
    SkillImportReview,
    SkillImportReviewRequest,
    SkillRepositoryScope,
)
from .skill_proposal_files import (
    capture_tree,
    directory_fd,
    file_inventory,
    fsync_directory,
    read_relative,
    relative_parts,
    write_package,
)
from .skill_proposal_models import SkillProposalReviewer
from .skill_proposals import _review_binding
from .skills import (
    CONTENT_HASH_PATTERN,
    TENANT_PLUGIN_NAMESPACE,
    SkillConflictError,
    SkillIntegrityError,
    SkillNotFoundError,
)
from .storage import LedgerNotFoundError


def digest(value):
    body = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def package_digest(files):
    result = hashlib.sha256()
    for path, content in sorted(files.items()):
        result.update(path.encode() + b"\0" + content + b"\0")
    return result.hexdigest()


class SkillRepository:
    def __init__(self, ledger, registry, settings=None, *, scope=None):
        self.ledger, self.registry = ledger, registry
        self.root = ledger.root / "skill-imports"
        with directory_fd(self.root.parent):
            self.root.mkdir(mode=0o700, exist_ok=True)
        with directory_fd(self.root):
            pass
        self.scope = scope or SkillRepositoryScope(
            auth_mode=settings.auth_mode,
            tenant_id=settings.trusted_gateway_tenant_id
            if settings.auth_mode == "trusted_gateway"
            else None,
        )

    def authorize(self, principal, *, manage=False):
        if self.scope.auth_mode == "local_only":
            if principal is not None:
                raise AuthorizationError("Private Skill repository requires the local operator")
        elif principal is None or principal.tenant_id != self.scope.tenant_id:
            raise AuthorizationError("Private Skill repository is outside this organization")
        elif utc_now() >= principal.assertion_expires_at:
            raise AuthAssertionExpiredError("Private Skill repository assertion expired")
        if (
            manage
            and principal is not None
            and principal.role not in {PrincipalRole.OWNER, PrincipalRole.ADMIN}
        ):
            raise AuthorizationError("Private Skill management requires an administrator")
        try:
            stored = SkillRepositoryScope.model_validate(
                parse_json(read_relative(self.root, "scope.json", 2048))
            )
        except FileNotFoundError:
            stored = None
        if stored is not None and stored != self.scope:
            raise AuthorizationError("Private Skill repository belongs to another deployment scope")

    @staticmethod
    def reviewer(principal):
        return (
            SkillProposalReviewer(kind="principal", id=principal.principal_id, role=principal.role)
            if principal
            else SkillProposalReviewer(kind="local_owner", id=LOCAL_OPERATOR_ID, role="owner")
        )

    @contextmanager
    def transaction(self, principal, *, manage=False):
        with self.ledger._lock_for("skill-imports"):
            self.authorize(principal, manage=manage)
            if not (self.root / "scope.json").exists():
                self.write(self.root / "scope.json", self.scope)
            yield
            self.authorize(principal, manage=manage)

    def write(self, path, model):
        value = type(model).model_validate(model.model_dump())
        if len((self.ledger._dump(value) + "\n").encode()) > MAX_SKILL_IMPORT_RECORD_BYTES:
            raise SkillConflictError("Private Skill review history is full")
        with directory_fd(path.parent):
            if path.is_symlink():
                raise SkillIntegrityError("Private Skill history must not be linked")
            self.ledger._atomic_write(path, value)

    def path(self, identifier):
        if not re.fullmatch(IMPORT_ID, identifier):
            raise LedgerNotFoundError("Private Skill import not found")
        return self.root / identifier

    def identifiers(self):
        with directory_fd(self.root):
            entries = list(self.root.iterdir())
        if len(entries) > MAX_SKILL_IMPORTS * 2 + 20:
            raise SkillConflictError("Private Skill import capacity reached")
        result = []
        for path in entries:
            if path.name == "scope.json" or (
                path.name.startswith(".") and path.name.endswith(".tmp")
            ):
                continue
            if path.name.startswith(".import-") and path.is_dir() and not path.is_symlink():
                continue
            if not re.fullmatch(IMPORT_ID, path.name) or path.is_symlink() or not path.is_dir():
                raise SkillIntegrityError("Private Skill import storage contains an unsafe entry")
            result.append(path.name)
        if len(result) > MAX_SKILL_IMPORTS:
            raise SkillConflictError("Private Skill import capacity reached")
        return result

    def clean_staging(self):
        # The shared import lock excludes every live uploader. Only unpublished,
        # host-created directories are eligible for this crash cleanup.
        for path in self.root.iterdir():
            if path.name.startswith(".import-"):
                tree_inventory(
                    path,
                    BackupLimits(
                        files=256, entries=1024, file_bytes=5_000_000, total_bytes=6_000_000
                    ),
                )
                remove_staging(path)
        fsync_directory(self.root)

    def load(self, identifier):
        try:
            raw = read_relative(self.path(identifier), "import.json", MAX_SKILL_IMPORT_RECORD_BYTES)
            parse_json(raw)
            item = SkillImport.model_validate_json(raw)
            identity = digest(
                [
                    item.scope.model_dump(),
                    item.uploaded_by.kind,
                    item.uploaded_by.id,
                    item.request_id,
                ]
            )
            if (
                item.id != identifier
                or item.id != "skill_import_" + identity
                or item.scope != self.scope
            ):
                raise ValueError("Import identity does not match its scope")
            previous, requests = 1, set()
            for index, review in enumerate(item.history):
                key = (review.reviewer.kind, review.reviewer.id, review.request_id)
                if (
                    review.review_revision != previous + 1
                    or review.content_hash != item.skill.content_hash
                    or key in requests
                    or _review_binding(item.id, review) != review.reviewer_binding
                    or (index < len(item.history) - 1 and review.outcome != "interrupted")
                    or (review.decision == "reject") != (review.outcome == "rejected")
                ):
                    raise ValueError("Import review history is inconsistent")
                previous = review.review_revision + (review.outcome in {"published", "interrupted"})
                requests.add(key)
            if item.revision != previous:
                raise ValueError("Import revision does not match its history")
            if item.publication_receipt and not self.receipt_matches(
                item, item.publication_receipt
            ):
                raise ValueError("Import receipt does not match its review")
            return item
        except FileNotFoundError as exc:
            raise LedgerNotFoundError("Private Skill import not found") from exc
        except (ValueError, OSError) as exc:
            raise SkillIntegrityError("Private Skill import could not be verified") from exc

    def verified_files(self, item):
        try:
            files = capture_tree(self.path(item.id) / "package")
        except (ValueError, OSError) as exc:
            raise SkillIntegrityError("Private Skill package could not be verified") from exc
        if file_inventory(files) != item.files or package_digest(files) != item.skill.content_hash:
            raise SkillIntegrityError("Private Skill package differs from its reviewed files")
        return files

    def view(self, item):
        return {
            "import": item.model_dump(mode="json"),
            "current_target": self.registry.selection_token(item.skill.id).model_dump(mode="json"),
            "can_publish": item.state == "pending" and item.scan.ok,
        }

    def capabilities(self, principal):
        with self.transaction(principal):
            return {
                "identity_mode": self.scope.auth_mode,
                "tenant_id": self.scope.tenant_id,
                "actor_id": principal.principal_id if principal else LOCAL_OPERATOR_ID,
                "can_manage": principal is None
                or principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN},
            }

    def list_imports(self, principal):
        with self.transaction(principal, manage=True):
            self.clean_staging()
            items = [self.recover(self.load(identifier)) for identifier in self.identifiers()]
            items.sort(key=lambda row: (row.created_at, row.id), reverse=True)
            return [self.view(item) for item in items]

    def detail(self, identifier, principal):
        with self.transaction(principal, manage=True):
            item = self.recover(self.load(identifier))
            self.verified_files(item)
            return self.view(item)

    def submit(self, payload, filename, request_id, principal):
        if not re.fullmatch(IMPORT_REQUEST_ID, request_id):
            raise ValueError("Private Skill import requires a request ID")
        with self.transaction(principal, manage=True):
            self.clean_staging()
            actor = self.reviewer(principal)
            identifier = "skill_import_" + digest(
                [self.scope.model_dump(), actor.kind, actor.id, request_id]
            )
            archive_hash = hashlib.sha256(payload).hexdigest()
            try:
                existing = self.load(identifier)
            except LedgerNotFoundError:
                existing = None
            if existing:
                if existing.archive_hash != archive_hash:
                    raise SkillConflictError("This Skill import request has different bytes")
                return self.view(self.recover(existing))
            if len(self.identifiers()) >= MAX_SKILL_IMPORTS:
                raise SkillConflictError("Private Skill import capacity reached")
            files = self.registry.unpack_zip(payload)
            inventory = tree_inventory(
                self.root,
                BackupLimits(
                    files=15000,
                    entries=20000,
                    file_bytes=5_000_000,
                    total_bytes=MAX_SKILL_IMPORT_BYTES,
                ),
            )
            if (
                sum(row["size"] for row in inventory["files"])
                + sum(map(len, files.values()))
                + MAX_SKILL_IMPORT_RECORD_BYTES
                > MAX_SKILL_IMPORT_BYTES
            ):
                raise SkillConflictError("Private Skill import storage is full")
            stage = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
            try:
                write_package(stage / "package", files)
                skill, scan = self.registry.inspect_candidate(stage / "package")
                try:
                    previously_published = any(
                        row.content_hash == skill.content_hash
                        for row in self.visible_versions(skill.id)
                    )
                except SkillNotFoundError:
                    previously_published = False
                now = utc_now()
                item = SkillImport(
                    id=identifier,
                    request_id=request_id,
                    archive_hash=archive_hash,
                    filename=str(redact(Path(filename).name))[:160] or "skill.zip",
                    scope=self.scope,
                    uploaded_by=actor,
                    published_before_import=previously_published,
                    skill=SkillVersionResponse.model_validate(skill.model_dump(exclude={"path"})),
                    files=file_inventory(files),
                    scan=scan,
                    revision=1,
                    state="pending",
                    created_at=now,
                    updated_at=now,
                )
                self.write(stage / "import.json", item)
                self.authorize(principal, manage=True)
                publish_exclusive(stage, self.path(identifier))
                fsync_directory(self.root)
                return self.view(item)
            finally:
                if stage.exists():
                    remove_staging(stage)

    @staticmethod
    def receipt_matches(item, receipt):
        if not item.history:
            return False
        intent = item.history[-1]
        return (
            intent.decision == "approve"
            and receipt.proposal_id == item.id
            and receipt.skill_id == item.skill.id
            and receipt.content_hash == item.skill.content_hash
            and receipt.review_revision == intent.review_revision
            and receipt.reviewer_binding == intent.reviewer_binding
            and receipt.previous_selection == intent.expected_target
        )

    def finish(self, item, receipt):
        if receipt and not self.receipt_matches(item, receipt):
            raise SkillIntegrityError("Private Skill publication receipt does not match")
        review = item.history[-1].model_copy(
            update={"outcome": "published" if receipt else "interrupted"}
        )
        item = item.model_copy(
            update={
                "state": "published" if receipt else "pending",
                "revision": item.revision + 1,
                "history": [*item.history[:-1], review],
                "publication_receipt": receipt,
                "updated_at": utc_now(),
            }
        )
        self.write(self.path(item.id) / "import.json", item)
        return item

    def recover(self, item):
        receipt = self.registry.publication_receipt(item.id)
        if item.state == "publishing":
            return self.finish(item, receipt)
        if receipt != item.publication_receipt:
            raise SkillIntegrityError(
                "Private Skill publication receipt is missing or inconsistent"
            )
        return item

    def review(self, identifier, payload: SkillImportReviewRequest, principal):
        with self.transaction(principal, manage=True):
            item = self.recover(self.load(identifier))
            actor, request_hash = self.reviewer(principal), digest(payload)
            previous = next(
                (
                    row
                    for row in item.history
                    if row.request_id == payload.request_id
                    and row.reviewer.kind == actor.kind
                    and row.reviewer.id == actor.id
                ),
                None,
            )
            if previous:
                if previous.request_hash != request_hash:
                    raise SkillConflictError("This Skill review request has different content")
                if previous.outcome == "interrupted":
                    raise SkillConflictError(
                        "Skill publication was interrupted; reload and review again"
                    )
                return self.view(item)
            if (
                item.state != "pending"
                or item.revision != payload.revision
                or item.skill.content_hash != payload.content_hash
            ):
                raise SkillConflictError("Private Skill import changed; reload before reviewing")
            if len(item.history) >= MAX_SKILL_IMPORT_REVIEWS:
                raise SkillConflictError("Private Skill review history is full")
            if payload.decision == "approve":
                self.verified_files(item)
                skill, scan = self.registry.inspect_candidate(self.path(item.id) / "package")
                if skill.model_dump(exclude={"path"}) != item.skill.model_dump():
                    raise SkillIntegrityError("Private Skill candidate identity changed")
                if not scan.ok:
                    raise SkillConflictError("Private Skill package failed its current safety scan")
                item = item.model_copy(update={"scan": scan})
            self.authorize(principal, manage=True)
            review = SkillImportReview(
                request_id=payload.request_id,
                request_hash=request_hash,
                review_revision=item.revision + 1,
                decision=payload.decision,
                content_hash=item.skill.content_hash,
                expected_target=payload.expected_target,
                reviewer=actor,
                note=str(redact(payload.note)),
                reviewed_at=utc_now(),
                reviewer_binding="0" * 64,
                outcome="publishing" if payload.decision == "approve" else "rejected",
            )
            review = review.model_copy(
                update={"reviewer_binding": _review_binding(item.id, review)}
            )
            item = item.model_copy(
                update={
                    "state": review.outcome,
                    "revision": review.review_revision,
                    "history": [*item.history, review],
                    "updated_at": review.reviewed_at,
                }
            )
            if len(self.ledger._dump(item).encode()) > MAX_SKILL_IMPORT_RECORD_BYTES - 8192:
                raise SkillConflictError("Private Skill review history is full")
            self.write(self.path(item.id) / "import.json", item)
            if payload.decision == "reject":
                return self.view(item)
            try:
                receipt = self.registry.publish_reviewed_candidate(
                    self.path(item.id) / "package",
                    proposal_id=item.id,
                    content_hash=item.skill.content_hash,
                    review_revision=review.review_revision,
                    reviewer_binding=review.reviewer_binding,
                    expected_token=review.expected_target,
                    before_commit=lambda: self.authorize(principal, manage=True),
                )
            except (OSError, ValueError, RuntimeError, AuthorizationError) as exc:
                receipt = self.registry.publication_receipt(item.id)
                item = self.finish(item, receipt)
                if receipt:
                    return self.view(item)
                if isinstance(exc, (AuthorizationError, AuthAssertionExpiredError)):
                    raise
                raise SkillConflictError(
                    "Skill publication was interrupted; reload and review again"
                ) from exc
            return self.view(self.finish(item, receipt))

    def visible_versions(self, identifier):
        """Called under the import lock; archiving alone is not a human publication.

        Existing operator history remains readable. New human imports require a
        canonical receipt even if a crash left their immutable archive on disk.
        """
        versions = self.registry.versions(identifier)
        imported, published = set(), set()
        for import_id in self.identifiers():
            item = self.load(import_id)
            if item.skill.id != identifier:
                continue
            receipt = self.registry.publication_receipt(item.id)
            if (receipt is not None and not self.receipt_matches(item, receipt)) or (
                item.state != "publishing" and receipt != item.publication_receipt
            ):
                raise SkillIntegrityError("Private Skill publication receipt is inconsistent")
            imported.add(item.skill.content_hash)
            if item.published_before_import or receipt is not None:
                published.add(item.skill.content_hash)
        return [row for row in versions if row.content_hash not in imported - published]

    def versions(self, identifier, principal):
        with self.transaction(principal):
            return self.visible_versions(identifier)

    def rollback(self, identifier, *, version=None, content_hash=None, principal):
        with self.transaction(principal, manage=True):
            if identifier.startswith(TENANT_PLUGIN_NAMESPACE + ":"):
                # The compatibility API must not select an archive whose human
                # publication stopped before its canonical receipt was committed.
                candidates = [
                    row
                    for row in self.visible_versions(identifier)
                    if (
                        row.content_hash == content_hash if content_hash else row.version == version
                    )
                ]
                if not candidates:
                    raise SkillNotFoundError("Private Skill published version not found")
                if len(candidates) > 1:
                    raise SkillConflictError("Multiple Skill versions match; select content_hash")
                version, content_hash = None, candidates[0].content_hash
            return self.registry.rollback(
                identifier,
                version=version,
                content_hash=content_hash,
                before_commit=lambda: self.authorize(principal, manage=True),
            )

    def published_package(self, name, content_hash, principal):
        with self.transaction(principal):
            identifier = f"{TENANT_PLUGIN_NAMESPACE}:{name}"
            if not CONTENT_HASH_PATTERN.fullmatch(content_hash):
                raise ValueError("Invalid private Skill version hash")
            versions = self.visible_versions(identifier)
            selected = next((row for row in versions if row.content_hash == content_hash), None)
            if selected is None:
                raise LedgerNotFoundError("Private Skill version not found")
            files = capture_tree(Path(selected.path).parent)
            if package_digest(files) != selected.content_hash:
                raise SkillIntegrityError("Private Skill version failed integrity verification")
            return selected, files

    def package_view(self, name, content_hash, principal):
        skill, files = self.published_package(name, content_hash, principal)
        return {
            "current_target": self.registry.selection_token(skill.id).model_dump(mode="json"),
            "skill": skill.model_dump(mode="json", exclude={"path"}),
            "files": [row.model_dump() for row in file_inventory(files)],
        }

    def read_file(self, identifier, path, principal, *, content_hash=None):
        relative_parts(path)
        if content_hash is None:
            with self.transaction(principal, manage=True):
                item = self.recover(self.load(identifier))
                files = self.verified_files(item)
        else:
            _, files = self.published_package(identifier, content_hash, principal)
        if path not in files:
            raise LedgerNotFoundError("Private Skill file not found")
        raw = files[path]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        preview = None if text is None or "\0" in text else str(redact(text))[:12000]
        return {
            "path": path,
            "content_hash": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "text": preview,
            "truncated": text is not None and len(text) > 12000,
        }, raw

    def export(self, name, content_hash, principal):
        skill, files = self.published_package(name, content_hash, principal)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in sorted(files.items()):
                archive.writestr(path, content)
        self.authorize(principal)
        return skill, buffer.getvalue()

    def select(self, name, payload, principal):
        with self.transaction(principal, manage=True):
            identifier = f"{TENANT_PLUGIN_NAMESPACE}:{name}"

            def guard():
                self.authorize(principal, manage=True)

            if payload.operation == "rollback":
                if not any(
                    row.content_hash == payload.content_hash
                    for row in self.visible_versions(identifier)
                ):
                    raise SkillNotFoundError("Private Skill published version not found")
                selected = self.registry.rollback(
                    identifier,
                    content_hash=payload.content_hash,
                    expected_token=payload.expected_target,
                    before_commit=guard,
                )
            else:
                if payload.content_hash != payload.expected_target.current_hash:
                    raise SkillConflictError("Enablement must target the reviewed current version")
                selected = self.registry.set_enabled(
                    identifier,
                    payload.operation == "enable",
                    expected_token=payload.expected_target,
                    before_commit=guard,
                )
            return {
                "skill": selected.model_dump(mode="json", exclude={"path"}),
                "current_target": self.registry.selection_token(identifier).model_dump(mode="json"),
            }
