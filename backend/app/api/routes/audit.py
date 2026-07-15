"""Audit-Protokoll lesen (nur für Administratoren)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

from ...repositories import audit_repo
from ...schemas.audit import AuditEntryOut, AuditPage
from ..deps import AdminUser, SessionDep

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditPage)
async def list_audit(
    _: AdminUser,
    session: SessionDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str | None = None,
    actor: str | None = None,
    outcome: str | None = None,
    days: int | None = Query(None, ge=1, le=3650),
) -> AuditPage:
    """Protokolleinträge, neueste zuerst. Bewusst nur lesbar — keine Route zum Ändern
    oder Löschen einzelner Einträge, sonst wäre die Spur manipulierbar."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days) if days else None
    rows, total = await audit_repo.list_paged(
        session,
        page=page,
        page_size=page_size,
        action=action,
        actor=actor,
        outcome=outcome,
        since=since,
    )
    return AuditPage(
        items=[AuditEntryOut.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/actions", response_model=list[str])
async def list_actions(_: AdminUser, session: SessionDep) -> list[str]:
    """Vorhandene Aktionsarten — speist den Filter in der Oberfläche."""
    return await audit_repo.distinct_actions(session)
