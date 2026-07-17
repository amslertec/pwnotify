"""Instanzweiter Multi-Tenant-Mode-Schalter + Default-Kunden-Umbenennung (Access-Modell/
Superadmin-Design, Task 5).

`GET` ist für JEDES authentifizierte Konto offen -- das Frontend braucht den Schalterstand,
um sein Chrome zu gaten (Mandanten-Umschalter/-Verwaltung nur sichtbar, wenn Multi-Tenant-
Mode aktiv ist), unabhängig von der Rolle. `PUT` ist SUPERADMIN-only (Design §6): nur er
darf die Instanz global umschalten oder den Default-Kunden umbenennen (der Slug bleibt
dabei immer `'default'` -- `TenantUpdate`/hier `InstanceUpdate` kennen kein Slug-Feld).
Zusätzlich (Context-Gating v2, Matrix B) nur im DEFAULT-Kontext: schaltet der Superadmin in
einen Kunden-Kontext um, ist diese Route gesperrt (`SuperadminDefaultContextUser`,
`default_context_required`) -- Instanz-Mode/Default-Umbenennung sind Provider-Ebene-
Aktionen, kein Kunden-Admin-Werkzeug.

Der Schalter selbst lebt IMMER als `setting`-Zeile des Default-Tenants (siehe
`services.instance_settings`), NICHT als rohe Owner-Zeile -- `instance.multi_tenant_mode`
ist zwar in `settings_schema.SETTINGS` registriert wie jeder andere Key, aber NICHT über
die generische Pro-Tenant-Settings-Route (`PUT /settings`) schreibbar (siehe deren Guard
in `routes/settings.py`) -- ausschliesslich hier.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...repositories import tenant_repo
from ...schemas.instance import InstanceOut, InstanceUpdate
from ...services import audit, instance_settings
from ..deps import CurrentUser, SessionDep, SuperadminDefaultContextUser

router = APIRouter(prefix="/admin/instance", tags=["admin-instance"])


@router.get("", response_model=InstanceOut)
async def get_instance(_: CurrentUser, session: SessionDep) -> InstanceOut:
    return InstanceOut(
        multi_tenant_mode=await instance_settings.read_mode(session),
        default_tenant_name=await instance_settings.read_default_tenant_name(session),
    )


@router.put("", response_model=InstanceOut)
async def update_instance(
    request: Request, admin: SuperadminDefaultContextUser, body: InstanceUpdate, session: SessionDep
) -> InstanceOut:
    if body.multi_tenant_mode is not None:
        await instance_settings.write_mode(session, body.multi_tenant_mode)
        await audit.record(
            session,
            action=audit.INSTANCE_MODE_CHANGED,
            actor=admin,
            request=request,
            detail={"multi_tenant_mode": body.multi_tenant_mode},
        )

    if body.default_tenant_name is not None:
        default = await tenant_repo.default_tenant(session)
        assert default.id is not None
        updated = await tenant_repo.update(session, default.id, name=body.default_tenant_name)
        await audit.record(
            session,
            action=audit.TENANT_UPDATED,
            actor=admin,
            target=updated.slug,
            request=request,
            detail={"name": updated.name},
        )

    await session.commit()

    return InstanceOut(
        multi_tenant_mode=await instance_settings.read_mode(session),
        default_tenant_name=await instance_settings.read_default_tenant_name(session),
    )
