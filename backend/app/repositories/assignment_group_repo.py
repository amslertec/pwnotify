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
from ..models.user import AppUser
from . import tenant_repo
from .tenant_repo import _grant_kind


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


async def reconcile_group_grants(
    session: AsyncSession, user: AppUser, entra_group_ids: list[str] | None
) -> None:
    """SICHERHEITSKRITISCH (Kronjuwel des Inkrements): materialisiert `source='group'`-Grants
    für ein PROVIDER-Konto anhand seiner Entra-Gruppen-Mitgliedschaften beim SSO-Login. Wird
    aus `auth.oidc_callback` bei JEDEM SSO-Login gerufen.

    Zwei harte Isolations-Invarianten, die dieser Reconcile NIE verletzen darf:
    1. **Ein Kunden-homed Konto (oder `tenant_id is None`) erhält NIEMALS einen Gruppen-Grant.**
       Die `is_provider_account`-Prüfung ist die ERSTE Zeile (fail-closed), VOR jedem DB-Lese-
       oder Schreibzugriff -- ein SSO-Konto eines Kunden hat als Heim-Tenant seinen Kunden,
       nicht den Default-Tenant, und wird hier sofort no-op abgewiesen, selbst wenn sein
       `groups`-Claim auf fremde Teams zeigt.
    2. **Ein `source='manual'`-Grant wird NIE angefasst** (nicht konvertiert, nicht gelöscht).
       Ein manueller Grant vom Superadmin gewinnt und bleibt `source='manual'`; deckt eine
       Gruppe denselben Tenant ab, wird KEINE zweite Zeile angelegt (Composite-PK, außerdem
       explizit aus der Add-Menge ausgeschlossen), und der Remove-Zweig entfernt nur
       `source='group'`-Zeilen.

    **Rollen-Flip-Sicherheit (Sicherheitsreview Task 4, "Important"-Finding):** Gruppen-Grants
    werden IMMER NUR in der Zieltabelle der AKTUELLEN Rolle (`kind = _grant_kind(user.role)`)
    materialisiert -- niemals in beiden. Flippt die Entra-Rolle eines Provider-Kontos zwischen
    zwei Logins (admin->auditor oder umgekehrt), würde ein Reconcile, der nur `kind` anfasst,
    eine ALTE `source='group'`-Zeile der VORHERIGEN Rolle dauerhaft verwaist zurücklassen (z.B.
    bliebe `admin_tenant(A, group)` nach einer Demotion zu `auditor` bestehen -- der Auditor
    behielte damit stillschweigend Schreibzugriff). Deshalb räumt dieser Reconcile bei JEDEM
    Aufruf ZUSÄTZLICH die ANDERE Zieltabelle auf: jede `source='group'`-Zeile des Kontos in
    `other_kind` (der Kind, der NICHT der aktuellen Rolle entspricht) wird entfernt. `source=
    'manual'`-Zeilen in `other_kind` bleiben unangetastet (Invariante 2 gilt auch dort -- ein
    manueller Grant der anderen Kapazität ist Sache der Zuweisungs-API/`set_role`, nicht dieses
    Reconciles). Ergebnis-Garantie nach jedem Reconcile: das Konto hat Gruppen-Grants
    AUSSCHLIESSLICH in der Zieltabelle der aktuellen Rolle, und ZERO Gruppen-Grants in der
    anderen.

    `entra_group_ids` falsy (kein Team) -> leere Wunschmenge -> alle bestehenden
    `source='group'`-Zeilen des Kontos (in `kind`) werden entfernt (ein Provider, der jedes Team
    verlassen hat, behält nur seine manuellen Grants). Inaktive Ziel-Tenants werden beim Anlegen
    still übersprungen (Verfügbarkeit, kein Sicherheitsproblem) -- eine bestehende Gruppen-Zeile
    auf einen zwischenzeitlich deaktivierten Tenant bleibt hingegen, solange die Gruppe ihn noch
    abbildet (kein Aktiv-Filter auf der Ist-Menge).

    Kein eigenes `commit`: die einzeln genutzten `add_grant`/`remove_grant` committen bereits
    (wie in `admin_assignments`); der Aufrufer (`oidc_callback`) committt die Login-Transaktion
    ohnehin danach. Es wird auf DERSELBEN `session` gearbeitet."""
    # (1) GATE FIRST -- fail-closed, MUSS vor jedem DB-Zugriff stehen.
    if not await tenant_repo.is_provider_account(session, user):
        return
    assert user.id is not None  # Provider-Konto hat tenant_id gesetzt => persistierte Zeile

    desired = await tenant_ids_for_entra_groups(session, set(entra_group_ids or []))
    kind = _grant_kind(user.role)
    other_kind = "auditor" if kind == "admin" else "admin"
    rows = await tenant_repo.list_grant_rows(session, user.id, kind)
    group_now = {tid for tid, src in rows if src == "group"}
    manual_now = {tid for tid, src in rows if src == "manual"}

    # (2) Add mit Manual-Vorrang: Tenants, die bereits eine MANUELLE Zeile haben, werden
    # übersprungen -- die manuelle Zeile bleibt `source='manual'`, es entsteht keine Dublette.
    for tid in desired - group_now - manual_now:
        if await tenant_repo._is_active(session, tid):
            await tenant_repo.add_grant(
                session, user_id=user.id, tenant_id=tid, kind=kind, source="group"
            )

    # Remove: nur `source='group'`-Zeilen, die kein Team mehr abbildet. `manual_now` ist nie
    # in `group_now` (eine Zeile pro Paar), also werden manuelle Grants hier nie berührt.
    for tid in group_now - desired:
        await tenant_repo.remove_grant(session, user_id=user.id, tenant_id=tid, kind=kind)

    # (3) ROLLEN-FLIP: verwaiste `source='group'`-Zeilen der ANDEREN Zieltabelle aufräumen.
    # Nur `source='group'` -- eine manuelle Zeile in `other_kind` gehört nicht in dieses
    # Reconcile und bleibt unangetastet.
    other_rows = await tenant_repo.list_grant_rows(session, user.id, other_kind)
    for tid, src in other_rows:
        if src == "group":
            await tenant_repo.remove_grant(session, user_id=user.id, tenant_id=tid, kind=other_kind)
