"""Scheduler-Läufe: Historie, Detail, manueller Trigger."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ...core.errors import NotFoundError
from ...repositories import run_repo
from ...schemas.common import Page
from ...schemas.entities import RunDetail, RunOut
from ...services.scheduler import get_scheduler
from ..deps import CurrentUser, SessionDep

router = APIRouter(prefix="/runs", tags=["runs"])


class TriggerRequest(BaseModel):
    dry_run: bool | None = None


@router.get("", response_model=Page[RunOut])
async def list_runs(
    _: CurrentUser,
    session: SessionDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> Page[RunOut]:
    rows, total = await run_repo.list_runs(session, page=page, page_size=page_size)
    return Page[RunOut](
        items=[RunOut.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(_: CurrentUser, run_id: int, session: SessionDep) -> RunDetail:
    run = await run_repo.get(session, run_id)
    if run is None:
        raise NotFoundError("Lauf nicht gefunden.")
    return RunDetail.model_validate(run, from_attributes=True)


@router.post("/trigger", response_model=RunDetail)
async def trigger(_: CurrentUser, body: TriggerRequest) -> RunDetail:
    run = await get_scheduler().trigger_now(dry_run_override=body.dry_run)
    return RunDetail.model_validate(run, from_attributes=True)
