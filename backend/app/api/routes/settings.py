"""Settings-Verwaltung (alle Tabs) + Verbindungstests + Vorschauen + Exclusions."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter

from ...repositories import exclusion_repo
from ...schemas.common import Message
from ...schemas.entities import ExclusionOut
from ...schemas.settings import (
    CronPreviewRequest,
    CronPreviewResult,
    GraphTestRequest,
    GraphTestResult,
    MailTestRequest,
    SettingsUpdate,
    TemplatePreviewRequest,
    TemplatePreviewResult,
)
from ...services.connectivity import send_test_mail, test_graph
from ...services.scheduler import compute_next_runs, get_scheduler
from ...services.settings_schema import SETTINGS
from ...services.settings_service import effective_base_url
from ...services.templating import render, sample_context
from ..deps import AdminUser, CurrentUser, SessionDep, SettingsDep

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=dict)
async def get_all(_: CurrentUser, svc: SettingsDep) -> dict[str, Any]:
    return await svc.get_public()


@router.put("", response_model=dict)
async def update(_: AdminUser, body: SettingsUpdate, svc: SettingsDep) -> dict[str, Any]:
    await svc.set_many(body.values)
    # Bei Schedule-Änderungen den laufenden Job neu planen.
    if any(k.startswith("schedule.") for k in body.values):
        with contextlib.suppress(RuntimeError):
            await get_scheduler().reschedule()
    return await svc.get_public()


@router.post("/graph/test", response_model=GraphTestResult)
async def graph_test(_: AdminUser, body: GraphTestRequest, svc: SettingsDep) -> GraphTestResult:
    settings = await svc.get_all()
    result = await test_graph(
        settings,
        tenant_id=body.tenant_id,
        client_id=body.client_id,
        client_secret=body.client_secret,
        cloud=body.cloud,
    )
    return GraphTestResult(**result.__dict__)


@router.post("/mail/test", response_model=Message)
async def mail_test(_: AdminUser, body: MailTestRequest, svc: SettingsDep) -> Message:
    settings = await svc.get_all()
    await send_test_mail(
        settings, to=body.to, locale=body.locale, base_url=effective_base_url(settings)
    )
    return Message(message=f"Test-Mail an {body.to} versendet.")


@router.post("/schedule/preview", response_model=CronPreviewResult)
async def schedule_preview(_: CurrentUser, body: CronPreviewRequest) -> CronPreviewResult:
    try:
        runs = compute_next_runs(body.cron, body.timezone, count=5)
    except Exception as exc:
        return CronPreviewResult(valid=False, error=str(exc))
    return CronPreviewResult(valid=True, next_runs=[r.isoformat() for r in runs])


@router.post("/template/preview", response_model=TemplatePreviewResult)
async def template_preview(
    _: CurrentUser, body: TemplatePreviewRequest, svc: SettingsDep
) -> TemplatePreviewResult:
    settings = await svc.get_all()
    locale = "en" if body.locale.lower().startswith("en") else "de"
    context = sample_context(settings, effective_base_url(settings), locale)
    return TemplatePreviewResult(
        subject=render(body.subject, context, html=False),
        html=render(body.html, context, html=True),
    )


@router.post("/template/reset", response_model=dict)
async def template_reset(_: AdminUser, svc: SettingsDep) -> dict[str, Any]:
    defaults = {k: spec.default for k, spec in SETTINGS.items() if k.startswith("template.")}
    await svc.set_many(defaults)
    return await svc.get_public()


# ---- Exclusions ------------------------------------------------------------- #
@router.get("/exclusions", response_model=list[ExclusionOut])
async def list_exclusions(_: CurrentUser, session: SessionDep) -> list[ExclusionOut]:
    rows = await exclusion_repo.list_all(session)
    return [ExclusionOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/exclusions", response_model=ExclusionOut)
async def add_exclusion(_: AdminUser, body: dict[str, str], session: SessionDep) -> ExclusionOut:
    exc = await exclusion_repo.add(
        session,
        kind=body.get("kind", "user"),
        value=body["value"],
        label=body.get("label"),
    )
    return ExclusionOut.model_validate(exc, from_attributes=True)


@router.delete("/exclusions/{exclusion_id}", response_model=Message)
async def delete_exclusion(_: AdminUser, exclusion_id: int, session: SessionDep) -> Message:
    await exclusion_repo.delete(session, exclusion_id)
    return Message(message="Ausschluss entfernt.")
