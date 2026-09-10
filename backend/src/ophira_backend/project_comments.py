"""Human project discussions, outside the Agent's project-truth workspace.

Membership remains the existing access authority. A project transaction serializes
comments across workers; a membership lease prevents revocation racing a commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager, nullcontext
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .auth import ROLE_PERMISSIONS, AuthorizationError, ProjectAccessStore, ProjectPermission
from .config import Settings
from .constants import EventType, PrincipalRole
from .events import redact
from .models import PrincipalContext, utc_now
from .storage import FileLedger, LedgerNotFoundError

MAX_COMMENT_BYTES = 16 * 1024 * 1024
MAX_COMMENTS = 5000
MAX_REVISIONS = 100
COMMENT_ID = r"^comment_[a-f0-9]{32}$"
REQUEST_ID = r"^[A-Za-z0-9_-]{16,100}$"


class CollaborationConflict(ValueError):
    pass


class CollaborationUnavailable(ValueError):
    pass


class CommentAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["project", "run", "message", "evidence", "decision", "artifact"] = "project"
    id: str | None = Field(default=None, min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def coherent(self):
        if (self.kind == "project") != (self.id is None):
            raise ValueError("Only project-wide comments omit the target id")
        if (self.kind == "message") != (self.run_id is not None):
            raise ValueError("Only message comments require a source run")
        return self


class CommentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference: CommentAnchor
    label: str = Field(max_length=1000)
    trace_id: str | None = None
    artifact_version: int | None = None
    artifact_checksum: str | None = None


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=REQUEST_ID)
    text: str = Field(min_length=1, max_length=4000)
    anchor: CommentAnchor = Field(default_factory=CommentAnchor)
    parent_id: str | None = Field(default=None, pattern=COMMENT_ID)


class CommentChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=REQUEST_ID)
    expected_revision: int = Field(ge=1)
    action: Literal["edit", "remove", "resolve", "reopen"]
    text: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def edit_text(self):
        if (self.action == "edit") != (self.text is not None):
            raise ValueError("Only editing a comment accepts new text")
        return self


class CommentRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: int = Field(ge=1)
    request_id: str = Field(pattern=REQUEST_ID)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_id: str = Field(min_length=1, max_length=160)
    action: Literal["create", "edit", "remove", "resolve", "reopen"]
    text: str | None = Field(max_length=5000)
    state: Literal["open", "resolved"]
    at: AwareDatetime


class ProjectComment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=COMMENT_ID)
    project_id: str
    sequence: int = Field(ge=1)
    parent_id: str | None = Field(pattern=COMMENT_ID)
    author_id: str = Field(min_length=1, max_length=160)
    target: CommentTarget
    history: list[CommentRevision] = Field(min_length=1, max_length=MAX_REVISIONS)

    @model_validator(mode="after")
    def coherent_history(self):
        if [item.revision for item in self.history] != list(range(1, len(self.history) + 1)):
            raise ValueError("Comment revisions are not contiguous")
        if self.history[0].action != "create" or self.history[0].actor_id != self.author_id:
            raise ValueError("Comment has no valid author origin")
        first = self.history[0]
        identifier = (
            "comment_"
            + hashlib.sha256(
                f"{self.project_id}\0{self.author_id}\0{first.request_id}".encode()
            ).hexdigest()[:32]
        )
        if self.id != identifier or first.state != "open" or not first.text:
            raise ValueError("Comment origin identity is inconsistent")
        keys = {(item.actor_id, item.request_id) for item in self.history}
        if len(keys) != len(self.history):
            raise ValueError("Comment request identities are ambiguous")
        for previous, current in zip(self.history, self.history[1:], strict=False):
            if current.at < previous.at:
                raise ValueError("Comment history timestamps are not ordered")
            if current.action == "edit":
                valid = (
                    current.actor_id == self.author_id
                    and previous.text is not None
                    and bool(current.text)
                    and current.state == previous.state
                )
            elif current.action == "remove":
                valid = (
                    previous.text is not None
                    and current.text is None
                    and current.state == previous.state
                )
            elif current.action in {"resolve", "reopen"}:
                valid = (
                    self.parent_id is None
                    and current.text == previous.text
                    and current.state != previous.state
                    and current.state == ("resolved" if current.action == "resolve" else "open")
                )
            else:
                valid = False
            if not valid:
                raise ValueError("Comment history has an invalid transition")
        return self


class CommentLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    project_id: str
    identity_binding: Literal["local_only", "authenticated_principal"]
    tenant_id: str | None
    comments: list[ProjectComment] = Field(default_factory=list, max_length=MAX_COMMENTS)

    @model_validator(mode="after")
    def coherent_comments(self):
        by_id = {item.id: item for item in self.comments}
        if len(by_id) != len(self.comments):
            raise ValueError("Comment identities are ambiguous")
        if [item.sequence for item in self.comments] != list(range(1, len(self.comments) + 1)):
            raise ValueError("Comment sequence is not contiguous")
        for item in self.comments:
            if item.project_id != self.project_id:
                raise ValueError("Comment belongs to another project")
            if item.parent_id:
                parent = by_id.get(item.parent_id)
                if (
                    parent is None
                    or parent.parent_id is not None
                    or parent.sequence >= item.sequence
                    or parent.target != item.target
                ):
                    raise ValueError("Comment reply has no matching root")
        return self


def _clean(text: str) -> str:
    if not text.strip():
        raise ValueError("Comment text must not be blank")
    return str(redact(text.strip()))[:5000]


def _hash(value: BaseModel) -> str:
    return hashlib.sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ProjectComments:
    def __init__(self, ledger: FileLedger, access: ProjectAccessStore, settings: Settings):
        self.ledger, self.access, self.settings = ledger, access, settings
        self.root = ledger.root / "collaboration" / "comments"
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise CollaborationUnavailable("Collaboration storage must not be linked")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def authorize(self, project_id, principal, permission=ProjectPermission.PROJECT_READ):
        project = self.ledger.get_project(project_id)
        if self.settings.auth_mode == "local_only":
            if principal is not None or project.identity_binding != "local_only":
                raise AuthorizationError("Project is outside local deployment scope")
        else:
            if (
                principal is None
                or project.identity_binding != "authenticated_principal"
                or project.tenant_id != self.settings.trusted_gateway_tenant_id
            ):
                raise AuthorizationError("Project is outside authenticated deployment scope")
            self.access.authorize(project_id, principal, permission)
        return project

    @staticmethod
    def actor_id(principal):
        return principal.principal_id if principal else "local-operator"

    @staticmethod
    def moderator(principal):
        return principal is None or principal.role in {PrincipalRole.OWNER, PrincipalRole.ADMIN}

    def _path(self, project_id):
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise CollaborationUnavailable("Collaboration storage must not be linked")
        return self.root / f"{self.ledger._safe_id(project_id)}.json"

    @contextmanager
    def _transaction(self, project_id, principal, permission=ProjectPermission.PROJECT_READ):
        authority = self.access._transaction(project_id) if principal else nullcontext()
        with authority, self.ledger._lock_for(f"project-comments:{project_id}"):
            project = self.authorize(project_id, principal, permission)
            path = self._path(project_id)
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                record = CommentLedger(
                    project_id=project.id,
                    tenant_id=project.tenant_id,
                    identity_binding=project.identity_binding,
                )
            else:
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise CollaborationUnavailable("Comment history must be a regular file")
                    raw = stream.read(MAX_COMMENT_BYTES + 1)
                if len(raw) > MAX_COMMENT_BYTES:
                    raise CollaborationUnavailable("Comment history exceeds the read limit")
                try:
                    record = CommentLedger.model_validate_json(raw)
                except ValueError as exc:
                    raise CollaborationUnavailable("Comment history could not be verified") from exc
            if (
                record.project_id != project.id
                or record.tenant_id != project.tenant_id
                or record.identity_binding != project.identity_binding
            ):
                raise CollaborationUnavailable("Comment history has a different identity")
            yield project, record
            self.authorize(project_id, principal, permission)

    def _commit(self, record, principal):
        self.authorize(record.project_id, principal, ProjectPermission.COMMENT_WRITE)
        validated = CommentLedger.model_validate(record.model_dump())
        if len((self.ledger._dump(validated) + "\n").encode()) > MAX_COMMENT_BYTES:
            raise CollaborationConflict("Project comment storage is full")
        self.ledger._atomic_write(self._path(record.project_id), validated)

    def _target(self, project, reference):
        label, trace, version, checksum = project.name, None, None, None
        if reference.kind in {"evidence", "decision", "artifact"}:
            collection = getattr(
                project,
                {"evidence": "evidence", "decision": "decisions", "artifact": "artifacts"}[
                    reference.kind
                ],
            )
            item = next((item for item in collection if item.id == reference.id), None)
            if item is None:
                raise LedgerNotFoundError("Comment target not found in this project")
            if reference.kind == "artifact":
                label, version, checksum = item.name, item.version, item.checksum
            else:
                label = item.claim if reference.kind == "evidence" else item.outcome
        elif reference.kind in {"run", "message"}:
            run = self.ledger.get_run(
                reference.run_id if reference.kind == "message" else reference.id
            )
            if run.project_id != project.id:
                raise AuthorizationError("Comment target belongs to another project")
            label, trace = run.prompt, run.trace_id
            if reference.kind == "message":
                rows = self.ledger.list_events(run.id, max_bytes=16 * 1024 * 1024, max_events=50000)
                item = next((item for item in rows if item.id == reference.id), None)
                if (
                    item is None
                    or item.type != EventType.ASSISTANT_MESSAGE_COMPLETED
                    or item.actor.agent_id
                    or item.parent_id
                    or item.project_id != project.id
                    or item.trace_id != run.trace_id
                    or item.run_id != run.id
                    or not isinstance(item.data.get("text"), str)
                ):
                    raise LedgerNotFoundError("Public message target not found")
                label = item.data["text"]
        return CommentTarget(
            reference=reference,
            label=str(redact(label))[:1000],
            trace_id=trace,
            artifact_version=version,
            artifact_checksum=checksum,
        )

    def _view(self, item, principal, *, reply_count=0):
        latest = item.history[-1]
        owns = item.author_id == self.actor_id(principal)
        manages = owns or self.moderator(principal)
        can_write = principal is None or (
            ProjectPermission.COMMENT_WRITE in ROLE_PERMISSIONS[principal.role]
        )
        return {
            "id": item.id,
            "project_id": item.project_id,
            "sequence": item.sequence,
            "parent_id": item.parent_id,
            "author_id": item.author_id,
            "target": item.target.model_dump(mode="json"),
            "revision": latest.revision,
            "text": latest.text,
            "state": latest.state,
            "created_at": item.history[0].at,
            "updated_at": latest.at,
            "reply_count": reply_count,
            "actions": {
                "edit": can_write and owns and latest.text is not None,
                "remove": can_write and manages and latest.text is not None,
                "resolve": can_write and manages and item.parent_id is None,
                "history": owns or self.moderator(principal),
            },
        }

    def capabilities(self, project_id, principal):
        authority = self.access._transaction(project_id) if principal else nullcontext()
        with authority:
            self.authorize(project_id, principal)
            allowed = (
                ROLE_PERMISSIONS[principal.role] if principal else frozenset(ProjectPermission)
            )
            result = {
                "project_id": project_id,
                "identity_mode": self.settings.auth_mode,
                "actor_id": self.actor_id(principal),
                "role": principal.role if principal else None,
                "can_comment": ProjectPermission.COMMENT_WRITE in allowed,
                "can_manage_members": principal is not None
                and (ProjectPermission.PROJECT_ACCESS_MANAGE in allowed),
                "grantable_roles": (
                    ["admin"] if principal and principal.role == PrincipalRole.OWNER else []
                )
                + (
                    ["member", "reviewer", "viewer"]
                    if principal and self.moderator(principal)
                    else []
                ),
            }
            self.authorize(project_id, principal)
            return result

    def list(self, project_id, principal, *, before=None, limit=20):
        with self._transaction(project_id, principal) as (_, record):
            roots = [
                item
                for item in reversed(record.comments)
                if item.parent_id is None and (before is None or item.sequence < before)
            ]
            page = roots[:limit]
            result = {
                "project_id": project_id,
                "items": [
                    self._view(
                        item,
                        principal,
                        reply_count=sum(reply.parent_id == item.id for reply in record.comments),
                    )
                    for item in page
                ],
                "next_before": page[-1].sequence if len(roots) > limit else None,
            }
        return result

    def thread(self, project_id, comment_id, principal, *, after=0, limit=50):
        with self._transaction(project_id, principal) as (_, record):
            root = next(
                (
                    item
                    for item in record.comments
                    if item.id == comment_id and item.parent_id is None
                ),
                None,
            )
            if root is None:
                raise LedgerNotFoundError("Discussion not found")
            replies = [
                item
                for item in record.comments
                if item.parent_id == root.id and item.sequence > after
            ]
            page = replies[:limit]
            result = {
                "project_id": project_id,
                "root": self._view(root, principal),
                "replies": [self._view(item, principal) for item in page],
                "next_after": page[-1].sequence if len(replies) > limit else None,
            }
        return result

    def create(self, project_id, payload: CommentCreate, principal: PrincipalContext | None):
        actor = self.actor_id(principal)
        identifier = (
            "comment_"
            + hashlib.sha256(f"{project_id}\0{actor}\0{payload.request_id}".encode()).hexdigest()[
                :32
            ]
        )
        request_hash = _hash(payload)
        text = _clean(payload.text)
        with self._transaction(project_id, principal, ProjectPermission.COMMENT_WRITE) as (
            project,
            record,
        ):
            previous = next((item for item in record.comments if item.id == identifier), None)
            if previous is not None:
                if previous.history[0].request_hash != request_hash:
                    raise CollaborationConflict(
                        "This comment request already has different content"
                    )
                return self._view(previous, principal)
            if len(record.comments) >= MAX_COMMENTS:
                raise CollaborationConflict("Project comment capacity reached")
            if payload.parent_id:
                parent = next(
                    (
                        item
                        for item in record.comments
                        if item.id == payload.parent_id and item.parent_id is None
                    ),
                    None,
                )
                if parent is None:
                    raise LedgerNotFoundError("Reply requires a discussion in this project")
                if payload.anchor.kind != "project":
                    raise ValueError("Replies inherit their discussion's target")
                target = parent.target
            else:
                target = self._target(project, payload.anchor)
            item = ProjectComment(
                id=identifier,
                project_id=project_id,
                sequence=len(record.comments) + 1,
                parent_id=payload.parent_id,
                author_id=actor,
                target=target,
                history=[
                    CommentRevision(
                        revision=1,
                        request_id=payload.request_id,
                        request_hash=request_hash,
                        actor_id=actor,
                        action="create",
                        text=text,
                        state="open",
                        at=utc_now(),
                    )
                ],
            )
            updated = record.model_copy(update={"comments": [*record.comments, item]})
            self._commit(updated, principal)
            return self._view(item, principal)

    def change(self, project_id, comment_id, payload: CommentChange, principal):
        with self._transaction(project_id, principal, ProjectPermission.COMMENT_WRITE) as (
            _,
            record,
        ):
            item = next((item for item in record.comments if item.id == comment_id), None)
            if item is None:
                raise LedgerNotFoundError("Comment not found")
            actor, fingerprint = self.actor_id(principal), _hash(payload)
            own, moderate = item.author_id == actor, self.moderator(principal)
            if (payload.action == "edit" and not own) or (not own and not moderate):
                raise AuthorizationError("This action requires the author or a project moderator")
            previous = next(
                (
                    entry
                    for entry in item.history
                    if entry.actor_id == actor and entry.request_id == payload.request_id
                ),
                None,
            )
            if previous is not None:
                if previous.request_hash != fingerprint:
                    raise CollaborationConflict("This change request has different content")
                return self._view(item, principal)
            latest = item.history[-1]
            if latest.revision != payload.expected_revision:
                raise CollaborationConflict("Comment changed; reload before editing")
            if len(item.history) >= MAX_REVISIONS:
                raise CollaborationConflict("Comment history limit reached; start a new discussion")
            text, state = latest.text, latest.state
            if payload.action in {"edit", "remove"}:
                if text is None:
                    raise CollaborationConflict("Removed comments cannot be edited")
                text = _clean(payload.text) if payload.action == "edit" else None
            else:
                if item.parent_id is not None:
                    raise CollaborationConflict("Only the root discussion can be resolved")
                state = "resolved" if payload.action == "resolve" else "open"
                if state == latest.state:
                    raise CollaborationConflict("Discussion is already in that state")
            changed = item.model_copy(
                update={
                    "history": [
                        *item.history,
                        CommentRevision(
                            revision=latest.revision + 1,
                            request_id=payload.request_id,
                            request_hash=fingerprint,
                            actor_id=actor,
                            action=payload.action,
                            text=text,
                            state=state,
                            at=utc_now(),
                        ),
                    ]
                }
            )
            self._commit(
                record.model_copy(
                    update={
                        "comments": [
                            changed if row.id == item.id else row for row in record.comments
                        ]
                    }
                ),
                principal,
            )
            return self._view(changed, principal)

    def history(self, project_id, comment_id, principal):
        with self._transaction(project_id, principal) as (_, record):
            item = next((item for item in record.comments if item.id == comment_id), None)
            if item is None:
                raise LedgerNotFoundError("Comment not found")
            if item.author_id != self.actor_id(principal) and not self.moderator(principal):
                raise AuthorizationError("Comment history requires its author or a moderator")
            return {
                "project_id": project_id,
                "comment_id": item.id,
                "revisions": [
                    {
                        "revision": row.revision,
                        "actor_id": row.actor_id,
                        "action": row.action,
                        "text": row.text,
                        "state": row.state,
                        "at": row.at,
                    }
                    for row in item.history
                ],
            }
