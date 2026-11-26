"""The model gateway adds the real key on the way out; the Agent only has a token."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from ophira_backend.gateway import ModelGateway, gateway_router
from ophira_backend.settings import Settings


class _SseStream(httpx.AsyncByteStream):
    """Like a real model endpoint: the body arrives as a stream, chunk by chunk."""

    async def __aiter__(self):
        yield b"event: message_start\n"
        yield b"data: {}\n\n"


def gateway_app(seen: list[httpx.Request], auth_env: str = "ANTHROPIC_AUTH_TOKEN"):
    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=_SseStream(),
                              headers={"content-type": "text/event-stream"})

    settings = Settings(model_base_url="https://api.example-model.cn/anthropic",
                        model_auth_token=SecretStr("sk-real"), model_auth_env=auth_env)
    gateway = ModelGateway(settings, transport=httpx.MockTransport(upstream))
    app = FastAPI()
    app.include_router(gateway_router(gateway))
    return app, gateway


@pytest.mark.parametrize("auth_env,header,value", [
    ("ANTHROPIC_AUTH_TOKEN", "authorization", "Bearer sk-real"),
    ("ANTHROPIC_API_KEY", "x-api-key", "sk-real"),
])
async def test_the_real_key_is_added_upstream(auth_env, header, value):
    seen: list[httpx.Request] = []
    app, gateway = gateway_app(seen, auth_env)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)),
                                 base_url="http://127.0.0.1:8000") as http:
        response = await http.post("/_model/v1/messages?beta=true", json={"stream": True},
                                   headers={"authorization": f"Bearer {gateway.token}",
                                            "anthropic-version": "2023-06-01"})
    assert response.status_code == 200
    assert response.text.startswith("event: message_start")
    forwarded = seen[0]
    assert str(forwarded.url) == "https://api.example-model.cn/anthropic/v1/messages?beta=true"
    assert forwarded.headers[header] == value
    assert forwarded.headers["anthropic-version"] == "2023-06-01"
    assert gateway.token not in str(forwarded.headers)


async def test_without_the_token_nothing_is_forwarded():
    seen: list[httpx.Request] = []
    app, _ = gateway_app(seen)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)),
                                 base_url="http://127.0.0.1:8000") as http:
        response = await http.post("/_model/v1/messages", headers={"x-api-key": "guess"})
    assert response.status_code == 401 and seen == []


async def test_only_this_machine_can_reach_it():
    seen: list[httpx.Request] = []
    app, gateway = gateway_app(seen)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 5)),
                                 base_url="http://example.com") as http:
        response = await http.post("/_model/v1/messages",
                                   headers={"authorization": f"Bearer {gateway.token}"})
    assert response.status_code == 404 and seen == []
