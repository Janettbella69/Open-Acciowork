"""In-process MCP tools for the approved native desktop host."""

from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .constants import DESKTOP_MCP_SERVER_NAME
from .desktop_bridge import DesktopBridgeError
from .desktop_control import DesktopControl, DesktopControlError
from .untrusted import UNTRUSTED_DATA_NOTICE

logger = logging.getLogger(__name__)


def _result(value: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "is_error": is_error,
    }


def build_desktop_tools(service: DesktopControl, project_id: str, run_id: str):
    @tool(
        "select_window",
        "Open the macOS system picker for one native app window in this attended Run. "
        "The user chooses the window. Browser apps use the URL-guarded L1/L2 tools. "
        "Every invocation requires human approval and organization allowlist access.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def select_window(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            selection = await service.select_window(project_id, run_id)
            return _result({"ok": True, "note": UNTRUSTED_DATA_NOTICE, **selection})
        except DesktopControlError as exc:
            return _result(
                {"ok": False, "error_code": exc.code, "error": str(exc)}, is_error=True,
            )
        except DesktopBridgeError:
            return _result({
                "ok": False, "error_code": "DESKTOP_HOST_UNAVAILABLE",
                "error": "Native desktop host could not complete window selection.",
            }, is_error=True)
        except Exception:
            logger.exception("Desktop window selection failed")
            return _result({
                "ok": False, "error_code": "DESKTOP_SELECTION_FAILED",
                "error": "Desktop window selection could not be recorded.",
            }, is_error=True)

    @tool(
        "snapshot_window",
        "Capture the previously user-selected native window as a source-linked screenshot. "
        "Use the exact selection_id from select_window. Every capture requires human approval. "
        "The screenshot becomes an Artifact, Evidence, and a Run-bound source receipt. "
        "Read the returned screenshot_path image before choosing any press coordinates.",
        {
            "type": "object",
            "properties": {
                "selection_id": {
                    "type": "string", "pattern": "^window_[a-f0-9]{32}$",
                },
            },
            "required": ["selection_id"],
            "additionalProperties": False,
        },
    )
    async def snapshot_window(args: dict[str, Any]) -> dict[str, Any]:
        try:
            capture = await service.snapshot(
                project_id, run_id, args["selection_id"],
            )
            return _result({"note": UNTRUSTED_DATA_NOTICE, **capture})
        except DesktopControlError as exc:
            return _result(
                {"ok": False, "error_code": exc.code, "error": str(exc)}, is_error=True,
            )
        except DesktopBridgeError:
            return _result({
                "ok": False, "error_code": "DESKTOP_HOST_UNAVAILABLE",
                "error": "Native desktop host could not capture the selected window.",
            }, is_error=True)
        except Exception:
            logger.exception("Desktop window capture failed")
            return _result({
                "ok": False, "error_code": "DESKTOP_CAPTURE_FAILED",
                "error": "Desktop window capture could not be recorded.",
            }, is_error=True)

    @tool(
        "press_window_control",
        "Press an accessible control inside the user-selected native window at a point "
        "from its most recent screenshot. Only controls that expose macOS AXPress are supported. "
        "Use the exact screenshot receipt and pixel coordinates, plus the control's "
        "exact visible label for human approval. Each press needs separate approval; "
        "read the screenshot image first and capture again afterward. "
        "An uncertain outcome must never be retried without a fresh observation.",
        {
            "type": "object",
            "properties": {
                "selection_id": {"type": "string", "pattern": "^window_[a-f0-9]{32}$"},
                "capture_receipt_id": {
                    "type": "string", "pattern": "^receipt_[A-Za-z0-9_-]{20,}$",
                },
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "expected_label": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            "required": [
                "selection_id", "capture_receipt_id", "x", "y", "expected_label",
            ],
            "additionalProperties": False,
        },
    )
    async def press_window_control(args: dict[str, Any]) -> dict[str, Any]:
        try:
            action = await service.press_window_control(
                project_id, run_id,
                args["selection_id"], args["capture_receipt_id"],
                args["x"], args["y"], args["expected_label"],
            )
            return _result(
                {"note": UNTRUSTED_DATA_NOTICE, **action}, is_error=not action["ok"],
            )
        except DesktopControlError as exc:
            uncertain = exc.code in {
                "DESKTOP_WINDOW_CHANGED", "DESKTOP_NATIVE_RESPONSE_INVALID",
            }
            return _result({
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "outcome": "uncertain" if uncertain else "not_sent",
                "next_step": "Capture a fresh screenshot before any retry." if uncertain else None,
            }, is_error=True)
        except DesktopBridgeError:
            return _result({
                "ok": False,
                "error_code": "DESKTOP_ACTION_UNCERTAIN",
                "error": "The native host did not confirm whether the press occurred.",
                "outcome": "uncertain",
                "next_step": "Capture a fresh screenshot before any retry.",
            }, is_error=True)
        except Exception:
            logger.exception("Desktop press result could not be recorded")
            return _result({
                "ok": False,
                "error_code": "DESKTOP_ACTION_UNCERTAIN",
                "error": "The press may have occurred, but its result could not be recorded.",
                "outcome": "uncertain",
                "next_step": "Capture a fresh screenshot before any retry.",
            }, is_error=True)

    @tool(
        "replace_window_text",
        "Replace the complete value of a labelled, non-secret native text field or text area. "
        "Read the latest screenshot first, then supply its receipt, pixel target, exact "
        "visible label, the full current value, and replacement text. The native host "
        "rejects a changed value or secure field. Every call needs human approval; "
        "capture again afterward and never blindly retry an uncertain result.",
        {
            "type": "object",
            "properties": {
                "selection_id": {"type": "string", "pattern": "^window_[a-f0-9]{32}$"},
                "capture_receipt_id": {
                    "type": "string", "pattern": "^receipt_[A-Za-z0-9_-]{20,}$",
                },
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "expected_label": {"type": "string", "minLength": 1, "maxLength": 120},
                "expected_current_text": {"type": "string", "maxLength": 4096},
                "replacement_text": {"type": "string", "maxLength": 4096},
            },
            "required": [
                "selection_id", "capture_receipt_id", "x", "y", "expected_label",
                "expected_current_text", "replacement_text",
            ],
            "additionalProperties": False,
        },
    )
    async def replace_window_text(args: dict[str, Any]) -> dict[str, Any]:
        try:
            action = await service.replace_window_text(
                project_id, run_id,
                args["selection_id"], args["capture_receipt_id"],
                args["x"], args["y"], args["expected_label"],
                args["expected_current_text"], args["replacement_text"],
            )
            return _result(
                {"note": UNTRUSTED_DATA_NOTICE, **action}, is_error=not action["ok"],
            )
        except DesktopControlError as exc:
            uncertain = exc.code in {
                "DESKTOP_WINDOW_CHANGED", "DESKTOP_NATIVE_RESPONSE_INVALID",
            }
            return _result({
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "outcome": "uncertain" if uncertain else "not_sent",
                "next_step": "Capture a fresh screenshot before any retry." if uncertain else None,
            }, is_error=True)
        except DesktopBridgeError:
            return _result({
                "ok": False,
                "error_code": "DESKTOP_ACTION_UNCERTAIN",
                "error": "The native host did not confirm whether the text changed.",
                "outcome": "uncertain",
                "next_step": "Capture a fresh screenshot before any retry.",
            }, is_error=True)
        except Exception:
            logger.exception("Desktop text result could not be recorded")
            return _result({
                "ok": False,
                "error_code": "DESKTOP_ACTION_UNCERTAIN",
                "error": "Text may have changed, but its result could not be recorded.",
                "outcome": "uncertain",
                "next_step": "Capture a fresh screenshot before any retry.",
            }, is_error=True)

    @tool(
        "send_window_key",
        "Send one unmodified key to the user-selected native app's focused window. "
        "The latest screenshot receipt and exact window title are required. Supported keys "
        "are Return, Escape, Tab, Space, Backspace, and the four arrows. The host checks "
        "the focused window before dispatch. Every call needs human approval and a fresh "
        "screenshot afterward; dispatch does not prove the UI changed.",
        {
            "type": "object",
            "properties": {
                "selection_id": {"type": "string", "pattern": "^window_[a-f0-9]{32}$"},
                "capture_receipt_id": {
                    "type": "string", "pattern": "^receipt_[A-Za-z0-9_-]{20,}$",
                },
                "expected_window_title": {"type": "string", "minLength": 1, "maxLength": 256},
                "key": {
                    "type": "string",
                    "enum": [
                        "Return", "Escape", "Tab", "Space", "Backspace",
                        "Up", "Down", "Left", "Right",
                    ],
                },
            },
            "required": [
                "selection_id", "capture_receipt_id", "expected_window_title", "key",
            ],
            "additionalProperties": False,
        },
    )
    async def send_window_key(args: dict[str, Any]) -> dict[str, Any]:
        try:
            action = await service.send_window_key(
                project_id, run_id,
                args["selection_id"], args["capture_receipt_id"],
                args["expected_window_title"], args["key"],
            )
            return _result({"note": UNTRUSTED_DATA_NOTICE, **action})
        except DesktopControlError as exc:
            uncertain = exc.code in {
                "DESKTOP_WINDOW_CHANGED", "DESKTOP_NATIVE_RESPONSE_INVALID",
            }
            return _result({
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "outcome": "uncertain" if uncertain else "not_sent",
                "next_step": "Capture a fresh screenshot before any retry." if uncertain else None,
            }, is_error=True)
        except DesktopBridgeError:
            return _result({
                "ok": False,
                "error_code": "DESKTOP_ACTION_UNCERTAIN",
                "error": "The native host did not confirm whether the key was dispatched.",
                "outcome": "uncertain",
                "next_step": "Capture a fresh screenshot before any retry.",
            }, is_error=True)
        except Exception:
            logger.exception("Desktop key result could not be recorded")
            return _result({
                "ok": False,
                "error_code": "DESKTOP_ACTION_UNCERTAIN",
                "error": "The key may have been dispatched, but its result could not be recorded.",
                "outcome": "uncertain",
                "next_step": "Capture a fresh screenshot before any retry.",
            }, is_error=True)

    return [
        select_window, snapshot_window, press_window_control,
        replace_window_text, send_window_key,
    ]


def build_desktop_mcp_server(service: DesktopControl, project_id: str, run_id: str):
    return create_sdk_mcp_server(
        name=DESKTOP_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_desktop_tools(service, project_id, run_id),
    )
