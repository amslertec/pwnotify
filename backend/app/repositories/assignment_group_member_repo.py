"""DB-Zugriff für den Mitglieder-Snapshot einer `AssignmentGroup` (Gruppen-Sync, Task 3).

Läuft wie `assignment_group_repo` auf der OWNER-Session -- `assignment_group_member` ist
eine instanzweite Tabelle, keine RLS-tenant-gescopten Kundendaten.

`groups_containing_upn` ist der Kern-Helper des Gruppen-Syncs: Er leitet die Team-Menge eines
Kontos AUSSCHLIESSLICH aus den lokalen Snapshots ab (nicht aus Live-Graph-Claims), damit der
proaktive Sync dieselbe Grant-Materialisierung anstossen kann wie der Login-Reconcile -- die
gefundene Menge wird UNVERÄNDERT an `assignment_group_repo.reconcile_group_grants` gereicht,
das seine `is_provider_account`-Gate selbst zieht. Dieses Modul trifft KEINE Grant-Entscheidung.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models._base import utcnow
from ..models.assignment_group import AssignmentGroup
from ..models.assignment_group_member import AssignmentGroupMember


async def entra_ids_for_group(session: AsyncSession, group_id: int) -> set[str]:
    res = await session.execute(
        select(AssignmentGroupMember.entra_id).where(
            AssignmentGroupMember.assignment_group_id == group_id
        )
    )
    return set(res.scalars().all())


async def upns_for_group(session: AsyncSession, group_id: int) -> set[str]:
    """Alle UPNs im aktuellen Snapshot einer Gruppe -- der Sync bildet daraus die OLD- bzw.
    NEW-Matchmenge (vor/nach dem `reconcile_snapshot`)."""
    res = await session.execute(
        select(AssignmentGroupMember.upn).where(
            AssignmentGroupMember.assignment_group_id == group_id
        )
    )
    return set(res.scalars().all())


async def groups_containing_upn(session: AsyncSession, upn: str) -> set[str]:
    """Die Team-Menge eines Kontos: die `entra_group_id`-Werte JEDER `assignment_group`, deren
    Snapshot diese UPN enthält (EIN Join `assignment_group_member` -> `assignment_group`).

    Rein aus lokalen Snapshots abgeleitet -- diese Menge wird 1:1 als `entra_group_ids` an
    `reconcile_group_grants` gereicht. Nach dem Reconcile DIESER Gruppe im selben Sync spiegelt
    sie die frische Mitgliedschaft: Ein aus der Gruppe entferntes Mitglied ist hier nicht mehr
    enthalten, seine `source='group'`-Grants dieser Gruppe fallen beim Reconcile weg."""
    res = await session.execute(
        select(AssignmentGroup.entra_group_id)
        .join(
            AssignmentGroupMember,
            AssignmentGroupMember.assignment_group_id == AssignmentGroup.id,
        )
        .where(AssignmentGroupMember.upn == upn)
        .distinct()
    )
    return set(res.scalars().all())


async def reconcile_snapshot(
    session: AsyncSession, group_id: int, members: list[dict[str, Any]]
) -> dict[str, int]:
    """Bringt den Snapshot einer Gruppe auf EXAKT die gefetchte Mitgliedermenge.

    Upsert je Mitglied auf `(assignment_group_id, entra_id)` (aktualisiert
    `upn/display_name/mail/synced_at`), DELETE aller Snapshot-Zeilen der Gruppe, deren
    `entra_id` nicht mehr in der Fetch-Menge ist. Kein eigenes `commit` -- der aufrufende
    `group_sync.sync_group`/die Route committet die Transaktion (die Grant-Reconciles
    committen ausserdem je Zeile, gleiche Semantik wie der Login-Pfad).

    Gibt `{added, removed, total}` zurück (added/removed = Diff gegen den bisherigen Snapshot,
    total = Grösse des neuen Snapshots)."""
    now = utcnow()
    existing = await entra_ids_for_group(session, group_id)

    incoming: dict[str, dict[str, Any]] = {}
    for m in members:
        eid = m.get("id")
        if eid:
            incoming[str(eid)] = m

    incoming_ids = set(incoming)
    added = incoming_ids - existing
    removed = existing - incoming_ids

    for eid, m in incoming.items():
        upn = m.get("userPrincipalName") or ""
        display_name = m.get("displayName")
        mail = m.get("mail")
        stmt = (
            pg_insert(AssignmentGroupMember.__table__)
            .values(
                assignment_group_id=group_id,
                entra_id=eid,
                upn=upn,
                display_name=display_name,
                mail=mail,
                synced_at=now,
            )
            .on_conflict_do_update(
                index_elements=["assignment_group_id", "entra_id"],
                set_={
                    "upn": upn,
                    "display_name": display_name,
                    "mail": mail,
                    "synced_at": now,
                },
            )
        )
        await session.execute(stmt)

    if removed:
        await session.execute(
            sa_delete(AssignmentGroupMember).where(
                AssignmentGroupMember.assignment_group_id == group_id,
                AssignmentGroupMember.entra_id.in_(removed),
            )
        )

    return {"added": len(added), "removed": len(removed), "total": len(incoming_ids)}


async def count(session: AsyncSession, group_id: int) -> int:
    res = await session.execute(
        select(func.count(AssignmentGroupMember.id)).where(
            AssignmentGroupMember.assignment_group_id == group_id
        )
    )
    return int(res.scalar_one())


async def list_members_page(
    session: AsyncSession, group_id: int, page: int, size: int
) -> tuple[list[AssignmentGroupMember], int]:
    """Eine (1-basierte) Seite des Snapshots plus Gesamtzahl -- für die Gruppen-Detail-API
    (Task 4), stabil nach `upn` sortiert."""
    total = await count(session, group_id)
    offset = max(page - 1, 0) * size
    res = await session.execute(
        select(AssignmentGroupMember)
        .where(AssignmentGroupMember.assignment_group_id == group_id)
        .order_by(AssignmentGroupMember.upn)
        .offset(offset)
        .limit(size)
    )
    return list(res.scalars().all()), total
