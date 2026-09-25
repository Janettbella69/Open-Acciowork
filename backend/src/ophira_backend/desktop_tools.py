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
        "The screenshot becomes an Artifact, Evidence, and a Run-bound source receipt.",
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

    return [select_window, snapshot_window]


def build_desktop_mcp_server(service: DesktopControl, project_id: str, run_id: str):
    return create_sdk_mcp_server(
        name=DESKTOP_MCP_SERVER_NAME,
        version="1.0.0",
        tools=build_desktop_tools(service, project_id, run_id),
    )
