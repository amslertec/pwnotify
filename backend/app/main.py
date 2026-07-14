"""FastAPI-App-Factory: Router, SPA-Auslieferung, Lifespan (Scheduler), Härtung."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from . import __version__
from .api.deps import limiter
from .api.routes import (
    admin_users,
    auth,
    branding,
    dashboard,
    health,
    notifications,
    runs,
    setup,
    users,
)
from .api.routes import (
    settings as settings_routes,
)
from .core.config import get_settings
from .core.errors import register_exception_handlers
from .core.logging import configure_logging, get_logger
from .db.migrate import run_migrations
from .db.session import dispose_engine, get_session_factory
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
    # 1) Migrationen (idempotent; im Thread, da alembic env.py asyncio.run nutzt)
    await asyncio.to_thread(run_migrations)
    # 2) Seed aus ENV (nur beim ersten Start wirksam)
    factory = get_session_factory()
    await run_seed(factory)
    # 3) Scheduler starten
    scheduler = SchedulerService(factory, base_url=settings.base_url)
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
        default_response_class=ORJSONResponse,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # Rate-Limiting (Login-Brute-Force-Schutz)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    register_exception_handlers(app)

    # Health ausserhalb /api (Docker-HEALTHCHECK + Orchestrator-Probes)
    app.include_router(health.router)

    api = [
        auth.router,
        setup.router,
        users.router,
        admin_users.router,
        notifications.router,
        runs.router,
        settings_routes.router,
        dashboard.router,
        branding.router,
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
    return ORJSONResponse(
        status_code=429,
        content={
            "error": {"code": "rate_limited", "message": "Zu viele Anfragen. Bitte kurz warten."}
        },
    )


app = create_app()
