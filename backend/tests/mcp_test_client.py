"""Exercise SDK in-process MCP servers through the public protocol client."""

from typing import Any

from mcp import Client
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, ListToolsResult


async def call_sdk_tool(
    server: Server, name: str, arguments: dict[str, Any]
) -> CallToolResult:
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


async def list_sdk_tools(server: Server) -> ListToolsResult:
    async with Client(server) as client:
        return await client.list_tools()
