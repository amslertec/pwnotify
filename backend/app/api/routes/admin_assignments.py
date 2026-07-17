"""Zuweisungs-API (Access-Modell/Superadmin-Phase, Task 4): welchen (aktiven) Mandanten
darf ein Admin/Auditor-Konto zusätzlich zu seinem Heim-Tenant verwalten/einsehen --
SUPERADMIN-only auf ALLEN Routen (`SuperadminUser`, Design §4/§6).

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
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...repositories import tenant_repo, user_repo
from ...schemas.assignment import AssignmentOut, AssignmentUpdate
from ...services import audit
from ..deps import SessionDep, SuperadminUser

router = APIRouter(prefix="/admin/assignments", tags=["admin-assignments"])


def _grant_kind(role: str) -> str:
    """Grant-Typ aus der Rolle des Zielkontos -- die einzige Stelle, die diese Abbildung
    trifft (siehe Moduldoku: Kern der Task-4-Abweichung)."""
    return "admin" if role == "admin" else "auditor"


@router.get("/{user_id}", response_model=AssignmentOut)
async def get_assignments(_: SuperadminUser, user_id: int, session: SessionDep) -> AssignmentOut:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    if target.role == "superadmin":
        # Instanzweit, keine Zuweisungszeile relevant -- nichts anzuzeigen, kein Fehler.
        return AssignmentOut(role=target.role, tenant_ids=[])
    kind = _grant_kind(target.role)
    ids = await tenant_repo.list_grant_tenant_ids(session, user_id, kind)
    return AssignmentOut(role=target.role, tenant_ids=sorted(ids))


@router.put("/{user_id}", response_model=AssignmentOut)
async def set_assignments(
    request: Request,
    admin: SuperadminUser,
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

    if not await tenant_repo.is_provider_account(session, target):
        # Kunden-homed Konto (s. Moduldoku "Cross-Grant-Lock"): die einzig erlaubte
        # Zuweisung ist der eigene Heim-Tenant -- jede Fremd-Id in `requested` lehnt die
        # GESAMTE Anfrage ab, bevor irgendeine Zuweisungszeile geschrieben wird.
        allowed = {target.tenant_id} if target.tenant_id is not None else set()
        if not (requested <= allowed):
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
