"""Human review and public read boundaries for published artifact pages."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from .auth import ProjectPermission
from .project_lifecycle import ProjectLifecycleBusyError, ProjectUnavailableError
from .project_scope import LOCAL_OPERATOR_ID
from .site_bundle import asset_media_type
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
    "img-src data:; font-src data:; media-src 'none'; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_SCRIPT_CSP = _PAGE_CSP.replace("sandbox;", "sandbox allow-scripts;").replace(
    "script-src 'none'", "script-src 'unsafe-inline'",
)
_OPAQUE_CORS_TYPES = frozenset({"text/javascript", "font/woff", "font/woff2"})
PREVIEW_TOKEN_TTL_SECONDS = 300
_PREVIEW_TOKEN = re.compile(r"[0-9a-f]{16}\.[0-9a-f]{64}")


def _page_headers(
    *, preview: bool = False, allow_scripts: bool = False,
    asset_source: str | None = None,
) -> dict[str, str]:
    csp = _SCRIPT_CSP if allow_scripts else _PAGE_CSP
    if asset_source:
        csp = csp.replace(
            "style-src 'unsafe-inline'", f"style-src 'unsafe-inline' {asset_source}",
        ).replace("img-src data:", f"img-src data: {asset_source}").replace(
            "font-src data:", f"font-src data: {asset_source}",
        ).replace("media-src 'none'", f"media-src {asset_source}")
        if allow_scripts:
            csp = csp.replace(
                "script-src 'unsafe-inline'",
                f"script-src 'unsafe-inline' {asset_source}",
            )
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


def _asset_source(
    request: Request, project_id: str, site_id: str, *, preview: bool,
    preview_token: str | None = None,
) -> str:
    hostname = request.url.hostname or ""
    if not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
        raise HTTPException(409, "Site host cannot be used in the asset policy")
    port = request.url.port
    host = hostname + (f":{port}" if port else "")
    if preview_token is not None:
        path = f"/api/site-previews/{project_id}/{site_id}/{preview_token}/assets/"
    elif preview:
        path = f"/api/projects/{project_id}/site-publications/{site_id}/preview/assets/"
    else:
        path = f"/published/{project_id}/{site_id}/assets/"
    return host + path


def _asset_headers(*, opaque_cors: bool = False) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store, max-age=0",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if opaque_cors:
        # Sandboxed modules and fonts have an opaque Origin. The preview
        # capability in its asset path authorizes private draft bytes.
        headers["Access-Control-Allow-Origin"] = "null"
        headers["Vary"] = "Origin"
    return headers


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

    def preview_signature(
        request: Request, project_id: str, site_id: str, revision: int,
        checksum: str, expires_at: int,
    ) -> str:
        configured = settings.trusted_gateway_auth_secret
        if configured is None:
            raise HTTPException(404, "Site preview not found")
        host = request.headers.get("host", "").casefold()
        message = (
            f"ophira-site-preview-v1\n{host}\n{project_id}\n{site_id}\n"
            f"{revision}\n{checksum}\n{expires_at}\n"
        ).encode()
        return hmac.new(
            configured.get_secret_value().encode("utf-8"), message, hashlib.sha256,
        ).hexdigest()

    def issue_preview_token(request: Request, record) -> str:
        expires_at = int(time.time()) + PREVIEW_TOKEN_TTL_SECONDS
        signature = preview_signature(
            request, record.project_id, record.id, record.revision,
            record.artifact_checksum, expires_at,
        )
        return f"{expires_at:016x}.{signature}"

    def authorized_preview_record(
        request: Request, project_id: str, site_id: str, token: str,
    ):
        if not _PREVIEW_TOKEN.fullmatch(token):
            raise HTTPException(404, "Site preview not found")
        expires_at = int(token[:16], 16)
        now = int(time.time())
        if expires_at <= now or expires_at > now + PREVIEW_TOKEN_TTL_SECONDS:
            raise HTTPException(404, "Site preview not found")
        try:
            record = store.get(project_id, site_id).publication
        except Exception as exc:
            raise _site_error(exc) from exc
        if (
            record.state != "draft" or not record.bundle
            or record.origin != store.public_origin
        ):
            raise HTTPException(404, "Site preview not found")
        signature = preview_signature(
            request, project_id, site_id, record.revision,
            record.artifact_checksum, expires_at,
        )
        if not hmac.compare_digest(token[17:], signature):
            raise HTTPException(404, "Site preview not found")
        return record

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

    @app.get("/api/projects/{project_id}/site-publications/{site_id}/preview/")
    @app.get("/api/projects/{project_id}/site-publications/{site_id}/preview")
    def preview_site(project_id: str, site_id: str, request: Request):
        authorize(request, project_id, ProjectPermission.PROJECT_READ)
        try:
            record = store.get(project_id, site_id).publication
            raw, allow_scripts, bundle = store.preview(project_id, site_id)
        except Exception as exc:
            raise _site_error(exc) from exc
        if bundle and not request.url.path.endswith("/"):
            raise HTTPException(404, "Use the site bundle preview address")
        if (
            settings.auth_mode == "trusted_gateway" and bundle
            and record.state == "draft" and record.origin == store.public_origin
        ):
            token = issue_preview_token(request, record)
            return RedirectResponse(
                f"/api/site-previews/{project_id}/{site_id}/{token}/",
                status_code=307, headers=_asset_headers(),
            )
        return Response(
            raw, media_type="text/html",
            headers=_page_headers(
                preview=True, allow_scripts=allow_scripts,
                asset_source=_asset_source(request, project_id, site_id, preview=True)
                if bundle else None,
            ),
        )

    @app.get("/api/projects/{project_id}/site-publications/{site_id}/preview/assets/{name}")
    def preview_asset(project_id: str, site_id: str, name: str, request: Request):
        authorize(request, project_id, ProjectPermission.PROJECT_READ)
        media_type = asset_media_type(name)
        if media_type is None:
            raise HTTPException(404, "Site asset not found")
        try:
            raw = store.preview_asset(project_id, site_id, name)
        except Exception as exc:
            raise _site_error(exc) from exc
        return Response(raw, media_type=media_type, headers=_asset_headers())

    @app.get("/api/site-previews/{project_id}/{site_id}/{token}/")
    def token_preview_site(project_id: str, site_id: str, token: str, request: Request):
        require_configured()
        record = authorized_preview_record(request, project_id, site_id, token)
        try:
            raw = store.draft_bundle_preview(
                project_id, site_id, expected_revision=record.revision,
                expected_checksum=record.artifact_checksum,
            )
        except Exception as exc:
            raise _site_error(exc) from exc
        return Response(
            raw, media_type="text/html", headers=_page_headers(
                preview=True, allow_scripts=record.allow_scripts,
                asset_source=_asset_source(
                    request, project_id, site_id, preview=True, preview_token=token,
                ),
            ),
        )

    @app.get("/api/site-previews/{project_id}/{site_id}/{token}/assets/{name}")
    def token_preview_asset(
        project_id: str, site_id: str, token: str, name: str, request: Request,
    ):
        require_configured()
        record = authorized_preview_record(request, project_id, site_id, token)
        media_type = asset_media_type(name)
        if media_type is None:
            raise HTTPException(404, "Site asset not found")
        try:
            raw = store.draft_bundle_preview(
                project_id, site_id, expected_revision=record.revision,
                expected_checksum=record.artifact_checksum, asset_name=name,
            )
        except Exception as exc:
            raise _site_error(exc) from exc
        opaque_cors = (
            media_type in _OPAQUE_CORS_TYPES
            and request.headers.get("origin") == "null"
        )
        return Response(
            raw, media_type=media_type, headers=_asset_headers(opaque_cors=opaque_cors),
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

    def require_public_host(request: Request) -> None:
        expected_host = (
            urlsplit(store.public_origin).netloc.casefold()
            if settings.auth_mode == "trusted_gateway" and store.public_origin else None
        )
        if request.headers.get("host", "").casefold() != expected_host:
            raise HTTPException(404, "Page not found")

    @app.get("/published/{project_id}/{site_id}/")
    @app.get("/published/{project_id}/{site_id}")
    def public_site(project_id: str, site_id: str, request: Request):
        require_public_host(request)
        try:
            raw, allow_scripts, bundle = store.public_html(project_id, site_id)
        except (LedgerNotFoundError, ProjectUnavailableError, FileNotFoundError):
            raise HTTPException(404, "Page not found") from None
        except (SitePublicationConflict, ProjectLifecycleBusyError, OSError, ValueError):
            raise HTTPException(503, "Page is temporarily unavailable") from None
        if bundle and not request.url.path.endswith("/"):
            raise HTTPException(404, "Page not found")
        return Response(
            raw, media_type="text/html", headers=_page_headers(
                allow_scripts=allow_scripts,
                asset_source=_asset_source(request, project_id, site_id, preview=False)
                if bundle else None,
            ),
        )

    @app.get("/published/{project_id}/{site_id}/assets/{name}")
    def public_asset(project_id: str, site_id: str, name: str, request: Request):
        require_public_host(request)
        media_type = asset_media_type(name)
        if media_type is None:
            raise HTTPException(404, "Page not found")
        try:
            raw = store.public_asset(project_id, site_id, name)
        except (LedgerNotFoundError, ProjectUnavailableError, FileNotFoundError):
            raise HTTPException(404, "Page not found") from None
        except (SitePublicationConflict, ProjectLifecycleBusyError, OSError, ValueError):
            raise HTTPException(503, "Page is temporarily unavailable") from None
        opaque_cors = (
            media_type in _OPAQUE_CORS_TYPES
            and request.headers.get("origin") == "null"
        )
        return Response(
            raw, media_type=media_type, headers=_asset_headers(opaque_cors=opaque_cors),
        )
