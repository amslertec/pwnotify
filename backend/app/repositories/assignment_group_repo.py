"""DB-Zugriff für Assignment-Groups (Entra-Security-Gruppe -> ein oder mehrere Kunden,
Console+Groups+Invite-Phase Task 3).

Läuft wie `tenant_repo` auf der OWNER-Session -- `assignment_group`/`assignment_group_tenant`
sind instanzweite Tabellen, keine RLS-tenant-gescopten Kundendaten.

`entra_group_id` ist in diesem Inkrement FREI-TEXT (Design §7) -- keine Graph-Validierung,
kein Picker; die einzige Prüfung ist Eindeutigkeit (DB-Unique-Index + Vorab-Check hier,
gleiches Muster wie `tenant_repo.create`s Slug-/Entra-Tenant-Id-Prüfung).

`tenant_ids_for_entra_groups` ist für Task 4 (Login-Reconcile) bestimmt: EIN Join-Query,
das die Vereinigung aller Kunden liefert, auf die IRGENDEINE der übergebenen Entra-Gruppen
gemappt ist -- Task 4 ruft das mit den `groups`-Claims eines SSO-Logins auf und muss die
Kandidatenmenge für den group-basierten Grant-Reconcile kennen.
"""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ConflictError, NotFoundError
from ..models.assignment_group import AssignmentGroup, AssignmentGroupTenant
from ..models.tenant import Tenant


async def get_by_entra_group_id(
    session: AsyncSession, entra_group_id: str
) -> AssignmentGroup | None:
    """Eindeutigkeits-Vorab-Check für `create` -- gleiches Muster wie
    `tenant_repo._get_by_entra_tid_any`."""
    res = await session.execute(
        select(AssignmentGroup).where(AssignmentGroup.entra_group_id == entra_group_id)
    )
    return res.scalar_one_or_none()


async def create(session: AsyncSession, *, name: str, entra_group_id: str) -> AssignmentGroup:
    if await get_by_entra_group_id(session, entra_group_id) is not None:
        raise ConflictError("Diese Entra-Gruppen-ID wird bereits verwendet.", code="group_exists")
    group = AssignmentGroup(name=name, entra_group_id=entra_group_id)
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return group


async def list_all(session: AsyncSession) -> list[AssignmentGroup]:
    res = await session.execute(select(AssignmentGroup).order_by(AssignmentGroup.name))
    return list(res.scalars().all())


async def get(session: AsyncSession, group_id: int) -> AssignmentGroup | None:
    return await session.get(AssignmentGroup, group_id)


async def update(session: AsyncSession, group_id: int, *, name: str) -> AssignmentGroup:
    """Reine Umbenennung -- `entra_group_id` ist unveränderlich (kein Feld in `GroupUpdate`),
    die Route lehnt Unbekanntes vorab mit `group_not_found` ab (404), diese Funktion bleibt
    aus Konsistenzgründen zu `tenant_repo.update` trotzdem defensiv."""
    group = await session.get(AssignmentGroup, group_id)
    if group is None:
        raise NotFoundError("Gruppe nicht gefunden.", code="group_not_found")
    group.name = name
    await session.commit()
    await session.refresh(group)
    return group


async def delete(session: AsyncSession, group_id: int) -> None:
    """Nur die reine Zeile -- `assignment_group_tenant` kaskadiert automatisch über
    `ondelete=CASCADE` (Migration 5d152bfe7585), kein manuelles Aufräumen nötig."""
    group = await session.get(AssignmentGroup, group_id)
    if group is not None:
        await session.delete(group)
        await session.commit()


async def list_tenant_ids(session: AsyncSession, group_id: int) -> list[int]:
    res = await session.execute(
        select(AssignmentGroupTenant.tenant_id)
        .where(AssignmentGroupTenant.assignment_group_id == group_id)
        .order_by(AssignmentGroupTenant.tenant_id)
    )
    return list(res.scalars().all())


async def set_tenants(session: AsyncSession, group_id: int, tenant_ids: list[int]) -> None:
    """Reconciled die Kunden-Mitgliedschaft einer Gruppe auf EXAKT `tenant_ids` -- Diff gegen
    den aktuellen Bestand, genau wie `admin_assignments.set_assignments` es für Konto-Zu-
    weisungen tut. Jede Ziel-Id muss ein AKTIVER Tenant sein (dieselbe Regel wie dort) --
    sonst `ConflictError(code="tenant_not_active")`, VOR jeder Schreiboperation geprüft,
    damit keine Teilmenge geschrieben wird, bevor eine ungültige Id auffällt."""
    requested = set(tenant_ids)
    for tid in requested:
        tenant = await session.get(Tenant, tid)
        if tenant is None or not tenant.is_active:
            raise ConflictError(
                "Nur aktive Mandanten können zugeordnet werden.", code="tenant_not_active"
            )

    existing = set(await list_tenant_ids(session, group_id))
    to_add = requested - existing
    to_remove = existing - requested

    for tid in sorted(to_add):
        session.add(AssignmentGroupTenant(assignment_group_id=group_id, tenant_id=tid))
    if to_remove:
        await session.execute(
            sa_delete(AssignmentGroupTenant).where(
                AssignmentGroupTenant.assignment_group_id == group_id,
                AssignmentGroupTenant.tenant_id.in_(to_remove),
            )
        )
    await session.commit()


async def tenant_ids_for_entra_groups(session: AsyncSession, entra_group_ids: set[str]) -> set[int]:
    """Vereinigung aller Kunden, auf die IRGENDEINE der übergebenen Entra-Gruppen gemappt
    ist -- EIN Join-Query (Task 4's Login-Reconcile ruft das mit den `groups`-Claims eines
    SSO-Tokens auf). Leere Eingabe oder unbekannte Entra-Ids liefern eine leere Menge, kein
    Fehler -- ein Login ohne (bekannte) Gruppen-Mitgliedschaft ist kein Fehlerfall."""
    if not entra_group_ids:
        return set()
    res = await session.execute(
        select(AssignmentGroupTenant.tenant_id)
        .join(
            AssignmentGroup,
            AssignmentGroup.id == AssignmentGroupTenant.assignment_group_id,
        )
        .where(AssignmentGroup.entra_group_id.in_(entra_group_ids))
        .distinct()
    )
    return set(res.scalars().all())
