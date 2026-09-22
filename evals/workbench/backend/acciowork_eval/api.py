"""Local API for the independently maintained Open-Acciowork evaluation workbench."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .config import database_path
from .contract import JUDGES
from .store import Conflict, Store


@lru_cache
def get_store():
    return Store(database_path())


def local_request(request: Request):
    # Archives and human labels are a local owner tool.
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "Open-Acciowork archives are available only on loopback")
    host = urlsplit("http://" + request.headers.get("host", "")).hostname
    if host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
        raise HTTPException(403, "A loopback Host is required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("x-eval-review") != "owner-ui":
            raise HTTPException(403, "Use the local review interface")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site reviews are not allowed")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "A local review origin is required")


router = APIRouter(prefix="/api/acciowork", dependencies=[Depends(local_request)])


class LabelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: StrictBool
    rationale: str = Field(min_length=1, max_length=10000)
    revision: int = Field(ge=0)
    owner_confirmed: StrictBool


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    open_codes: list[str] = Field(max_length=30)
    notes: str = Field(max_length=20000)
    revision: int = Field(ge=0)


def invoke(call):
    try:
        return call()
    except KeyError as exc:
        raise HTTPException(404, "Archive record not found") from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/experiments")
def experiments(store: Store = Depends(get_store)):
    return store.experiments()


@router.get("/traces")
def traces(experiment_id: str | None = None, store: Store = Depends(get_store)):
    return store.traces(experiment_id)


@router.get("/traces/{trace_id}")
def trace(trace_id: str, store: Store = Depends(get_store)):
    return invoke(lambda: store.trace(trace_id))


@router.get("/traces/{trace_id}/events")
def events(
    trace_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    kind: str = "key",
    q: str = Query("", max_length=300),
    sequence: int | None = Query(None, ge=1),
    store: Store = Depends(get_store),
):
    return invoke(
        lambda: store.events(
            trace_id, offset=offset, limit=limit, kind=kind, query=q, sequence=sequence
        )
    )


@router.get("/review")
def review_queue(store: Store = Depends(get_store)):
    return {"judges": JUDGES, "items": store.items()}


@router.put("/items/{item_id}/label")
def label(item_id: str, body: LabelInput, store: Store = Depends(get_store)):
    return invoke(lambda: store.save_label(item_id, **body.model_dump()))


@router.put("/traces/{trace_id}/review")
def review(trace_id: str, body: ReviewInput, store: Store = Depends(get_store)):
    return invoke(lambda: store.save_review(trace_id, **body.model_dump()))


@router.get("/labels/{judge}/export")
def export(judge: str, store: Store = Depends(get_store)):
    content = invoke(lambda: store.export_labels(judge))
    return Response(
        content,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{judge}-labels.jsonl"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/files/{sha}")
def raw_file(sha: str, store: Store = Depends(get_store)):
    content = invoke(lambda: store.file(sha))
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="archive-source"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/compare")
def compare(left: str, right: str, store: Store = Depends(get_store)):
    return invoke(lambda: store.compare(left, right))


app = FastAPI(title="Open-Acciowork evaluation archives")
app.include_router(router)
