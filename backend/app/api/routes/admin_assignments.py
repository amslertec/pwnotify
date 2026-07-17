"""Zuweisungs-API (Access-Modell/Superadmin-Phase, Task 4): welchen (aktiven) Mandanten
darf ein Admin/Auditor-Konto zusätzlich zu seinem Heim-Tenant verwalten/einsehen --
SUPERADMIN-only auf ALLEN Routen (`SuperadminUser`, Design §4/§6). Seit Context-Gating v2
(Matrix B) zusätzlich nur im DEFAULT-Kontext (`SuperadminDefaultContextUser`,
`default_context_required`) -- die Zuweisungs-Konsole ist Provider-Ebene und aus einem
Kunden-Kontext heraus gesperrt, genau wie die Instanz- und Mandanten-Konsole.

**Kernentscheidung (bewusste Abweichung vom Task-4-Brief):** der Zuweisungstyp
(`admin_tenant` vs. `auditor_tenant`) wird NICHT vom Aufrufer per Dual-Liste
(`{admin:[...], auditor:[...]}`) gewählt, sondern strukturell aus der ROLLE des
Zielkontos abgeleitet -- `role=='admin'` -> `admin_tenant` (Schreib-Kapazität),
sonst (`auditor`) -> `auditor_tenant` (nur lesend). Ein frei wählbarer Grant-Typ hätte
einem `role=='admin'`-Konto erlaubt, NUR eine `auditor_tenant`-Zuweisung zu erhalten --
über das Rollen-Gate (`require_admin`) hätte es dort trotzdem SCHREIBEND agieren dürfen,
obwohl die Zuweisung selbst nur Lesen hergeben sollte (dieselbe Fehlerklasse, die
`admin_users.create_local`, Task 3, für die Kontoanlage bereits schliesst -- diese Route
schliesst sie für die NACHTRÄGLICHE Zuweisung).

Ein Superadmin-Zielkonto ist NIE zuweisbar (er sieht ohnehin alle aktiven Tenants,
`tenant_repo.allowed_tenant_ids`) -- `PUT` lehnt das hart ab, `GET` liefert defensiv eine
leere Liste statt eines Fehlers (reiner Lesezugriff, nichts, worüber man reconcilen müsste).

**Cross-Grant-Lock (Task 2, Kronjuwel dieser Route):** `set_assignments` prüft zusätzlich
`tenant_repo.is_provider_account(session, target)` -- ein Kunden-homed Konto (Heim-Tenant
ist NICHT der Default-Tenant, oder `tenant_id is None`) darf NUR noch auf seinen EIGENEN
Heim-Tenant berechtigt werden, niemals auf einen fremden. Bewusst geprüft wird der HEIM-
Tenant (`AppUser.tenant_id`), NICHT die Rolle: die Rolle (`admin`/`auditor`) sagt nur, welche
KAPAZITÄT eine Zuweisung verleiht (Schreiben vs. Lesen, s. `_grant_kind` oben) -- sie sagt
nichts darüber aus, ob das Konto überhaupt ein Provider- oder ein Kunden-Konto ist. Ein
Kunden-homed `admin` und ein Kunden-homed `auditor` sind gleichermassen cross-grant-gesperrt;
nur die Heimat entscheidet, nicht die Rolle. Der Default-Tenant ist die bewusste Ausnahme:
NUR seine (Provider-)Konten darf der Superadmin auf beliebige weitere aktive Tenants
berechtigen -- das ist der eigentliche Zweck dieser Route (der IT-Dienstleister betreut
mehrere Kunden). Jedes andere Konto ist strukturell un-cross-grantable, selbst durch den
Superadmin -- RLS und `tenant_repo.is_allowed` bleiben die Backstop-Ebene, dieser Lock ist
die API-seitige Durchsetzung, BEVOR überhaupt eine Zuweisungszeile geschrieben wird.

**Bulk-Zuweisung (`PUT /bulk`, Task 2 der Console+Groups+Invite-Phase):** `bulk_assign`
wendet den Cross-Grant-Lock auf JEDES Konto der Charge über EXAKT denselben Codepfad an
wie `set_assignments` (Provider-Prüfung, `requested <= allowed`) -- absichtlich NICHT
dupliziert/neu geschrieben. Eine Sicherheitsinvariante, die nur an EINER Stelle geprüft
wird, kann nicht durch zwei leicht abweichende Implementierungen auseinanderlaufen; ein
zweiter, "ähnlicher" Lock-Check wäre genau die Art Duplikat, die künftige Änderungen an
einer Stelle vergisst, an der anderen nachzuziehen.

Zwei UNTERSCHIEDLICHE Fehlerklassen, bewusst nicht symmetrisch behandelt:
- Ein cross-grant-gesperrtes, ein unbekanntes (`user_not_found`) oder ein Superadmin-Ziel
  (`cannot_assign_superadmin`) ist eine PRO-KONTO-Policy-Entscheidung -- die Charge enthält
  typischerweise viele Konten, und ein einzelnes gesperrtes/falsches Konto soll die übrigen,
  legitimen Reconciles nicht verhindern. Diese Konten werden übersprungen (`skipped`,
  s. `schemas/assignment.py`), es wird für sie NICHTS geschrieben, der Rest der Charge läuft
  normal durch.
- Eine unbekannte/inaktive `tenant_id` in `body.tenant_ids` ist dagegen ein AUFRUFER-Fehler
  (derselbe Fehlerfall wie `set_assignments`' `tenant_not_active`) -- er betrifft die ganze
  Anfrage, nicht ein einzelnes Konto, und wird deshalb VORAB (vor jeder Iteration über
  `user_ids`) hart geprüft: die gesamte Anfrage schlägt fehl, BEVOR irgendeine Zeile für
  irgendein Konto geschrieben wurde. Alles andere würde einen Teil-Erfolg quer über die
  Charge erzeugen, der von einem simplen Tippfehler in `tenant_ids` abhinge.

`add_grant(..., source="manual")`: eine Bulk-Zuweisung ist eine explizite Admin-Aktion
(genau wie die Einzel-Zuweisung über `set_assignments`) -- `"manual"` stellt sicher, dass
ein künftiger Assignment-Group-Reconcile (`source="group"`) diese Zeile respektiert und
nicht überschreibt/entfernt.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...models.user import AppUser
from ...repositories import tenant_repo, user_repo
from ...schemas.assignment import (
    AssignmentOut,
    AssignmentUpdate,
    BulkAssignmentResult,
    BulkAssignmentUpdate,
    SkippedUser,
)
from ...services import audit
from ..deps import SessionDep, SuperadminDefaultContextUser

router = APIRouter(prefix="/admin/assignments", tags=["admin-assignments"])


def _grant_kind(role: str) -> str:
    """Grant-Typ aus der Rolle des Zielkontos -- die einzige Stelle, die diese Abbildung
    trifft (siehe Moduldoku: Kern der Task-4-Abweichung)."""
    return "admin" if role == "admin" else "auditor"


async def _cross_grant_lock_allows(
    session: AsyncSession, target: AppUser, requested: set[int]
) -> bool:
    """DIE eine Stelle, an der der Cross-Grant-Lock ausgewertet wird -- von
    `set_assignments` UND `bulk_assign` gerufen (Moduldoku "Bulk-Zuweisung"), damit die
    Sicherheitsinvariante strukturell NICHT auseinanderlaufen kann (keine zwei parallelen
    Kopien derselben Prüfung, die eine Änderung an der einen Stelle vergessen könnte, an
    der anderen nachzuziehen).

    Provider-Konto: uneingeschränkt (`True`). Kunden-homed Konto (oder `tenant_id is
    None`): nur erlaubt, wenn `requested` Teilmenge des eigenen Heim-Tenants ist (leere
    Menge, falls kein Heim-Tenant)."""
    if await tenant_repo.is_provider_account(session, target):
        return True
    allowed = {target.tenant_id} if target.tenant_id is not None else set()
    return requested <= allowed


@router.get("/{user_id}", response_model=AssignmentOut)
async def get_assignments(
    _: SuperadminDefaultContextUser, user_id: int, session: SessionDep
) -> AssignmentOut:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    if target.role == "superadmin":
        # Instanzweit, keine Zuweisungszeile relevant -- nichts anzuzeigen, kein Fehler.
        return AssignmentOut(role=target.role, tenant_ids=[])
    kind = _grant_kind(target.role)
    ids = await tenant_repo.list_grant_tenant_ids(session, user_id, kind)
    return AssignmentOut(role=target.role, tenant_ids=sorted(ids))


@router.put("/bulk", response_model=BulkAssignmentResult)
async def bulk_assign(
    request: Request,
    admin: SuperadminDefaultContextUser,
    body: BulkAssignmentUpdate,
    session: SessionDep,
) -> BulkAssignmentResult:
    """Reconciled `body.tenant_ids` gegen JEDES Konto in `body.user_ids` -- pro Konto EXAKT
    dieselbe Logik wie `set_assignments` (s. Moduldoku "Bulk-Zuweisung" oben für die
    Skip-statt-Fehlschlag- vs. Hart-Fehlschlag-Abgrenzung).

    **Route-Reihenfolge (Sicherheitsrelevant):** MUSS vor `set_assignments`
    (`PUT /{user_id}`) registriert sein -- sonst versucht Starlette, das literale
    Pfadsegment `"bulk"` als `{user_id}: int` zu parsen, und liefert 422 statt diese Route
    zu erreichen."""
    # Aufrufer-Fehler zuerst und VOLLSTÄNDIG vorab geprüft (s. Moduldoku): eine unbekannte/
    # inaktive Tenant-Id lehnt die GESAMTE Anfrage ab, bevor auch nur ein Konto der Charge
    # angefasst wird -- kein Teil-Schreiben quer über die Charge.
    for tid in set(body.tenant_ids):
        tenant = await tenant_repo.get(session, tid)
        if tenant is None or not tenant.is_active:
            raise ConflictError(
                "Nur aktive Mandanten können zugewiesen werden.", code="tenant_not_active"
            )

    tenant_ids = set(body.tenant_ids)
    updated: list[int] = []
    skipped: list[SkippedUser] = []

    for user_id in body.user_ids:
        target = await user_repo.get(session, user_id)
        if target is None:
            skipped.append(SkippedUser(user_id=user_id, reason="user_not_found"))
            continue
        if target.role == "superadmin":
            skipped.append(SkippedUser(user_id=user_id, reason="cannot_assign_superadmin"))
            continue
        kind = _grant_kind(target.role)
        existing = set(await tenant_repo.list_grant_tenant_ids(session, user_id, kind))
        if body.action == "add":
            requested = existing | tenant_ids
        elif body.action == "remove":
            requested = existing - tenant_ids
        else:
            requested = set(tenant_ids)

        if not await _cross_grant_lock_allows(session, target, requested):
            # `_cross_grant_lock_allows` ist DIESELBE Prüfung wie in `set_assignments` --
            # s. Moduldoku, EIN Codepfad für die Invariante. Skip statt Fehlschlag: nur
            # DIESES Konto bleibt unangetastet, der Rest der Charge läuft weiter.
            skipped.append(SkippedUser(user_id=user_id, reason="customer_account_not_grantable"))
            continue

        to_add = requested - existing
        to_remove = existing - requested
        for tid in sorted(to_add):
            await tenant_repo.add_grant(
                session, user_id=user_id, tenant_id=tid, kind=kind, source="manual"
            )
            await audit.record(
                session,
                action=audit.TENANT_ASSIGNED,
                actor=admin,
                target=target.username,
                request=request,
                detail={"tenant_id": tid, "kind": kind},
            )
        for tid in sorted(to_remove):
            await tenant_repo.remove_grant(session, user_id=user_id, tenant_id=tid, kind=kind)
            await audit.record(
                session,
                action=audit.TENANT_UNASSIGNED,
                actor=admin,
                target=target.username,
                request=request,
                detail={"tenant_id": tid, "kind": kind},
            )
        updated.append(user_id)

    await session.commit()
    return BulkAssignmentResult(updated=updated, skipped=skipped)


@router.put("/{user_id}", response_model=AssignmentOut)
async def set_assignments(
    request: Request,
    admin: SuperadminDefaultContextUser,
    user_id: int,
    body: AssignmentUpdate,
    session: SessionDep,
) -> AssignmentOut:
    """Reconciled die Zuweisungen von `user_id` auf exakt `body.tenant_ids` -- Diff gegen
    den aktuellen Bestand (`tenant_repo.list_grant_tenant_ids`), Add/Remove-Delta, jede
    Änderung einzeln auditiert (Design: Nachvollziehbarkeit pro Tenant, nicht nur "geändert")."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    if target.role == "superadmin":
        raise ConflictError(
            "Superadmins sehen bereits alle Mandanten -- keine Zuweisung nötig.",
            code="cannot_assign_superadmin",
        )
    kind = _grant_kind(target.role)
    requested = set(body.tenant_ids)

    if not await _cross_grant_lock_allows(session, target, requested):
        # Kunden-homed Konto (s. Moduldoku "Cross-Grant-Lock"): die einzig erlaubte
        # Zuweisung ist der eigene Heim-Tenant -- jede Fremd-Id in `requested` lehnt die
        # GESAMTE Anfrage ab, bevor irgendeine Zuweisungszeile geschrieben wird.
        # `_cross_grant_lock_allows` ist dieselbe Prüfung, die `bulk_assign` pro Konto
        # ruft (s. Moduldoku "Bulk-Zuweisung") -- EIN Codepfad für die Invariante.
        raise ForbiddenError(
            "Kunden-Konten können nicht auf fremde Mandanten berechtigt werden.",
            code="customer_account_not_grantable",
        )

    for tid in requested:
        tenant = await tenant_repo.get(session, tid)
        if tenant is None or not tenant.is_active:
            raise ConflictError(
                "Nur aktive Mandanten können zugewiesen werden.", code="tenant_not_active"
            )

    existing = set(await tenant_repo.list_grant_tenant_ids(session, user_id, kind))
    to_add = requested - existing
    to_remove = existing - requested

    for tid in sorted(to_add):
        await tenant_repo.add_grant(session, user_id=user_id, tenant_id=tid, kind=kind)
        await audit.record(
            session,
            action=audit.TENANT_ASSIGNED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"tenant_id": tid, "kind": kind},
        )
    for tid in sorted(to_remove):
        await tenant_repo.remove_grant(session, user_id=user_id, tenant_id=tid, kind=kind)
        await audit.record(
            session,
            action=audit.TENANT_UNASSIGNED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"tenant_id": tid, "kind": kind},
        )
    await session.commit()

    ids = await tenant_repo.list_grant_tenant_ids(session, user_id, kind)
    return AssignmentOut(role=target.role, tenant_ids=sorted(ids))
