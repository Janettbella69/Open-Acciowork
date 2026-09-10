"""Reviewed human template curation and recoverable creation of fresh projects."""

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, AuthorizationError
from .project_template_apply import TemplateApplications
from .project_template_models import TemplateApply, TemplateArchive, TemplateSave
from .project_templates import ProjectTemplates, TemplateConflict, TemplateUnavailable
from .storage import LedgerNotFoundError


class TemplateResume(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm_creation: Literal[True]


def register_project_template_routes(app: FastAPI, ledger, access, settings, read_body):
    templates = ProjectTemplates(ledger, access, settings)
    applications = TemplateApplications(templates)
    app.state.project_templates = templates
    app.state.template_applications = applications
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

    def principal(request):
        return getattr(request.state, "principal", None)

    async def result(response, operation):
        response.headers.update(headers)
        try:
            return await run_in_threadpool(operation)
        except (AuthenticationError, AuthorizationError) as exc:
            raise HTTPException(403, "Project template access is unavailable", headers) from exc
        except LedgerNotFoundError as exc:
            raise HTTPException(404, "Project template or reference not found", headers) from exc
        except TemplateConflict as exc:
            raise HTTPException(409, str(exc), headers) from exc
        except (TemplateUnavailable, OSError) as exc:
            raise HTTPException(
                409, "Project template storage could not be verified", headers
            ) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid project template request", headers) from exc

    async def payload(request, model):
        raw = await read_body(
            request,
            max_bytes=256 * 1024 if model in (TemplateSave, TemplateApply) else 1024,
            too_large_detail="Project template request is too large",
        )
        try:
            return model.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid project template request", headers) from exc

    @app.get("/api/projects/{project_id}/templates")
    async def library(project_id: str, request: Request, response: Response):
        return await result(response, lambda: templates.list(project_id, principal(request)))

    @app.post("/api/projects/{project_id}/templates")
    async def create(project_id: str, request: Request, response: Response):
        data = await payload(request, TemplateSave)
        return await result(response, lambda: templates.save(project_id, data, principal(request)))

    @app.post("/api/projects/{project_id}/templates/{template_id}/versions")
    async def save_version(project_id: str, template_id: str, request: Request, response: Response):
        data = await payload(request, TemplateSave)
        return await result(
            response,
            lambda: templates.save(
                project_id,
                data,
                principal(request),
                template_id=template_id,
            ),
        )

    @app.get("/api/projects/{project_id}/templates/{template_id}/versions/{version}")
    async def detail(
        project_id: str, template_id: str, version: int, request: Request, response: Response
    ):
        return await result(
            response,
            lambda: templates.detail(
                project_id,
                template_id,
                version,
                principal(request),
            ),
        )

    @app.post("/api/projects/{project_id}/templates/{template_id}/archive")
    async def archive(project_id: str, template_id: str, request: Request, response: Response):
        data = await payload(request, TemplateArchive)
        return await result(
            response,
            lambda: templates.archive(
                project_id,
                template_id,
                data,
                principal(request),
            ),
        )

    @app.get(
        "/api/projects/{project_id}/templates/{template_id}/versions/{version}/references/{artifact_id}/download"
    )
    async def download(
        project_id: str,
        template_id: str,
        version: int,
        artifact_id: str,
        request: Request,
        response: Response,
    ):
        reference, raw = await result(
            response,
            lambda: templates.reference(
                project_id,
                template_id,
                version,
                artifact_id,
                principal(request),
            ),
        )
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                **headers,
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(reference.name, safe=""),
            },
        )

    @app.post("/api/projects/{project_id}/templates/{template_id}/applications")
    async def apply(project_id: str, template_id: str, request: Request, response: Response):
        data = await payload(request, TemplateApply)
        return await result(
            response,
            lambda: applications.apply(
                project_id,
                template_id,
                data,
                principal(request),
            ),
        )

    @app.get("/api/projects/{project_id}/templates/{template_id}/applications")
    async def history(
        project_id: str,
        template_id: str,
        request: Request,
        response: Response,
        before: Annotated[str | None, Query(max_length=100)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        return await result(
            response,
            lambda: applications.list(
                project_id,
                template_id,
                principal(request),
                before=before,
                limit=limit,
            ),
        )

    @app.post(
        "/api/projects/{project_id}/templates/{template_id}/applications/{application_id}/resume"
    )
    async def resume(
        project_id: str,
        template_id: str,
        application_id: str,
        request: Request,
        response: Response,
    ):
        await payload(request, TemplateResume)
        return await result(
            response,
            lambda: applications.resume(
                project_id,
                template_id,
                application_id,
                principal(request),
            ),
        )
