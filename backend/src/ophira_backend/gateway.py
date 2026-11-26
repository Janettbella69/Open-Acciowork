"""Model gateway: the Agent calls the model through here and never holds the key.

Textbook p.20 names this as legitimate infrastructure ("在SDK外面加一层proxy，不改SDK"),
and the SDK's secure-deployment guide recommends the same shape: credentials are
injected outside the agent's boundary. The CLI gets a per-process gateway token;
Bash inside the sandbox can print its environment and find nothing worth stealing.

The route lives outside /api, so the public site proxy never forwards to it;
only processes on this machine can reach it.
"""

from __future__ import annotations

import secrets

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .settings import Settings

GATEWAY_PATH = "/_model"
ANTHROPIC_API = "https://api.anthropic.com"
_HOP_HEADERS = {"host", "content-length", "connection", "authorization", "x-api-key",
                "accept-encoding", "transfer-encoding"}


class ModelGateway:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.token = secrets.token_urlsafe(32)
        self._client = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(30.0, read=None)
        )

    @property
    def upstream(self) -> str:
        return (self.settings.model_base_url or ANTHROPIC_API).rstrip("/")

    def authorized(self, request: Request) -> bool:
        offered = request.headers.get("x-api-key") or request.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ")
        return secrets.compare_digest(offered, self.token)

    def credential_headers(self) -> dict[str, str]:
        key = self.settings.model_auth_token.get_secret_value()
        if not key:
            return {}
        if self.settings.model_auth_env == "ANTHROPIC_API_KEY":
            return {"x-api-key": key}
        return {"authorization": f"Bearer {key}"}

    async def forward(self, request: Request, path: str) -> StreamingResponse:
        if not self.authorized(request):
            raise HTTPException(status_code=401, detail="gateway token required")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        headers.update(self.credential_headers())
        upstream = self._client.build_request(
            request.method,
            f"{self.upstream}/{path}",
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
        response = await self._client.send(upstream, stream=True)
        passthrough = {k: v for k, v in response.headers.items()
                       if k.lower() not in _HOP_HEADERS | {"content-encoding"}}
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=passthrough,
            background=BackgroundTask(response.aclose),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def gateway_router(gateway: ModelGateway) -> APIRouter:
    router = APIRouter()

    @router.api_route(f"{GATEWAY_PATH}/{{path:path}}", methods=["GET", "POST"],
                      include_in_schema=False)
    async def model_gateway(path: str, request: Request) -> StreamingResponse:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=404)
        return await gateway.forward(request, path)

    return router
