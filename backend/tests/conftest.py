"""The model side is replaced by a scripted client; the host code around it is real."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from ophira_backend.main import create_app
from ophira_backend.settings import Settings


def result(session_id: str = "sess-1", *, is_error: bool = False,
           text: str = "完成") -> ResultMessage:
    return ResultMessage(subtype="error_during_execution" if is_error else "success",
                         duration_ms=10, duration_api_ms=5, is_error=is_error, num_turns=1,
                         session_id=session_id, result=text, total_cost_usd=0.0)


def say(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="fake-model")


Script = Callable[["FakeClient"], AsyncIterator[Any]]


class FakeClient:
    def __init__(self, options: Any, script: Script, record: list[FakeClient]) -> None:
        self.options = options
        self.script = script
        self.prompts: list[str] = []
        self.interrupted = asyncio.Event()
        record.append(self)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        async for message in self.script(self):
            yield message

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def pre_tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        return await hook({"tool_name": tool_name, "tool_input": tool_input}, "tu-1", None)


class Harness:
    def __init__(self, tmp_path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data", model="fake-model",
                                 model_base_url="http://upstream.test",
                                 cors_origins="https://trademind.store")
        self.clients: list[FakeClient] = []
        self.script: Script = default_script
        self.app = create_app(self.settings, client_factory=self._factory)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                      base_url="http://127.0.0.1:8000")

    def _factory(self, options: Any) -> FakeClient:
        return FakeClient(options, self.script, self.clients)

    @property
    def runner(self):
        return self.app.state.runner

    @property
    def store(self):
        return self.app.state.store

    async def project(self, name: str = "宠物用品出海") -> dict[str, Any]:
        response = await self.http.post("/api/projects", json={"name": name})
        assert response.status_code == 201
        return response.json()

    async def run_to_end(self, project_id: str, prompt: str = "帮我看看卖什么") -> dict[str, Any]:
        response = await self.http.post(f"/api/projects/{project_id}/runs", json={"prompt": prompt})
        assert response.status_code == 201, response.text
        await self.runner.wait_idle()
        return response.json()


async def default_script(client: FakeClient) -> AsyncIterator[Any]:
    yield say("好的")
    yield result()


@pytest.fixture
async def harness(tmp_path) -> AsyncIterator[Harness]:
    h = Harness(tmp_path)
    yield h
    await h.http.aclose()
