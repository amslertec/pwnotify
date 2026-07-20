"""FastAPI-App-Factory: Router, SPA-Auslieferung, Lifespan (Scheduler), Härtung."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from . import __version__
from .api.deps import limiter
from .api.routes import (
    admin_assignments,
    admin_groups,
    admin_instance,
    admin_tenants,
    admin_users,
    audit,
    auth,
    branding,
    dashboard,
    entra_avatar,
    health,
    notifications,
    public_tokens,
    runs,
    setup,
    users,
    version,
)
from .api.routes import (
    settings as settings_routes,
)
from .core.body_limit import MaxBodySizeMiddleware
from .core.config import get_settings
from .core.errors import UnhandledExceptionMiddleware, register_exception_handlers
from .core.logging import configure_logging, get_logger
from .core.security_headers import (
    SecurityHeadersMiddleware,
    build_csp,
    inline_script_hashes,
)
from .db.migrate import run_migrations
from .db.session import dispose_engine, get_session_factory
from .db.tenant_context import open_active_session
from .repositories import run_repo
from .seed import run_seed
from .services.scheduler import SchedulerService, set_scheduler

log = get_logger("main")


class SPAStaticFiles(StaticFiles):
    """Statisches Frontend mit SPA-Fallback: unbekannte Pfade -> index.html.

    Cache-Header: gehashte Assets dürfen lange gecacht werden, die HTML-Shell und
    alle anderen Dateien nicht (sonst zeigen Browser wie Opera veraltete Versionen).
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            resp = await super().get_response(path, scope)
            served = path
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                resp = await super().get_response("index.html", scope)
                served = "index.html"
            else:
                raise
        if served.startswith("assets/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Fail fast at startup instead of serving a healthy-looking container whose every
    # tenant-scoped request 500s: touching runtime_database_url raises when
    # PWNOTIFY_RUNTIME_DB_PASSWORD is unset (config.py refuses a superuser fallback).
    _ = settings.runtime_database_url
    # 1) Migrationen (idempotent; im Thread, da alembic env.py asyncio.run nutzt)
    await asyncio.to_thread(run_migrations)
    # 2) Seed aus ENV (nur beim ersten Start wirksam)
    factory = get_session_factory()
    await run_seed(factory)
    # 2b) Läufe aufräumen, die ein Neustart mitten drin erwischt hat — sie stünden
    # sonst für immer auf "running" und verfälschten Historie und Statistik.
    async with factory() as session:
        stale = await run_repo.mark_stale_as_error(session)
        if stale:
            log.warning("stale_runs_cleaned", count=stale)
    # 3) Start scheduler -- tenant writes go through the context-aware factory
    # (runtime role when a tenant is active, owner otherwise); `mark_stale_as_error` above
    # deliberately stays on the owner factory (no tenant context, cross-tenant op).
    scheduler = SchedulerService(open_active_session, base_url=settings.base_url)
    set_scheduler(scheduler)
    await scheduler.start()
    log.info("startup_complete", version=__version__)
    try:
        yield
    finally:
        await scheduler.shutdown()
        await dispose_engine()
        log.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="PwNotify",
        version=__version__,
        # OpenAPI docs/schema off by default (M6) -- they expose the full route map to anyone.
        docs_url="/api/docs" if settings.enable_docs else None,
        openapi_url="/api/openapi.json" if settings.enable_docs else None,
        lifespan=lifespan,
    )

    # Catch-all for unhandled exceptions (I2). Registered FIRST so it is the INNERMOST user
    # middleware -- it converts a genuine 500 into a clean JSON response that then flows back
    # out through SecurityHeadersMiddleware (so the headers apply and no traceback leaks).
    app.add_middleware(UnhandledExceptionMiddleware)

    # Rate-Limiting (Login-Brute-Force-Schutz)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    # Reject over-large request bodies before any handler reads them (M5). Inside the security
    # headers (added later, so outer) -> the 413 still carries them.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)

    # Host-Header prüfen, sofern konfiguriert. Standard ist offen — siehe config.py.
    hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
    if hosts:
        # Der Healthcheck spricht den Container über 127.0.0.1 an und muss weiter laufen.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*hosts, "127.0.0.1", "localhost"])
        log.info("trusted_hosts_enabled", hosts=hosts)

    # Security-Header. Die Hashes der Inline-Skripte werden einmal beim Start aus der
    # ausgelieferten index.html gelesen, damit die CSP ohne 'unsafe-inline' auskommt.
    hashes = inline_script_hashes(Path(settings.static_dir) / "index.html")
    if not hashes:
        log.warning(
            "csp_inline_hashes_missing", hint="index.html nicht lesbar — CSP evtl. zu streng"
        )
    app.add_middleware(
        SecurityHeadersMiddleware,
        csp=build_csp(hashes),
        hsts=settings.cookie_secure,  # nur sinnvoll, wenn die App über HTTPS läuft
    )

    register_exception_handlers(app)

    # Health ausserhalb /api (Docker-HEALTHCHECK + Orchestrator-Probes)
    app.include_router(health.router)

    api = [
        auth.router,
        audit.router,
        setup.router,
        users.router,
        entra_avatar.router,
        admin_users.router,
        admin_tenants.router,
        admin_assignments.router,
        admin_groups.router,
        admin_instance.router,
        public_tokens.router,
        notifications.router,
        runs.router,
        settings_routes.router,
        dashboard.router,
        branding.router,
        version.router,
    ]
    for r in api:
        app.include_router(r, prefix="/api")

    # SPA zuletzt mounten (fängt alle nicht von der API belegten Pfade ab)
    static_dir = settings.static_dir
    if os.path.isdir(static_dir):
        app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="spa")
    else:  # Dev ohne gebautes Frontend
        log.warning("static_dir_missing", path=static_dir)

    return app


def _rate_limit_handler(request, exc):  # type: ignore[no-untyped-def]
    return JSONResponse(
        status_code=429,
        content={
            "error": {"code": "rate_limited", "message": "Zu viele Anfragen. Bitte kurz warten."}
        },
    )


app = create_app()
