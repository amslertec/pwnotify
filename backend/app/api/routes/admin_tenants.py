"""Mandantenverwaltung (Kunden-CRUD, Phase 4c Task 2).

Kunden-CRUD (Create/Update/Delete) ist seit dem Access-Modell/Superadmin-Design (§6)
SUPERADMIN-only (`SuperadminUser`) -- ein lokaler Admin verwaltet nur noch Konten
innerhalb seiner zugewiesenen Kunden, weist aber selbst keine Kunden zu und legt keine
neuen an. `list_tenants` bleibt für jedes Konto erreichbar, scopet die Ausgabe aber über
`tenant_repo.allowed_tenant_ids` (Superadmin -> alle, sonst nur die eigenen).

Guard-Rails leben bewusst HIER, nicht in `tenant_repo` (siehe dessen Docstrings zu
`update`/`delete`): der Default-Tenant (`slug == "default"`, von der Migration angelegt)
darf weder gelöscht noch deaktiviert werden, und der letzte AKTIVE Tenant darf nicht
gelöscht werden -- sonst könnte sich niemand mehr anmelden (jeder Login löst über einen
Tenant auf, siehe `tenant_repo.resolve_initial_tenant`). Ein Slug-Wechsel ist bereits
schemaseitig unmöglich: `TenantUpdate` kennt kein `slug`-Feld.

Löschen ist hart und kaskadierend (Design §6): die sechs Datentabellen
(`entra_user`/`exclusion`/`notification_log`/`run`/`setting`) und `auditor_tenant` hängen
per `ondelete=CASCADE` am Tenant und verschwinden automatisch mit `tenant_repo.delete`.
Die per SSO an den Kunden gebundenen Konten (`app_user.tenant_id`) hängen dagegen per
`ondelete=SET NULL` -- ohne explizites Vorab-Löschen blieben sie als instanzweit
aussehende Waisenkonten zurück (kein Datenleck, aber Datenmüll und eine falsche Auskunft
darüber, wer diese Konten noch verwalten dürfte). Branding-Dateien auf Disk sind aktuell
nicht pro Kunde getrennt und überleben die Löschung als verwaiste Datei (Minor, kein
Datenleck, siehe Design §6) -- nur die `branding.*_path`-Settings verschwinden mit der
`setting`-Kaskade.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.errors import ConflictError, NotFoundError
from ...models.tenant import Tenant
from ...repositories import tenant_repo, user_repo
from ...schemas.common import Message
from ...schemas.tenant import TenantCreate, TenantOut, TenantUpdate
from ...services import audit
from ..deps import CurrentUser, SessionDep, SuperadminUser

router = APIRouter(prefix="/admin/tenants", tags=["admin-tenants"])


async def _to_out(session: SessionDep, tenant: Tenant) -> TenantOut:
    assert tenant.id is not None  # persistierte Zeile
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        entra_tenant_id=tenant.entra_tenant_id,
        is_active=tenant.is_active,
        created_at=tenant.created_at,
        sso_user_count=await tenant_repo.count_sso_users(session, tenant.id),
    )


@router.get("")
async def list_tenants(user: CurrentUser, session: SessionDep) -> list[TenantOut]:
    """Kundenliste -- instanzweit NUR für den Superadmin (`allowed_tenant_ids` -> `None`).

    Jedes andere Konto (lokaler Admin/Auditor, jedes SSO-Konto) sieht NUR seine eigenen
    autorisierten Mandanten (`admin_tenants` vereinigt mit `auditor_tenants`, Access-Modell-
    Design §2).
    `tenant` ist keine RLS-Tabelle -- ohne diesen Filter könnte ein Konto, das an Kunde B
    gebunden ist, Name/Slug/Entra-Tenant-Id ALLER anderen Kunden auslesen
    (Cross-Tenant-Enumeration, dieselbe Grenze wie in `get_audit_session`).
    """
    rows = await tenant_repo.list_all(session)
    allowed = await tenant_repo.allowed_tenant_ids(session, user)
    if allowed is not None:
        rows = [t for t in rows if t.id in allowed]
    return [await _to_out(session, t) for t in rows]


@router.post("", response_model=TenantOut)
async def create_tenant(
    request: Request, admin: SuperadminUser, body: TenantCreate, session: SessionDep
) -> TenantOut:
    tenant = await tenant_repo.create(
        session, name=body.name, slug=body.slug, entra_tenant_id=body.entra_tenant_id
    )
    await audit.record(
        session,
        action=audit.TENANT_CREATED,
        actor=admin,
        target=tenant.slug,
        request=request,
        detail={"name": tenant.name, "entra_tenant_id": tenant.entra_tenant_id},
    )
    await session.commit()
    return await _to_out(session, tenant)


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    request: Request, admin: SuperadminUser, tenant_id: int, body: TenantUpdate, session: SessionDep
) -> TenantOut:
    tenant = await tenant_repo.get(session, tenant_id)
    if tenant is None:
        raise NotFoundError("Mandant nicht gefunden.", code="tenant_not_found")
    # Der Default-Kunde darf nicht deaktiviert werden -- ohne ihn hat der lokale Admin
    # keinen Fallback-Tenant mehr (siehe `resolve_initial_tenant`). Ein Slug-Wechsel ist
    # bereits unmöglich: `TenantUpdate` besitzt kein `slug`-Feld.
    if tenant.slug == "default" and body.is_active is False:
        raise ConflictError(
            "Der Standard-Mandant kann nicht deaktiviert werden.",
            code="cannot_deactivate_default_tenant",
        )
    updated = await tenant_repo.update(
        session,
        tenant_id,
        name=body.name,
        entra_tenant_id=body.entra_tenant_id,
        is_active=body.is_active,
    )
    await audit.record(
        session,
        action=audit.TENANT_UPDATED,
        actor=admin,
        target=updated.slug,
        request=request,
        detail={
            "name": updated.name,
            "entra_tenant_id": updated.entra_tenant_id,
            "is_active": updated.is_active,
        },
    )
    await session.commit()
    return await _to_out(session, updated)


@router.delete("/{tenant_id}", response_model=Message)
async def delete_tenant(
    request: Request, admin: SuperadminUser, tenant_id: int, session: SessionDep
) -> Message:
    tenant = await tenant_repo.get(session, tenant_id)
    if tenant is None:
        raise NotFoundError("Mandant nicht gefunden.", code="tenant_not_found")
    if tenant.slug == "default":
        raise ConflictError(
            "Der Standard-Mandant kann nicht gelöscht werden.", code="cannot_delete_default_tenant"
        )
    if tenant.is_active and len(await tenant_repo.list_active(session)) <= 1:
        raise ConflictError(
            "Der letzte aktive Mandant kann nicht gelöscht werden.",
            code="cannot_delete_last_tenant",
        )
    sso_user_count = await tenant_repo.count_sso_users(session, tenant_id)
    await audit.record(
        session,
        action=audit.TENANT_DELETED,
        actor=admin,
        target=tenant.slug,
        request=request,
        detail={"name": tenant.name, "sso_user_count": sso_user_count},
    )
    await session.commit()
    # Harte Löschkaskade (Design §6): gebundene SSO-Konten zuerst räumen -- sonst macht sie
    # der SET-NULL-FK auf app_user.tenant_id zu instanzweit aussehenden Waisenkonten, BEVOR
    # `tenant_repo.delete` die Zeile entfernt (die sechs Datentabellen + auditor_tenant
    # kaskadieren dabei automatisch über ondelete=CASCADE).
    await user_repo.delete_by_tenant(session, tenant_id)
    await tenant_repo.delete(session, tenant_id)
    return Message(message="Mandant gelöscht.")
