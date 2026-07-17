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

from fastapi import APIRouter, Request

from ...core.errors import NotFoundError
from ...models.assignment_group import AssignmentGroup
from ...repositories import assignment_group_repo
from ...schemas.assignment_group import GroupCreate, GroupOut, GroupTenants, GroupUpdate
from ...schemas.common import Message
from ...services import audit
from ..deps import SessionDep, SuperadminDefaultContextUser

router = APIRouter(prefix="/admin/groups", tags=["admin-groups"])


async def _to_out(session: SessionDep, group: AssignmentGroup) -> GroupOut:
    assert group.id is not None  # persistierte Zeile
    return GroupOut(
        id=group.id,
        name=group.name,
        entra_group_id=group.entra_group_id,
        tenant_ids=await assignment_group_repo.list_tenant_ids(session, group.id),
    )


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
    return await _to_out(session, group)
