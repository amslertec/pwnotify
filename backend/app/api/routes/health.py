"""Liveness/Readiness-Endpunkte."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from ...core.config import get_settings
from ..deps import SessionDep, limiter

router = APIRouter(tags=["health"])

_settings = get_settings()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness — bewusst ohne DB (vom Docker-HEALTHCHECK genutzt).

    Unauthenticated by design (the Docker HEALTHCHECK has no credentials) -- so it must
    NOT disclose the running version to anyone probing it. `GET /api/version` (behind
    `CurrentUser`) is the legitimate, authenticated source of that information.
    """
    return {"status": "ok"}


@router.get("/ready")
@limiter.limit(_settings.ready_rate_limit)
async def ready(request: Request, session: SessionDep) -> dict[str, object]:
    """Readiness — prüft DB-Verbindung.

    Rate-limited (M6): unauthenticated and DB-touching, so an unbounded flood would exhaust
    the connection pool. `request` is required by slowapi to key the limit on the client IP.
    """
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"status": "ready" if db_ok else "degraded", "database": db_ok}
