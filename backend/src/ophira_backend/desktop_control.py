"""Run-bound native window selection and screenshot provenance for FR-42."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from .constants import (
    DESKTOP_MCP_SERVER_NAME,
    TOOL_SNAPSHOT_DESKTOP,
    EvidenceKind,
    EvidenceSourceType,
    RunMode,
    RunStatus,
)
from .models import Evidence, OrgPolicy
from .skill_proposal_files import child_directory, directory_fd
from .storage import FileLedger
from .untrusted import UntrustedSource, sanitize_untrusted_text

if TYPE_CHECKING:
    from .desktop_bridge import DesktopBridge
    from .policy import OrgPolicyStore
    from .project_events import ProjectEventProjector

_BROWSER_BUNDLE_IDS = frozenset({
    "com.apple.safari",
    "com.google.chrome",
    "com.microsoft.edgemac",
    "com.brave.browser",
    "org.mozilla.firefox",
    "org.chromium.chromium",
    "com.operasoftware.opera",
    "com.vivaldi.vivaldi",
    "company.thebrowser.browser",
})
_MAX_PNG_BYTES = 12_000_000


class DesktopControlError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WindowSelection:
    run_id: str
    selection_id: str
    window_id: int
    bundle_id: str
    process_id: int
    title: str


class DesktopControl:
    def __init__(
        self,
        bridge: DesktopBridge,
        ledger: FileLedger,
        policy: OrgPolicyStore,
        project_events: ProjectEventProjector | None = None,
    ) -> None:
        self.bridge = bridge
        self.ledger = ledger
        self.policy = policy
        self.project_events = project_events
        self._selection: WindowSelection | None = None
        self._lock = asyncio.Lock()

    def _require_run(self, project_id: str, run_id: str) -> None:
        try:
            run = self.ledger.get_run(run_id)
        except (KeyError, ValueError) as exc:
            raise DesktopControlError(
                "DESKTOP_RUN_UNAVAILABLE", "Desktop capture requires an attended active Run."
            ) from exc
        if (
            run.project_id != project_id
            or run.status != RunStatus.RUNNING
            or run.sdk_session_id is None
            or run.mode == RunMode.PLAN
            or run.schedule_id is not None
            or run.event_trigger_id is not None
            or run.standing_intent_id is not None
            or run.manual_compaction
        ):
            raise DesktopControlError(
                "DESKTOP_RUN_UNAVAILABLE", "Desktop capture requires an attended active Run."
            )

    def _require_policy(self, bundle_id: str | None = None) -> OrgPolicy:
        policy = self.policy.get()
        if not policy.computer_control_enabled:
            raise DesktopControlError(
                "DESKTOP_DISABLED_BY_POLICY", "Organization policy disables desktop control."
            )
        if bundle_id is not None:
            normalized = bundle_id.casefold()
            if normalized in _BROWSER_BUNDLE_IDS:
                raise DesktopControlError(
                    "DESKTOP_BROWSER_UNSUPPORTED",
                    "Browser windows require the L1/L2 tools and their URL/domain checks.",
                )
            if normalized not in policy.allowed_desktop_bundle_ids:
                raise DesktopControlError(
                    "DESKTOP_APP_NOT_ALLOWED",
                    "The selected app is outside the organization desktop allowlist.",
                )
        return policy

    @staticmethod
    def _window_metadata(value: dict[str, Any]) -> tuple[int, str, int, str]:
        window_id = value.get("window_id")
        bundle_id = value.get("bundle_id")
        process_id = value.get("process_id")
        title = value.get("title")
        if (
            type(window_id) is not int or window_id <= 0
            or not isinstance(bundle_id, str) or len(bundle_id) > 200
            or type(process_id) is not int or process_id <= 0
            or not isinstance(title, str)
        ):
            raise DesktopControlError(
                "DESKTOP_NATIVE_RESPONSE_INVALID", "Native window identity is invalid."
            )
        safe_title = sanitize_untrusted_text(
            title, UntrustedSource.BROWSER_PAGE, max_chars=256,
        ).text
        return window_id, bundle_id, process_id, safe_title

    async def select_window(self, project_id: str, run_id: str) -> dict[str, Any]:
        async with self._lock:
            self._require_run(project_id, run_id)
            self._require_policy()
            self._selection = None
            response = await self.bridge.request(
                "select_window", run_id=run_id, timeout_seconds=120,
            )
            self._require_run(project_id, run_id)
            window_id, bundle_id, process_id, title = self._window_metadata(response)
            self._require_policy(bundle_id)
            selection = WindowSelection(
                run_id=run_id,
                selection_id=f"window_{uuid4().hex}",
                window_id=window_id,
                bundle_id=bundle_id,
                process_id=process_id,
                title=title,
            )
            self._selection = selection
            return {
                "selection_id": selection.selection_id,
                "window_id": selection.window_id,
                "bundle_id": selection.bundle_id,
                "title": selection.title,
            }

    async def snapshot(
        self, project_id: str, run_id: str, selection_id: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._require_run(project_id, run_id)
            selection = self._selection
            if (
                selection is None
                or selection.run_id != run_id
                or selection.selection_id != selection_id
            ):
                raise DesktopControlError(
                    "DESKTOP_SELECTION_UNAVAILABLE", "Select a window in this Run first."
                )
            self._require_policy(selection.bundle_id)
            response = await self.bridge.request(
                "snapshot", run_id=run_id, timeout_seconds=45,
            )
            self._require_run(project_id, run_id)
            self._require_policy(selection.bundle_id)
            window_id, bundle_id, process_id, title = self._window_metadata(response)
            if (
                window_id != selection.window_id
                or bundle_id != selection.bundle_id
                or process_id != selection.process_id
            ):
                raise DesktopControlError(
                    "DESKTOP_WINDOW_CHANGED", "Selected window identity changed before capture."
                )
            raw = response.get("image_base64")
            if not isinstance(raw, str) or len(raw) > 16_000_000:
                raise DesktopControlError(
                    "DESKTOP_IMAGE_INVALID", "Native screenshot exceeds its size limit."
                )
            try:
                png = base64.b64decode(raw, validate=True)
                with Image.open(io.BytesIO(png)) as image:
                    if (
                        image.format != "PNG"
                        or not 1 <= image.width <= 2560
                        or not 1 <= image.height <= 1600
                    ):
                        raise ValueError("invalid screenshot shape")
                    dimensions = (image.width, image.height)
                    image.verify()
            except (binascii.Error, UnidentifiedImageError, ValueError, OSError) as exc:
                raise DesktopControlError(
                    "DESKTOP_IMAGE_INVALID", "Native screenshot is invalid."
                ) from exc
            if len(png) > _MAX_PNG_BYTES:
                raise DesktopControlError(
                    "DESKTOP_IMAGE_INVALID", "Native screenshot exceeds its size limit."
                )
            result = await asyncio.to_thread(
                self._record_capture, project_id, run_id, selection, title, png, dimensions,
            )
            if self.project_events is not None:
                await self.project_events.reconcile_run(run_id)
            return result

    def _record_capture(
        self,
        project_id: str,
        run_id: str,
        selection: WindowSelection,
        title: str,
        png: bytes,
        dimensions: tuple[int, int],
    ) -> dict[str, Any]:
        self._require_run(project_id, run_id)
        self._require_policy(selection.bundle_id)
        workspace = self.ledger.workspace_for_project(project_id)
        stamp = datetime.now(UTC)
        filename = f"desktop-{stamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4().hex[:10]}.png"
        relative = f"evidence-screens/{filename}"
        with directory_fd(workspace) as workspace_fd:
            try:
                os.mkdir("evidence-screens", mode=0o700, dir_fd=workspace_fd)
            except FileExistsError:
                pass
            with child_directory(workspace_fd, ("evidence-screens",)) as folder_fd:
                descriptor = os.open(
                    filename,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=folder_fd,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(png)
                    handle.flush()
                    os.fsync(handle.fileno())
        checksum = hashlib.sha256(png).hexdigest()
        canonical = (
            f"ophira-desktop://{selection.bundle_id.casefold()}"
            f"/window/{selection.window_id}"
        )
        payload = {
            "bundle_id": selection.bundle_id,
            "window_id": selection.window_id,
            "process_id": selection.process_id,
            "title": title,
            "screenshot_path": relative,
            "sha256": checksum,
            "width": dimensions[0],
            "height": dimensions[1],
            "observed_at": stamp.isoformat(),
        }
        receipt = self.ledger.issue_tool_receipt(
            project_id, run_id,
            tool=TOOL_SNAPSHOT_DESKTOP,
            source_type=EvidenceSourceType.TOOL,
            canonical_source=canonical,
            payload=payload,
            object_type="desktop_window_capture",
            object_key=canonical,
            object_metadata={
                "bundle_id": selection.bundle_id,
                "window_id": selection.window_id,
                "screenshot_path": relative,
                "observed_at": stamp.isoformat(),
            },
        )
        evidence = Evidence(
            kind=EvidenceKind.OBSERVED_FACT,
            claim=f"Selected desktop window was captured at {stamp.isoformat()}.",
            source_title="OPHIRA selected desktop window screenshot",
            source_type=EvidenceSourceType.TOOL,
            source_path=relative,
            receipt_id=receipt.id,
            source_run_id=run_id,
            retrieved_at=receipt.retrieved_at,
            observed_at=stamp,
            location=f"run:{run_id}",
            freshness_note=(
                "A screenshot proves only the selected window's visible state "
                "at capture time."
            ),
            confidence=1.0,
            recorded_by=DESKTOP_MCP_SERVER_NAME,
            tags=["desktop", "screenshot", f"run:{run_id}"],
        )
        self.ledger.update_project(project_id, lambda project: project.evidence.append(evidence))
        artifact = self.ledger.register_artifact(
            project_id, relative, run_id=run_id,
            source_ids=[evidence.id], mime="image/png",
        )
        return {
            "ok": True,
            "selection_id": selection.selection_id,
            "window_id": selection.window_id,
            "bundle_id": selection.bundle_id,
            "title": title,
            "screenshot_path": relative,
            "sha256": checksum,
            "width": dimensions[0],
            "height": dimensions[1],
            "receipt_id": receipt.id,
            "evidence_id": evidence.id,
            "artifact_id": artifact.id,
            "observed_at": stamp.isoformat(),
        }
