"""Private package browsing, reviewed human imports, and exact version export."""

from typing import Annotated
from urllib.parse import quote
from zipfile import BadZipFile

from fastapi import FastAPI, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, AuthorizationError
from .skill_import_models import (
    IMPORT_REQUEST_ID,
    SkillImportReviewRequest,
    SkillRepositorySelection,
)
from .skill_repository import SkillRepository
from .skills import (
    MAX_SKILL_ARCHIVE_BYTES,
    SkillConflictError,
    SkillIdentifierError,
    SkillIntegrityError,
    SkillNotFoundError,
)
from .storage import LedgerNotFoundError


def register_skill_repository_routes(app: FastAPI, ledger, registry, settings, read_body):
    repository = SkillRepository(ledger, registry, settings)
    app.state.skill_repository = repository
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    def principal(request):
        return getattr(request.state, "principal", None)

    async def result(request, response, operation):
        response.headers.update(headers)
        try:
            value = await run_in_threadpool(operation)
            repository.authorize(principal(request))
            return value
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(
                403, "Private Skill repository access is unavailable", headers
            ) from exc
        except (LedgerNotFoundError, SkillNotFoundError) as exc:
            raise HTTPException(404, "Private Skill package or review not found", headers) from exc
        except SkillConflictError as exc:
            raise HTTPException(409, str(exc), headers) from exc
        except (SkillIntegrityError, OSError) as exc:
            raise HTTPException(
                409, "Private Skill storage could not be verified", headers
            ) from exc
        except (ValueError, SkillIdentifierError, BadZipFile) as exc:
            raise HTTPException(422, "Invalid private Skill package or request", headers) from exc

    @app.get("/api/skills/repository")
    async def capabilities(request: Request, response: Response):
        return await result(request, response, lambda: repository.capabilities(principal(request)))

    @app.post("/api/skills/install", status_code=201)
    async def submit(
        request: Request,
        response: Response,
        request_id: Annotated[str, Query(pattern=IMPORT_REQUEST_ID)],
        filename: Annotated[str, Query(min_length=1, max_length=160)] = "skill.zip",
    ):
        raw = await read_body(
            request,
            max_bytes=MAX_SKILL_ARCHIVE_BYTES,
            too_large_detail="Skill archive exceeds the upload limit",
        )
        return await result(
            request,
            response,
            lambda: repository.submit(raw, filename, request_id, principal(request)),
        )

    @app.get("/api/skills/imports")
    async def imports(request: Request, response: Response):
        return await result(request, response, lambda: repository.list_imports(principal(request)))

    @app.get("/api/skills/imports/{import_id}")
    async def detail(import_id: str, request: Request, response: Response):
        return await result(
            request, response, lambda: repository.detail(import_id, principal(request))
        )

    @app.post("/api/skills/imports/{import_id}/review")
    async def review(import_id: str, request: Request, response: Response):
        raw = await read_body(
            request, max_bytes=32_000, too_large_detail="Private Skill review request is too large"
        )
        try:
            data = SkillImportReviewRequest.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid private Skill review request", headers) from exc
        return await result(
            request, response, lambda: repository.review(import_id, data, principal(request))
        )

    @app.get("/api/skills/imports/{import_id}/files")
    async def import_file(
        import_id: str,
        request: Request,
        response: Response,
        path: Annotated[str, Query(min_length=1, max_length=1000)],
    ):
        value, _raw = await result(
            request, response, lambda: repository.read_file(import_id, path, principal(request))
        )
        return value

    @app.get("/api/skills/imports/{import_id}/download")
    async def import_download(
        import_id: str,
        request: Request,
        response: Response,
        path: Annotated[str, Query(min_length=1, max_length=1000)],
    ):
        _preview, raw = await result(
            request, response, lambda: repository.read_file(import_id, path, principal(request))
        )
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                **headers,
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(path.split("/")[-1], safe=""),
            },
        )

    @app.get("/api/skills/repository/{name}/versions/{content_hash}")
    async def version(name: str, content_hash: str, request: Request, response: Response):
        return await result(
            request,
            response,
            lambda: repository.package_view(name, content_hash, principal(request)),
        )

    @app.post("/api/skills/repository/{name}/selection")
    async def select(name: str, request: Request, response: Response):
        raw = await read_body(
            request, max_bytes=2048, too_large_detail="Private Skill selection request is too large"
        )
        try:
            data = SkillRepositorySelection.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid private Skill selection request", headers) from exc
        return await result(
            request, response, lambda: repository.select(name, data, principal(request))
        )

    @app.get("/api/skills/repository/{name}/versions/{content_hash}/files")
    async def version_file(
        name: str,
        content_hash: str,
        request: Request,
        response: Response,
        path: Annotated[str, Query(min_length=1, max_length=1000)],
    ):
        value, _raw = await result(
            request,
            response,
            lambda: repository.read_file(name, path, principal(request), content_hash=content_hash),
        )
        return value

    @app.get("/api/skills/repository/{name}/versions/{content_hash}/download")
    async def export(name: str, content_hash: str, request: Request, response: Response):
        skill, raw = await result(
            request, response, lambda: repository.export(name, content_hash, principal(request))
        )
        return Response(
            raw,
            media_type="application/zip",
            headers={
                **headers,
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(f"{skill.name}-{skill.content_hash[:12]}.zip", safe=""),
            },
        )
