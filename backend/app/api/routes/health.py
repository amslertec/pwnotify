"""Liveness/Readiness-Endpunkte."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ..deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness — bewusst ohne DB (vom Docker-HEALTHCHECK genutzt).

    Unauthenticated by design (the Docker HEALTHCHECK has no credentials) -- so it must
    NOT disclose the running version to anyone probing it. `GET /api/version` (behind
    `CurrentUser`) is the legitimate, authenticated source of that information.
    """
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: SessionDep) -> dict[str, object]:
    """Readiness — prüft DB-Verbindung."""
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"status": "ready" if db_ok else "degraded", "database": db_ok}
