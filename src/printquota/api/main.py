"""FastAPI application: admin console + self-service portal."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .. import __version__
from ..core.config import get_settings
from ..core.logging import setup_logging
from ..db import session as db_session
from .deps import TEMPLATE_DIR, render
from .routes import admin, auth as auth_routes, portal, queues, settings as settings_routes, setup, users

log = setup_logging("api")

#: Paths that answer auth errors with JSON instead of a page or redirect.
_JSON_PREFIXES = ("/api/", "/admin/api/")


def create_app() -> FastAPI:
    """Application factory (used by uvicorn and by the tests)."""
    settings = get_settings()
    app = FastAPI(
        title="printquota",
        description="Self-hosted CUPS print quota, policy and accounting system",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    static_dir = TEMPLATE_DIR.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(setup.router)
    app.include_router(auth_routes.router)
    app.include_router(portal.router)
    # Specific /admin routes first: /admin/users/import must not match /admin/users/{username}.
    app.include_router(users.router)
    app.include_router(queues.router)
    app.include_router(settings_routes.router)
    app.include_router(admin.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """Liveness probe that also proves the datastore is reachable."""
        try:
            with db_session.session_scope() as session:
                session.execute(text("SELECT 1"))
            return JSONResponse({"status": "ok"})
        except Exception as exc:  # pragma: no cover - failure path
            log.error("health check failed", extra={"error": str(exc)})
            return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)

    @app.exception_handler(401)
    async def _unauthenticated(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith(_JSON_PREFIXES):
            return JSONResponse({"detail": "not signed in"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(403)
    async def _forbidden(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith(_JSON_PREFIXES):
            return JSONResponse({"detail": "administrator only"}, status_code=403)
        response = render(request, "error.html", code=403, message="Administrators only.")
        response.status_code = 403
        return response

    return app


app = create_app()
