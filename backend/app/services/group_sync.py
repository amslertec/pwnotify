"""SICHERHEITSKRITISCH (Kronjuwel des Inkrements): der proaktive Entra-Gruppen-Sync.

`sync_group` holt die (transitiven) Mitglieder einer `AssignmentGroup` aus Microsoft Graph,
bringt den lokalen Snapshot (`assignment_group_member`) auf diesen Stand und materialisiert
daraus Kunden-Zugriffe -- ABER ausschliesslich über die bereits sicherheitsgeprüfte
`assignment_group_repo.reconcile_group_grants`. Es gibt in diesem Sync KEINEN eigenen
Grant-Schreibpfad.

WARUM `reconcile_group_grants` wiederverwendet wird (statt eigener Grant-Logik):
    Die harte Isolations-Invariante des Multi-Tenant-Produkts lebt in genau EINER gated
    Codestelle -- der `is_provider_account`-Gate (erste Zeile, fail-closed) von
    `reconcile_group_grants`. Ein kunden-homed oder `tenant_id is None`-Konto erhält damit
    NIEMALS einen Gruppen-Grant, selbst wenn es (etwa durch eine Fehlkonfiguration) im
    Snapshot eines fremden Teams auftaucht. Ein zweiter, geforkter Grant-Pfad hier würde diese
    Garantie duplizieren und könnte davon abdriften -- deshalb: eine Materialisierung, ein Gate.

MATCH-REGEL (UPN -> lokales Konto): `user_repo.get_by_username(session, upn)`, mit der UPN
    EXAKT wie im Snapshot gespeichert (aus Graph `userPrincipalName`). CASE-SENSITIVE -- bewusst
    identisch zum Login-Pfad: der SSO-User-Sync (`services/oidc.py`) matcht ebenfalls über
    `get_by_username` (exakt, `AppUser.username == username`) und speichert den Benutzernamen
    roh (kein Lowercasing). Ein case-insensitiver Match hier würde Grants anders vergeben als
    der Login -- diese Inkonsistenz wäre selbst ein Bug. Ungematchte Mitglieder bleiben nur im
    Snapshot; ihr Zugriff entsteht beim ersten SSO-Login über den unveränderten Login-Reconcile.
    Es werden hier KEINE Konten angelegt.

OLD/NEW-Reconcile-Menge (Vereinigung): Vor dem Snapshot-Reconcile wird die Match-Menge des ALTEN
    dieser Gruppe erfasst, nach dem Reconcile die des NEUEN -- reconciled wird die VEREINIGUNG.
    So wird ein in DIESEM Lauf aus der Gruppe entferntes Mitglied ebenfalls reconciled: seine
    Team-Menge enthält diese Gruppe nicht mehr, sein nun verwaister `source='group'`-Grant wird
    entzogen (ein `source='manual'`-Grant bleibt).

SNAPSHOT ALS QUELLE DER WAHRHEIT (bewusstes Design, kein Defekt): Die Team-Menge eines Kontos
    wird für diesen Sync aus ALLEN lokalen Snapshots abgeleitet
    (`assignment_group_member_repo.groups_containing_upn`), nach dem Reconcile dieser Gruppe.
    Der Login-Reconcile (aus Live-Graph-Claims) bleibt der stets-frische Primärpfad; dieser Sync
    ist die proaktive Ergänzung, die zwischen zwei Logins eines Kontos wirkt.

`transitiveMembers` (aus Task 2): `graph.get_group_members` fragt `transitiveMembers/
    microsoft.graph.user` ab -- verschachtelte Gruppen werden aufgelöst, der OData-Cast
    beschränkt auf echte Benutzerkonten (keine Geräte/Service-Principals).

COMMIT: kein eigenes `session.commit()` -- der Aufrufer (Task-4-Route) committet die
    Transaktion. Die einzeln genutzten `add_grant`/`remove_grant` in `reconcile_group_grants`
    committen bereits je Zeile (gleiche Transaktionssemantik wie der Login-Pfad).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import GraphError, NotFoundError, PwNotifyError
from ..models._base import utcnow
from ..models.assignment_group import AssignmentGroup
from ..repositories import assignment_group_member_repo as member_repo
from ..repositories import assignment_group_repo, tenant_repo, user_repo
from . import audit
from .graph.client import GraphClient, GraphConfig


class GroupSyncError(PwNotifyError):
    """Ein Gruppen-Sync ist an einem erwartbaren, upstream-bedingten Fehler gescheitert
    (Gruppe in Graph nicht gefunden, fehlende Berechtigung, Transportfehler). Typisiert und
    message-tragend, damit die Route sie sauber als `sync_failed`-Antwort rendert statt als
    unbehandelten 500er -- Snapshot und Grants bleiben unverändert."""

    status_code = 502
    code = "sync_failed"


async def sync_group(
    session: AsyncSession, settings: dict[str, Any], group_id: int
) -> dict[str, int]:
    """Synchronisiert eine `AssignmentGroup` (Snapshot + Grant-Materialisierung).

    Gibt `{member_count, materialized, added, removed}` zurück:
    - `member_count`: Grösse des neuen Snapshots (Mitglieder nach dem Reconcile),
    - `materialized`: Anzahl der reconciled PROVIDER-Konten (Kunden-/NULL-Home-Matches sind
      per Gate ein No-Op und zählen NICHT),
    - `added`/`removed`: Snapshot-Diff gegen den vorherigen Stand.
    """
    group = await session.get(AssignmentGroup, group_id)
    if group is None:
        raise NotFoundError("Gruppe nicht gefunden.", code="group_not_found")

    graph = GraphClient(
        GraphConfig(
            tenant_id=settings.get("graph.tenant_id") or "",
            client_id=settings.get("graph.client_id") or "",
            client_secret=settings.get("graph.client_secret") or "",
            cloud=settings.get("graph.cloud") or "global",
        )
    )

    # Graph-Fehler NIE als 500 durchreichen -- Snapshot/Grants bleiben unangetastet.
    try:
        members = await graph.get_group_members(group.entra_group_id)
    except GraphError as exc:
        raise GroupSyncError(
            f"Der Gruppen-Sync ist fehlgeschlagen: {exc.message}", code="sync_failed"
        ) from exc
    except Exception as exc:  # Transport/unerwartet -> ebenfalls sauber typisiert
        raise GroupSyncError(
            f"Der Gruppen-Sync ist fehlgeschlagen: {exc}", code="sync_failed"
        ) from exc

    # (1) OLD-Matchmenge dieser Gruppe VOR dem Snapshot-Reconcile erfassen.
    old_upns = await member_repo.upns_for_group(session, group_id)

    # (2) Snapshot auf die gefetchte Menge bringen; Sync-Zeitstempel der Gruppe setzen.
    recon = await member_repo.reconcile_snapshot(session, group_id, members)
    group.last_synced_at = utcnow()

    # (3) NEW-Matchmenge NACH dem Reconcile; reconciled wird die VEREINIGUNG (OLD und NEW),
    # damit auch ein in diesem Lauf entferntes Mitglied seinen verwaisten Grant verliert.
    new_upns = await member_repo.upns_for_group(session, group_id)

    materialized = 0
    for upn in old_upns | new_upns:
        account = await user_repo.get_by_username(session, upn)  # exakt/case-sensitive
        if account is None:
            continue  # Ungematcht -> nur Snapshot, kein Grant, keine Kontoanlage.
        # Team-Menge rein aus lokalen Snapshots (post-reconcile), 1:1 an das vetted Reconcile.
        team = await member_repo.groups_containing_upn(session, upn)
        # Reconcile ist jetzt ROLLEN-BEWUSST: die Rolle jedes Teams entscheidet die Zieltabelle
        # seiner Kunden (Admin-Team -> admin_tenant, Auditor-Team -> auditor_tenant, Admin
        # gewinnt) -- der Sync erbt das unverändert, kein eigener Grant-Schreibpfad.
        await assignment_group_repo.reconcile_group_grants(session, account, list(team))
        # `materialized` zählt die effektiv wirksamen Provider-Matches -- der Gate-Entscheid
        # bleibt allein in `reconcile_group_grants`; diese Prüfung ist nur fürs Zählen.
        if await tenant_repo.is_provider_account(session, account):
            materialized += 1

    result = {
        "member_count": recon["total"],
        "materialized": materialized,
        "added": recon["added"],
        "removed": recon["removed"],
    }

    await audit.record(
        session,
        action=audit.GROUP_SYNCED,
        actor_type="system",
        target=group.name,
        detail={
            "member_count": result["member_count"],
            "materialized": result["materialized"],
            "added": result["added"],
            "removed": result["removed"],
        },
    )
    return result
