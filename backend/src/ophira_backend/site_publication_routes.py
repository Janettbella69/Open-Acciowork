"""Human review and public read boundaries for published artifact pages."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .auth import ProjectPermission
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .project_scope import LOCAL_OPERATOR_ID
from .site_publications import (
    SitePublicationCatalog,
    SitePublicationConflict,
    SitePublicationDecision,
    SitePublicationRevoke,
    SitePublicationStage,
    SitePublicationStore,
    SitePublicationView,
)
from .storage import LedgerNotFoundError

_PAGE_CSP = (
    "sandbox; default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_SCRIPT_CSP = _PAGE_CSP.replace("sandbox;", "sandbox allow-scripts;").replace(
    "script-src 'none'", "script-src 'unsafe-inline'",
)


def _page_headers(*, preview: bool = False, allow_scripts: bool = False) -> dict[str, str]:
    csp = _SCRIPT_CSP if allow_scripts else _PAGE_CSP
    if preview:
        csp = csp.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    return {
        "Cache-Control": "no-store, max-age=0",
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN" if preview else "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
    }


def _site_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (LedgerNotFoundError, ProjectUnavailableError, FileNotFoundError)):
        return HTTPException(404, "Site publication or artifact not found")
    if isinstance(exc, SitePublicationConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, (ProjectLifecycleBusyError, OSError, ValueError)):
        return HTTPException(409, "Site publication could not be verified; refresh and retry")
    raise exc


def register_site_publication_routes(
    app, *, store: SitePublicationStore, settings, read_body,
    require_project_authorization,
) -> None:
    def authorize(request: Request, project_id: str, permission: ProjectPermission) -> str:
        principal = getattr(request.state, "principal", None)
        require_project_authorization(project_id, principal, permission)
        if principal is None:
            if permission == ProjectPermission.PROJECT_READ and settings.auth_mode == "local_only":
                return LOCAL_OPERATOR_ID
            raise HTTPException(403, "An authenticated project administrator is required")
        return principal.principal_id

    def require_configured() -> None:
        if settings.auth_mode != "trusted_gateway" or store.public_origin is None:
            raise HTTPException(409, "Configure a separate public HTTPS site origin")

    @app.get(
        "/api/projects/{project_id}/site-publications",
        response_model=SitePublicationCatalog,
    )
    def list_sites(project_id: str, request: Request):
        authorize(request, project_id, ProjectPermission.PROJECT_READ)
        try:
            publications = store.list(project_id)
        except Exception as exc:
            raise _site_error(exc) from exc
        can_stage = False
        can_publish = False
        if settings.auth_mode == "trusted_gateway" and store.public_origin is not None:
            try:
                authorize(request, project_id, ProjectPermission.PROJECT_WRITE)
                can_stage = True
            except HTTPException:
                pass
            try:
                authorize(request, project_id, ProjectPermission.PROJECT_ACCESS_MANAGE)
                can_publish = True
            except HTTPException:
                pass
        return SitePublicationCatalog(
            public_origin=store.public_origin if settings.auth_mode == "trusted_gateway" else None,
            can_stage=can_stage, can_publish=can_publish, publications=publications,
        )

    @app.post(
        "/api/projects/{project_id}/site-publications",
        response_model=SitePublicationView, status_code=201,
    )
    async def stage_site(project_id: str, request: Request):
        require_configured()
        actor = authorize(request, project_id, ProjectPermission.PROJECT_WRITE)
        raw = await read_body(
            request, max_bytes=4096, too_large_detail="Site draft request is too large",
        )
        try:
            payload = SitePublicationStage.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid site draft request") from exc
        try:
            return await run_in_threadpool(
                store.stage, project_id, payload.artifact_id,
                created_by=actor, allow_scripts=payload.allow_scripts,
            )
        except Exception as exc:
            raise _site_error(exc) from exc

    @app.get(
        "/api/projects/{project_id}/site-publications/{site_id}",
        response_model=SitePublicationView,
    )
    def get_site(project_id: str, site_id: str, request: Request):
        authorize(request, project_id, ProjectPermission.PROJECT_READ)
        try:
            return store.get(project_id, site_id)
        except Exception as exc:
            raise _site_error(exc) from exc

    @app.get("/api/projects/{project_id}/site-publications/{site_id}/preview")
    def preview_site(project_id: str, site_id: str, request: Request):
        authorize(request, project_id, ProjectPermission.PROJECT_READ)
        try:
            raw, allow_scripts = store.preview(project_id, site_id)
        except Exception as exc:
            raise _site_error(exc) from exc
        return Response(
            raw, media_type="text/html",
            headers=_page_headers(preview=True, allow_scripts=allow_scripts),
        )

    @app.post(
        "/api/projects/{project_id}/site-publications/{site_id}/publish",
        response_model=SitePublicationView,
    )
    async def publish_site(project_id: str, site_id: str, request: Request):
        require_configured()
        actor = authorize(request, project_id, ProjectPermission.PROJECT_ACCESS_MANAGE)
        raw = await read_body(
            request, max_bytes=4096, too_large_detail="Site publication request is too large",
        )
        try:
            payload = SitePublicationDecision.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid site publication approval") from exc
        try:
            return await run_in_threadpool(
                store.publish, project_id, site_id, payload, approved_by=actor,
            )
        except Exception as exc:
            raise _site_error(exc) from exc

    @app.post(
        "/api/projects/{project_id}/site-publications/{site_id}/revoke",
        response_model=SitePublicationView,
    )
    async def revoke_site(project_id: str, site_id: str, request: Request):
        actor = authorize(request, project_id, ProjectPermission.PROJECT_ACCESS_MANAGE)
        raw = await read_body(
            request, max_bytes=4096, too_large_detail="Site revoke request is too large",
        )
        try:
            payload = SitePublicationRevoke.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid site revoke request") from exc
        try:
            return await run_in_threadpool(
                store.revoke, project_id, site_id, payload, revoked_by=actor,
            )
        except Exception as exc:
            raise _site_error(exc) from exc

    @app.get("/published/{project_id}/{site_id}")
    def public_site(project_id: str, site_id: str, request: Request):
        expected_host = (
            urlsplit(store.public_origin).netloc.casefold()
            if settings.auth_mode == "trusted_gateway" and store.public_origin else None
        )
        if request.headers.get("host", "").casefold() != expected_host:
            raise HTTPException(404, "Page not found")
        try:
            raw, allow_scripts = store.public_html(project_id, site_id)
        except (LedgerNotFoundError, ProjectUnavailableError, FileNotFoundError):
            raise HTTPException(404, "Page not found") from None
        except (SitePublicationConflict, ProjectLifecycleBusyError, OSError, ValueError):
            raise HTTPException(503, "Page is temporarily unavailable") from None
        return Response(
            raw, media_type="text/html", headers=_page_headers(allow_scripts=allow_scripts),
        )
