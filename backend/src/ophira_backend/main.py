"""HTTP surface: projects, turns, the live event stream, approvals, files, feedback.

Sign-in is not here. The deployment's gateway (deploy/shared-box) decides who may
reach /api; the product itself has one operator and no user system (textbook p.66:
先拿到用户).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .agent import AgentRunner, ClientFactory, RunBusyError
from .gateway import ModelGateway, gateway_router
from .settings import Settings, get_settings
from .store import NotFoundError, Store, require_git

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def sse(event: dict[str, Any]) -> dict[str, str]:
    return {"id": str(event["seq"]), "data": json.dumps(event, ensure_ascii=False)}


class NewProject(BaseModel):
    name: str = Field(default="", max_length=200)


class NewRun(BaseModel):
    prompt: str = Field(min_length=1, max_length=50_000)


class Decision(BaseModel):
    allow: bool
    message: str | None = Field(default=None, max_length=5_000)
    answers: dict[str, Any] | None = None
    response: str | None = Field(default=None, max_length=10_000)


class Feedback(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    run_id: str | None = None


def create_app(settings: Settings | None = None,
               client_factory: ClientFactory | None = None) -> FastAPI:
    settings = settings or get_settings()
    require_git()
    store = Store(settings.data_dir)
    gateway = ModelGateway(settings)
    runner = AgentRunner(settings, store, gateway, settings.self_url,
                         **({"client_factory": client_factory} if client_factory else {}))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        for run_id in list(runner.live):
            await runner.cancel(run_id)
        await runner.wait_idle()
        await gateway.aclose()

    app = FastAPI(title="TradeMind", lifespan=lifespan)
    app.state.store, app.state.runner, app.state.gateway = store, runner, gateway
    app.include_router(gateway_router(gateway))

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # A browser on another site must not drive this API (cross-site form posts
        # need no CORS preflight). Requests without an Origin are not from a page.
        origin = request.headers.get("origin")
        if (origin and urlsplit(origin).netloc != request.headers.get("host")
                and origin.rstrip("/") not in settings.allowed_origins):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    def not_found(exc: NotFoundError) -> HTTPException:
        return HTTPException(status_code=404, detail=f"not found: {exc}")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "model": settings.model or "default"}

    @app.get("/api/projects")
    async def list_projects() -> list[dict[str, Any]]:
        return store.list_projects()

    @app.post("/api/projects", status_code=201)
    async def create_project(body: NewProject) -> dict[str, Any]:
        return await asyncio.to_thread(store.create_project, body.name)

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        try:
            project = store.get_project(project_id)
            runs = store.list_runs(project_id)
        except NotFoundError as exc:
            raise not_found(exc) from exc
        active = next((r.run["id"] for r in runner.live.values()
                       if r.run["project_id"] == project_id), None)
        return {**project, "runs": runs, "active_run_id": active}

    @app.post("/api/projects/{project_id}/runs", status_code=201)
    async def start_run(project_id: str, body: NewRun) -> dict[str, Any]:
        try:
            return await runner.start(project_id, body.prompt)
        except NotFoundError as exc:
            raise not_found(exc) from exc
        except RunBusyError as exc:
            raise HTTPException(status_code=409, detail="上一轮还在进行中") from exc

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> EventSourceResponse:
        try:
            run = store.find_run(run_id)
        except NotFoundError as exc:
            raise not_found(exc) from exc
        after = int(request.headers.get("last-event-id") or request.query_params.get("after")
                    or 0)

        async def stream() -> AsyncIterator[dict[str, Any]]:
            live = runner.live.get(run_id)
            if live is None:
                for event in store.read_events(run["project_id"], run_id):
                    if event["seq"] > after:
                        yield sse(event)
                return
            seen = after
            while True:
                async with live.changed:
                    await live.changed.wait_for(
                        lambda seen=seen: len(live.events) > seen or live.done)
                    batch = live.events[seen:]
                for event in batch:
                    yield sse(event)
                seen += len(batch)
                if live.done and seen >= len(live.events):
                    return

        return EventSourceResponse(stream(), ping=15)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, bool]:
        return {"cancelled": await runner.cancel(run_id)}

    @app.post("/api/runs/{run_id}/permissions/{request_id}")
    async def decide(run_id: str, request_id: str, body: Decision) -> dict[str, bool]:
        if not runner.answer(run_id, request_id, body.model_dump(exclude_none=True)):
            raise HTTPException(status_code=404, detail="这个请求已经结束了")
        return {"ok": True}

    @app.get("/api/projects/{project_id}/files")
    async def list_files(project_id: str) -> list[dict[str, Any]]:
        try:
            return store.list_files(project_id)
        except NotFoundError as exc:
            raise not_found(exc) from exc

    @app.get("/api/projects/{project_id}/files/{path:path}")
    async def download(project_id: str, path: str) -> FileResponse:
        try:
            target = store.resolve_file(project_id, path)
        except NotFoundError as exc:
            raise not_found(exc) from exc
        return FileResponse(target, filename=target.name)

    @app.post("/api/projects/{project_id}/files", status_code=201)
    async def upload(project_id: str,
                     file: Annotated[UploadFile, File()]) -> dict[str, list[str]]:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过 50MB")
        try:
            saved = await asyncio.to_thread(store.save_upload, project_id,
                                            file.filename or "file", data)
        except NotFoundError as exc:
            raise not_found(exc) from exc
        return {"saved": saved}

    @app.post("/api/projects/{project_id}/feedback", status_code=201)
    async def feedback(project_id: str, body: Feedback) -> dict[str, Any]:
        try:
            return store.add_feedback(project_id, body.run_id, body.text)
        except NotFoundError as exc:
            raise not_found(exc) from exc

    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn ophira_backend.main:app` builds the app on first access, so importing
    # this module in tests does not read the environment or touch the data folder.
    if name == "app":
        app = create_app()
        globals()["app"] = app
        return app
    raise AttributeError(name)
