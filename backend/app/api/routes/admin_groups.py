"""Assignment-Group-Verwaltung ("Teams", Console+Groups+Invite-Phase Task 3): Entra-
Security-Gruppen des PROVIDER-Tenants, die auf einen oder mehrere Kunden gemappt sind.

Wie die Mandanten-/Zuweisungs-Konsole SUPERADMIN-only UND nur im DEFAULT-Kontext
(`SuperadminDefaultContextUser`) -- Provider-Ebene-Verwaltung, aus einem Kunden-Kontext
heraus gesperrt, genau wie `admin_tenants`/`admin_assignments`.

**`entra_group_id` ist in diesem Inkrement FREI-TEXT** (Design §7): kein Graph-Backed
Group-Picker fuer den Provider-Tenant -- das ist eine explizit vertagte Phase-2-Erweiterung,
hier bewusst NICHT gebaut.

Der eigentliche Gruppen-Reconcile (welche Login-Gruppen-Claims welche `admin_tenant`/
`auditor_tenant`-Zeilen mit `source='group'` erzeugen) ist Task 4 -- diese Route liefert nur
die CRUD-Verwaltung der Zuordnung selbst, inklusive `assignment_group_repo.
tenant_ids_for_entra_groups`, die Task 4 fuer den Login-Reconcile konsumiert."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ...core.errors import NotFoundError
from ...core.logging import get_logger
from ...db.tenant_context import tenant_scoped_session
from ...models.assignment_group import AssignmentGroup
from ...repositories import assignment_group_member_repo, assignment_group_repo
from ...schemas.assignment_group import (
    GroupCreate,
    GroupMemberOut,
    GroupMemberPage,
    GroupOut,
    GroupSyncResult,
    GroupTenants,
    GroupUpdate,
)
from ...schemas.common import Message
from ...services import audit, group_sync
from ...services.group_sync import GroupSyncError
from ...services.settings_service import SettingsService
from ..deps import SessionDep, SuperadminDefaultContextUser, default_tenant_id

router = APIRouter(prefix="/admin/groups", tags=["admin-groups"])

log = get_logger("admin_groups")

_MAX_PAGE_SIZE = 200


async def _to_out(session: SessionDep, group: AssignmentGroup) -> GroupOut:
    assert group.id is not None  # persistierte Zeile
    return GroupOut(
        id=group.id,
        name=group.name,
        entra_group_id=group.entra_group_id,
        tenant_ids=await assignment_group_repo.list_tenant_ids(session, group.id),
        member_count=await assignment_group_member_repo.count(session, group.id),
        last_synced_at=group.last_synced_at,
    )


async def _auto_sync(session: SessionDep, group_id: int) -> None:
    """Best-effort-Sync nach `create_group`/`set_group_tenants` (Design §5): die primäre
    Mutation ist bereits committet, ein Graph-Fehler hier darf sie NICHT zurückrollen oder
    als 500 durchschlagen -- der Sync ist über den Button jederzeit erneut auslösbar.

    Die Provider-Graph-Config wird über `tenant_scoped_session` auf dem DEFAULT-Tenant
    gelesen (wie `services/instance_settings.read_mode`/`auth.sync_sso`) -- NICHT über die
    rohe Owner-`session`: `setting`s PK ist `(tenant_id, key)`, die Owner-Rolle umgeht RLS,
    also läse `SettingsService(session).get_all()` hier ein undefiniertes Gemisch der
    `graph.*`-Zeilen ALLER Tenants, sobald ein zweiter Kunde Graph konfiguriert. Nur der
    SETTINGS-Read ist tenant-scoped -- der eigentliche `sync_group`-Aufruf (Snapshot- +
    Grant-Schreibzugriffe) bleibt bewusst auf der übergebenen Request-`session`."""
    try:
        async with tenant_scoped_session(await default_tenant_id(session)) as scoped:
            settings = await SettingsService(scoped).get_all()
        await group_sync.sync_group(session, settings, group_id)
        await session.commit()
    except GroupSyncError as exc:
        log.warning("group_auto_sync_failed", group_id=group_id, message=exc.message)
    except Exception:  # pragma: no cover - unerwarteter Fehler, darf primäre Mutation nie stören
        log.exception("group_auto_sync_unexpected_error", group_id=group_id)
        await session.rollback()


@router.get("")
async def list_groups(_: SuperadminDefaultContextUser, session: SessionDep) -> list[GroupOut]:
    groups = await assignment_group_repo.list_all(session)
    return [await _to_out(session, g) for g in groups]


@router.post("", response_model=GroupOut)
async def create_group(
    request: Request,
    admin: SuperadminDefaultContextUser,
    body: GroupCreate,
    session: SessionDep,
) -> GroupOut:
    group = await assignment_group_repo.create(
        session, name=body.name, entra_group_id=body.entra_group_id
    )
    await audit.record(
        session,
        action=audit.GROUP_CREATED,
        actor=admin,
        target=group.name,
        request=request,
        detail={"entra_group_id": group.entra_group_id},
    )
    await session.commit()
    assert group.id is not None
    await _auto_sync(session, group.id)
    return await _to_out(session, group)


@router.put("/{group_id}", response_model=GroupOut)
async def update_group(
    request: Request,
    admin: SuperadminDefaultContextUser,
    group_id: int,
    body: GroupUpdate,
    session: SessionDep,
) -> GroupOut:
    if await assignment_group_repo.get(session, group_id) is None:
        raise NotFoundError("Gruppe nicht gefunden.", code="group_not_found")
    group = await assignment_group_repo.update(session, group_id, name=body.name)
    await audit.record(
        session,
        action=audit.GROUP_UPDATED,
        actor=admin,
        target=group.name,
        request=request,
        detail={"name": group.name},
    )
    await session.commit()
    return await _to_out(session, group)


@router.delete("/{group_id}", response_model=Message)
async def delete_group(
    request: Request,
    admin: SuperadminDefaultContextUser,
    group_id: int,
    session: SessionDep,
) -> Message:
    group = await assignment_group_repo.get(session, group_id)
    if group is None:
        raise NotFoundError("Gruppe nicht gefunden.", code="group_not_found")
    await audit.record(
        session,
        action=audit.GROUP_DELETED,
        actor=admin,
        target=group.name,
        request=request,
        detail={"entra_group_id": group.entra_group_id},
    )
    await session.commit()
    # Reine Zeile -- `assignment_group_tenant` kaskadiert automatisch (ondelete=CASCADE).
    await assignment_group_repo.delete(session, group_id)
    return Message(message="Gruppe gelöscht.")


@router.put("/{group_id}/tenants", response_model=GroupOut)
async def set_group_tenants(
    request: Request,
    admin: SuperadminDefaultContextUser,
    group_id: int,
    body: GroupTenants,
    session: SessionDep,
) -> GroupOut:
    group = await assignment_group_repo.get(session, group_id)
    if group is None:
        raise NotFoundError("Gruppe nicht gefunden.", code="group_not_found")
    await assignment_group_repo.set_tenants(session, group_id, body.tenant_ids)
    await audit.record(
        session,
        action=audit.GROUP_TENANTS_SET,
        actor=admin,
        target=group.name,
        request=request,
        detail={"tenant_ids": sorted(body.tenant_ids)},
    )
    await session.commit()
    await _auto_sync(session, group_id)
    return await _to_out(session, group)


@router.post("/{group_id}/sync", response_model=GroupSyncResult)
async def sync_group_route(
    _: SuperadminDefaultContextUser,
    group_id: int,
    session: SessionDep,
) -> GroupSyncResult:
    # Provider-Graph-Config aus dem DEFAULT-Tenant-Scope lesen (nicht aus der rohen
    # Owner-`session`) -- selbe Begründung wie in `_auto_sync` oben: die Owner-Rolle umgeht
    # RLS, `SettingsService(session).get_all()` läse sonst ein Gemisch der `graph.*`-Zeilen
    # ALLER Tenants. Nur der Settings-Read ist gescoped; Snapshot-/Grant-Schreibzugriffe in
    # `sync_group` bleiben auf der Request-`session`.
    async with tenant_scoped_session(await default_tenant_id(session)) as scoped:
        settings = await SettingsService(scoped).get_all()
    result = await group_sync.sync_group(session, settings, group_id)
    await session.commit()
    return GroupSyncResult(**result)


@router.get("/{group_id}/members", response_model=GroupMemberPage)
async def list_group_members(
    _: SuperadminDefaultContextUser,
    group_id: int,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=25, ge=1, le=_MAX_PAGE_SIZE),
) -> GroupMemberPage:
    if await assignment_group_repo.get(session, group_id) is None:
        raise NotFoundError("Gruppe nicht gefunden.", code="group_not_found")
    rows, total = await assignment_group_member_repo.list_members_page(
        session, group_id, page, size
    )
    return GroupMemberPage(
        items=[
            GroupMemberOut(entra_id=r.entra_id, upn=r.upn, display_name=r.display_name, mail=r.mail)
            for r in rows
        ],
        total=total,
        page=page,
        size=size,
    )
