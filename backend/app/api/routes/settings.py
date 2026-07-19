"""Settings-Verwaltung (alle Tabs) + Verbindungstests + Vorschauen + Exclusions."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Request

from ...core.errors import ForbiddenError
from ...models.entra import Exclusion
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
from ...services import audit
from ...services.connectivity import send_test_mail, test_graph
from ...services.scheduler import compute_next_runs, get_scheduler
from ...services.settings_schema import MASK, SETTINGS
from ...services.settings_service import effective_base_url
from ...services.templating import render, sample_context
from ..deps import (
    AdminUser,
    CurrentUser,
    TenantSessionDep,
    TenantSettingsDep,
    TenantWriteSessionDep,
    TenantWriteSettingsDep,
)

router = APIRouter(prefix="/settings", tags=["settings"])

# `instance.*` (aktuell nur `instance.multi_tenant_mode`, Task 5) ist zwar in `SETTINGS`
# registriert wie jeder andere Key -- aber ausschliesslich über die superadmin-gegatete
# `PUT /admin/instance` schreibbar. Ohne diesen Guard könnte ein lokaler (Nicht-Super-)
# Admin über DIESE generische Pro-Tenant-Route den instanzweiten Schalter auf seinem
# eigenen Tenant umschalten (`SettingsService._upsert` stempelt `tenant_id` aus dem
# aktiven Kontext) -- das würde den globalen Schalter faktisch aushebeln, weil
# `instance_settings.read_mode` ihn ohnehin IMMER default-tenant-gescopt liest, ein
# solcher Fremd-Tenant-Write also nur verwirrenden Datenmüll erzeugen, aber schlimmer:
# ein Schreibversuch GEGEN den Default-Tenant selbst (falls der aufrufende Admin dort
# berechtigt ist) würde den globalen Schalter tatsächlich unerwünscht umschalten.
_INSTANCE_PREFIX = "instance."


@router.get("", response_model=dict)
async def get_all(_: CurrentUser, svc: TenantSettingsDep) -> dict[str, Any]:
    return await svc.get_public()


@router.put("", response_model=dict)
async def update(
    request: Request,
    admin: AdminUser,
    body: SettingsUpdate,
    svc: TenantWriteSettingsDep,
    session: TenantWriteSessionDep,
) -> dict[str, Any]:
    forbidden = sorted(k for k in body.values if k.startswith(_INSTANCE_PREFIX))
    if forbidden:
        raise ForbiddenError(
            "Instanzweite Einstellungen können nur über /admin/instance geändert werden.",
            code="instance_setting_forbidden",
        )
    await svc.set_many(body.values)

    # Protokolliert werden die geänderten SCHLÜSSEL, niemals ihre Werte — sonst stünden
    # Graph- und SMTP-Secrets im Klartext im Protokoll. Secret-Änderungen bekommen einen
    # eigenen Eintrag, weil sie sicherheitsrelevanter sind als eine Cron-Anpassung.
    # Der Masken-Marker heisst "unverändert" und zählt daher nicht als Änderung.
    geaendert = sorted(k for k in body.values if k in SETTINGS)
    secrets = sorted(
        k for k in geaendert if SETTINGS[k].secret and body.values[k] not in (MASK, None, "")
    )
    if secrets:
        await audit.record(
            session,
            action=audit.SECRET_CHANGED,
            actor=admin,
            request=request,
            detail={"keys": secrets},
        )
    normale = [k for k in geaendert if k not in secrets]
    if normale:
        await audit.record(
            session,
            action=audit.SETTINGS_CHANGED,
            actor=admin,
            request=request,
            detail={"keys": normale},
        )
    await session.commit()

    # Bei Schedule-Änderungen den laufenden Job neu planen.
    if any(k.startswith("schedule.") for k in body.values):
        with contextlib.suppress(RuntimeError):
            await get_scheduler().reschedule()
    return await svc.get_public()


@router.post("/graph/test", response_model=GraphTestResult)
async def graph_test(
    _: AdminUser, body: GraphTestRequest, svc: TenantWriteSettingsDep
) -> GraphTestResult:
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
async def mail_test(_: AdminUser, body: MailTestRequest, svc: TenantWriteSettingsDep) -> Message:
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
    _: CurrentUser, body: TemplatePreviewRequest, svc: TenantSettingsDep
) -> TemplatePreviewResult:
    settings = await svc.get_all()
    locale = "en" if body.locale.lower().startswith("en") else "de"
    context = sample_context(settings, effective_base_url(settings), locale)
    return TemplatePreviewResult(
        subject=render(body.subject, context, html=False),
        html=render(body.html, context, html=True),
    )


@router.post("/template/reset", response_model=dict)
async def template_reset(_: AdminUser, svc: TenantWriteSettingsDep) -> dict[str, Any]:
    defaults = {k: spec.default for k, spec in SETTINGS.items() if k.startswith("template.")}
    await svc.set_many(defaults)
    return await svc.get_public()


# ---- Exclusions ------------------------------------------------------------- #
@router.get("/exclusions", response_model=list[ExclusionOut])
async def list_exclusions(_: CurrentUser, session: TenantSessionDep) -> list[ExclusionOut]:
    rows = await exclusion_repo.list_all(session)
    return [ExclusionOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/exclusions", response_model=ExclusionOut)
async def add_exclusion(
    request: Request, admin: AdminUser, body: dict[str, str], session: TenantWriteSessionDep
) -> ExclusionOut:
    kind = body.get("kind", "user")
    value = body["value"]
    await audit.record(
        session,
        action=audit.USER_EXCLUDED,
        actor=admin,
        request=request,
        target=value,
        detail={"excluded": True, "count": 1, "kind": kind},
    )
    exc = await exclusion_repo.add(session, kind=kind, value=value, label=body.get("label"))
    return ExclusionOut.model_validate(exc, from_attributes=True)


@router.delete("/exclusions/{exclusion_id}", response_model=Message)
async def delete_exclusion(
    request: Request, admin: AdminUser, exclusion_id: int, session: TenantWriteSessionDep
) -> Message:
    exc = await session.get(Exclusion, exclusion_id)
    if exc is not None:
        await audit.record(
            session,
            action=audit.USER_EXCLUDED,
            actor=admin,
            request=request,
            target=exc.value,
            detail={"excluded": False, "count": 1, "kind": exc.kind},
        )
    await exclusion_repo.delete(session, exclusion_id)
    return Message(message="Ausschluss entfernt.")
