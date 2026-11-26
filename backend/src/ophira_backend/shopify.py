"""The person's Shopify store as one tool: the Agent writes Admin GraphQL, the host signs it.

Textbook p.31: MCP exists for talking to another system. p.94: give the Agent the query
language (SQL there, GraphQL here) instead of one tool per question. The client secret
and the access token stay in this process; the Agent only sees what Shopify answered.

Whether a call may change the store is not decided here: agent.py's PreToolUse hook sends
every mutation to the person before it runs.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from .settings import Settings

SERVER_NAME = "shopify"
TOOL_NAME = f"mcp__{SERVER_NAME}__admin_graphql"
_REFRESH_EARLY_SECONDS = 60


class ShopifyError(RuntimeError):
    pass


def is_mutation(document: str) -> bool:
    """True if the GraphQL document can change the store. Comments and strings don't count;
    anything that does not parse as a plain query is treated as a change (ask the person)."""
    without_strings = re.sub(r'"""[\s\S]*?"""|"(?:[^"\\]|\\.)*"', '""', document)
    without_comments = re.sub(r"#[^\n]*", "", without_strings)
    return re.search(r"\b(mutation|subscription)\b", without_comments) is not None


class ShopifyClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.store = settings.shopify_store.removesuffix(".myshopify.com")
        self.base = f"https://{self.store}.myshopify.com"
        self.version = settings.shopify_api_version
        self._client_id = settings.shopify_client_id
        self._secret = settings.shopify_client_secret
        self._token: str | None = None
        self._expires_at = 0.0
        self._http = httpx.AsyncClient(transport=transport, timeout=30, follow_redirects=False)

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - _REFRESH_EARLY_SECONDS:
            return self._token
        response = await self._http.post(
            f"{self.base}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": self._client_id,
                  "client_secret": self._secret.get_secret_value()},
        )
        if response.status_code != 200:
            raise ShopifyError(f"Shopify refused the app credentials (HTTP {response.status_code})")
        grant = response.json()
        self._token = grant["access_token"]
        self._expires_at = time.monotonic() + float(grant.get("expires_in", 0))
        return self._token

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        for attempt in (1, 2):
            response = await self._http.post(
                f"{self.base}/admin/api/{self.version}/graphql.json",
                json={"query": query, "variables": variables or {}},
                headers={"X-Shopify-Access-Token": await self._access_token()},
            )
            if response.status_code == 401 and attempt == 1:
                self._token = None  # expired or revoked: get a fresh one once
                continue
            if response.status_code != 200:
                raise ShopifyError(f"Shopify answered HTTP {response.status_code}")
            return response.json()
        raise ShopifyError("Shopify rejected a fresh access token")

    def server(self):
        @tool(
            "admin_graphql",
            f"Read or change the user's Shopify store ({self.store}.myshopify.com) through the "
            f"Admin GraphQL API {self.version}. `query` is a complete GraphQL document, "
            "`variables` its variables. `description` says in one plain sentence, in the "
            "user's language, what this call is for; the user sees it when a change needs "
            "their OK. Every mutation waits for the user's OK. Returns Shopify's JSON as is "
            "(data, errors, and cost in extensions). Shopify protects customer data (names, "
            "emails, addresses, phones): don't query it unless the user asks for it.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "variables": {"type": "object"},
                    "description": {"type": "string"},
                },
                "required": ["query", "description"],
            },
        )
        async def admin_graphql(args: dict[str, Any]) -> dict[str, Any]:
            try:
                result = await self.graphql(args["query"], args.get("variables"))
            except (ShopifyError, httpx.HTTPError) as exc:
                return {"content": [{"type": "text", "text": f"Shopify call failed: {exc}"}],
                        "is_error": True}
            return {"content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False)}]}

        return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=[admin_graphql])

    async def aclose(self) -> None:
        await self._http.aclose()
