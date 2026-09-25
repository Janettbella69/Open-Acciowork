"""Run-bound native window capture and accessible actions for FR-42."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from .constants import (
    DESKTOP_MCP_SERVER_NAME,
    TOOL_PRESS_DESKTOP,
    TOOL_REPLACE_DESKTOP_TEXT,
    TOOL_SEND_DESKTOP_KEY,
    TOOL_SNAPSHOT_DESKTOP,
    EvidenceKind,
    EvidenceSourceType,
    RunMode,
    RunStatus,
)
from .models import Evidence, OrgPolicy, ToolReceipt
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
_CAPTURE_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_MAX_ACTION_AGE_SECONDS = 120
_MAX_TEXT_BYTES = 4096
_MAX_TEXT_REQUEST_BYTES = 4000
_ALLOWED_KEYS = frozenset({
    "Return", "Escape", "Tab", "Space", "Backspace", "Up", "Down", "Left", "Right",
})


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


@dataclass(frozen=True)
class WindowCapture:
    run_id: str
    selection_id: str
    receipt_id: str
    native_token: str
    title: str
    width: int
    height: int
    recorded_at_monotonic: float


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
        self._capture: WindowCapture | None = None
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
            self._capture = None
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
            # A new observation attempt supersedes the old screenshot even if
            # the native capture or evidence write later fails.
            self._capture = None
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
            native_token = response.get("capture_token")
            if not isinstance(native_token, str) or not _CAPTURE_TOKEN.fullmatch(native_token):
                raise DesktopControlError(
                    "DESKTOP_NATIVE_RESPONSE_INVALID", "Native screenshot identity is invalid."
                )
            result = await asyncio.to_thread(
                self._record_capture, project_id, run_id, selection, title, png, dimensions,
            )
            self._capture = WindowCapture(
                run_id=run_id,
                selection_id=selection.selection_id,
                receipt_id=result["receipt_id"],
                native_token=native_token,
                title=title,
                width=dimensions[0],
                height=dimensions[1],
                recorded_at_monotonic=time.monotonic(),
            )
            if self.project_events is not None:
                await self.project_events.reconcile_run(run_id)
            return result

    def _require_capture(
        self, project_id: str, run_id: str,
        selection_id: str, capture_receipt_id: str,
    ) -> tuple[WindowSelection, WindowCapture]:
        self._require_run(project_id, run_id)
        selection = self._selection
        capture = self._capture
        if (
            selection is None or selection.run_id != run_id
            or selection.selection_id != selection_id
            or capture is None or capture.run_id != run_id
            or capture.selection_id != selection_id
            or capture.receipt_id != capture_receipt_id
        ):
            raise DesktopControlError(
                "DESKTOP_CAPTURE_UNAVAILABLE",
                "Capture the selected window in this Run before an input action.",
            )
        self._require_policy(selection.bundle_id)
        if time.monotonic() - capture.recorded_at_monotonic > _MAX_ACTION_AGE_SECONDS:
            raise DesktopControlError(
                "DESKTOP_CAPTURE_STALE", "Capture a fresh screenshot before acting."
            )
        return selection, capture

    @staticmethod
    def _check_coordinate(capture: WindowCapture, x: int, y: int) -> None:
        if (
            type(x) is not int or type(y) is not int
            or not 0 <= x < capture.width or not 0 <= y < capture.height
        ):
            raise DesktopControlError(
                "DESKTOP_COORDINATE_INVALID", "Target coordinates are outside the screenshot."
            )

    @staticmethod
    def _clean_label(expected_label: str) -> str:
        if not isinstance(expected_label, str) or not 1 <= len(
            expected_label.strip()
        ) <= 120:
            raise DesktopControlError(
                "DESKTOP_TARGET_INVALID", "Name the exact visible control for approval."
            )
        requested_label = expected_label.strip()
        label = sanitize_untrusted_text(
            requested_label, UntrustedSource.BROWSER_PAGE, max_chars=120,
        ).text
        if label != requested_label or any(ord(char) < 32 for char in label):
            raise DesktopControlError(
                "DESKTOP_TARGET_INVALID", "Use a plain visible control label."
            )
        return label

    def _check_action_response(
        self, selection: WindowSelection, capture: WindowCapture,
        response: dict[str, Any], allowed_outcomes: set[str],
    ) -> tuple[int, str, str, str]:
        window_id, bundle_id, process_id, title = self._window_metadata(response)
        if (
            window_id != selection.window_id
            or bundle_id != selection.bundle_id
            or process_id != selection.process_id
            or title != capture.title
        ):
            raise DesktopControlError(
                "DESKTOP_WINDOW_CHANGED", "Selected window identity changed during action."
            )
        if response.get("outcome") not in allowed_outcomes:
            raise DesktopControlError(
                "DESKTOP_NATIVE_RESPONSE_INVALID", "Native action result is invalid."
            )
        return window_id, bundle_id, title, response["outcome"]

    async def press_window_control(
        self,
        project_id: str,
        run_id: str,
        selection_id: str,
        capture_receipt_id: str,
        x: int,
        y: int,
        expected_label: str,
    ) -> dict[str, Any]:
        async with self._lock:
            selection, capture = self._require_capture(
                project_id, run_id, selection_id, capture_receipt_id,
            )
            self._check_coordinate(capture, x, y)
            label = self._clean_label(expected_label)
            # Once a native request has been sent, its result may be unknown if
            # the pipe closes. Force fresh observation before any retry.
            self._capture = None
            response = await self.bridge.request(
                "press", run_id=run_id,
                arguments={
                    "capture_token": capture.native_token,
                    "x": x,
                    "y": y,
                    "width": capture.width,
                    "height": capture.height,
                    "expected_label": label,
                },
                timeout_seconds=20,
            )
            window_id, bundle_id, title, outcome = self._check_action_response(
                selection, capture, response, {"confirmed", "uncertain"},
            )
            ax_error_code = response.get("ax_error_code")
            actual_label = response.get("control_label")
            control_role = response.get("control_role")
            if type(ax_error_code) is not int or (
                outcome == "confirmed" and ax_error_code != 0
            ) or (
                not isinstance(actual_label, str) or len(actual_label) > 120
                or actual_label != label
                or not isinstance(control_role, str) or len(control_role) > 80
            ):
                raise DesktopControlError(
                    "DESKTOP_NATIVE_RESPONSE_INVALID", "Native action result is invalid."
                )
            safe_actual_label = sanitize_untrusted_text(
                actual_label, UntrustedSource.BROWSER_PAGE, max_chars=120,
            ).text
            safe_role = sanitize_untrusted_text(
                control_role, UntrustedSource.BROWSER_PAGE, max_chars=80,
            ).text
            receipt = await asyncio.to_thread(
                self._record_action,
                project_id, run_id, selection, capture, title,
                TOOL_PRESS_DESKTOP, "desktop_accessibility_press", outcome,
                {
                    "x": x, "y": y,
                    "expected_label": label,
                    "control_label": safe_actual_label,
                    "control_role": safe_role,
                    "ax_error_code": ax_error_code,
                },
            )
            return {
                "ok": outcome == "confirmed",
                "outcome": outcome,
                "selection_id": selection_id,
                "window_id": window_id,
                "bundle_id": bundle_id,
                "title": title,
                "control_label": safe_actual_label,
                "control_role": safe_role,
                "action_receipt_id": receipt.id,
                "next_step": "Capture a fresh screenshot to verify the resulting window state.",
            }

    async def replace_window_text(
        self,
        project_id: str,
        run_id: str,
        selection_id: str,
        capture_receipt_id: str,
        x: int,
        y: int,
        expected_label: str,
        expected_current_text: str,
        replacement_text: str,
    ) -> dict[str, Any]:
        async with self._lock:
            selection, capture = self._require_capture(
                project_id, run_id, selection_id, capture_receipt_id,
            )
            self._check_coordinate(capture, x, y)
            label = self._clean_label(expected_label)
            if not isinstance(expected_current_text, str) or not isinstance(
                replacement_text, str
            ):
                raise DesktopControlError(
                    "DESKTOP_TEXT_INVALID", "Expected and replacement text must be strings."
                )
            try:
                prior = expected_current_text.encode("utf-8")
                replacement = replacement_text.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise DesktopControlError(
                    "DESKTOP_TEXT_INVALID", "Text must be valid UTF-8."
                ) from exc
            if (
                len(prior) > _MAX_TEXT_BYTES or len(replacement) > _MAX_TEXT_BYTES
                or len(prior) + len(replacement) > _MAX_TEXT_REQUEST_BYTES
                or "\x00" in expected_current_text or "\x00" in replacement_text
                or any(
                    ord(char) < 32 and char not in "\n\r\t"
                    for char in expected_current_text + replacement_text
                )
            ):
                raise DesktopControlError(
                    "DESKTOP_TEXT_INVALID", "Text exceeds the native action limit."
                )
            self._capture = None
            response = await self.bridge.request(
                "replace_text", run_id=run_id,
                arguments={
                    "capture_token": capture.native_token,
                    "x": x,
                    "y": y,
                    "width": capture.width,
                    "height": capture.height,
                    "expected_label": label,
                    "expected_current_text": expected_current_text,
                    "replacement_text": replacement_text,
                },
                timeout_seconds=20,
            )
            window_id, bundle_id, title, outcome = self._check_action_response(
                selection, capture, response, {"confirmed", "uncertain"},
            )
            actual_label = response.get("control_label")
            role = response.get("control_role")
            ax_error_code = response.get("ax_error_code")
            if (
                actual_label != label or role not in {"AXTextField", "AXTextArea"}
                or type(ax_error_code) is not int
                or (outcome == "confirmed" and ax_error_code != 0)
            ):
                raise DesktopControlError(
                    "DESKTOP_NATIVE_RESPONSE_INVALID", "Native text result is invalid."
                )
            receipt = await asyncio.to_thread(
                self._record_action,
                project_id, run_id, selection, capture, title,
                TOOL_REPLACE_DESKTOP_TEXT, "desktop_accessibility_text", outcome,
                {
                    "x": x, "y": y,
                    "expected_label": label,
                    "control_label": actual_label,
                    "control_role": role,
                    "expected_current_sha256": hashlib.sha256(prior).hexdigest(),
                    "expected_current_bytes": len(prior),
                    "replacement_sha256": hashlib.sha256(replacement).hexdigest(),
                    "replacement_bytes": len(replacement),
                    "ax_error_code": ax_error_code,
                },
            )
            return {
                "ok": outcome == "confirmed",
                "outcome": outcome,
                "selection_id": selection_id,
                "window_id": window_id,
                "bundle_id": bundle_id,
                "title": title,
                "control_label": actual_label,
                "control_role": role,
                "replacement_bytes": len(replacement),
                "action_receipt_id": receipt.id,
                "next_step": "Capture a fresh screenshot to verify the resulting window state.",
            }

    async def send_window_key(
        self,
        project_id: str,
        run_id: str,
        selection_id: str,
        capture_receipt_id: str,
        expected_window_title: str,
        key: str,
    ) -> dict[str, Any]:
        async with self._lock:
            selection, capture = self._require_capture(
                project_id, run_id, selection_id, capture_receipt_id,
            )
            if not capture.title or expected_window_title != capture.title:
                raise DesktopControlError(
                    "DESKTOP_TARGET_INVALID", "Use the exact title from the latest screenshot."
                )
            if not isinstance(key, str) or key not in _ALLOWED_KEYS:
                raise DesktopControlError(
                    "DESKTOP_KEY_INVALID", "Use one supported unmodified key."
                )
            self._capture = None
            response = await self.bridge.request(
                "key", run_id=run_id,
                arguments={
                    "capture_token": capture.native_token,
                    "width": capture.width,
                    "height": capture.height,
                    "expected_window_title": capture.title,
                    "key": key,
                },
                timeout_seconds=20,
            )
            window_id, bundle_id, title, outcome = self._check_action_response(
                selection, capture, response, {"dispatched"},
            )
            if response.get("key") != key:
                raise DesktopControlError(
                    "DESKTOP_NATIVE_RESPONSE_INVALID", "Native key result is invalid."
                )
            receipt = await asyncio.to_thread(
                self._record_action,
                project_id, run_id, selection, capture, title,
                TOOL_SEND_DESKTOP_KEY, "desktop_keyboard_event", outcome,
                {"key": key},
            )
            return {
                "ok": True,
                "outcome": outcome,
                "selection_id": selection_id,
                "window_id": window_id,
                "bundle_id": bundle_id,
                "title": title,
                "key": key,
                "action_receipt_id": receipt.id,
                "next_step": "Capture a fresh screenshot to verify the resulting window state.",
            }

    def _record_action(
        self,
        project_id: str,
        run_id: str,
        selection: WindowSelection,
        capture: WindowCapture,
        title: str,
        tool: str,
        object_type: str,
        outcome: str,
        action_payload: dict[str, Any],
    ) -> ToolReceipt:
        # Action receipt persists even if the Run ends or policy is revoked
        # while the native AX call is in flight. The pre-action checks above
        # decide authority; the post-action write records what already happened.
        stamp = datetime.now(UTC)
        canonical = (
            f"ophira-desktop://{selection.bundle_id.casefold()}"
            f"/window/{selection.window_id}"
        )
        return self.ledger.issue_tool_receipt(
            project_id, run_id,
            tool=tool,
            source_type=EvidenceSourceType.TOOL,
            canonical_source=canonical,
            payload={
                "bundle_id": selection.bundle_id,
                "window_id": selection.window_id,
                "process_id": selection.process_id,
                "title": title,
                "capture_receipt_id": capture.receipt_id,
                **action_payload,
                "outcome": outcome,
                "observed_at": stamp.isoformat(),
            },
            object_type=object_type,
            object_key=f"{canonical}/{object_type}/{uuid4().hex}",
            object_metadata={
                "capture_receipt_id": capture.receipt_id,
                "outcome": outcome,
                "observed_at": stamp.isoformat(),
            },
        )

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
