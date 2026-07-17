"""Benutzerverwaltung: lokale Konten (CRUD) + SSO-Konten (aus Entra-Gruppe)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...core.security import hash_password
from ...db.tenant_context import tenant_scoped_session
from ...repositories import tenant_repo, user_repo
from ...schemas.auth import (
    AdminUserCreate,
    AdminUserOut,
    RoleUpdate,
    SuperadminCreate,
    SuperadminToggle,
)
from ...schemas.common import Message
from ...services import audit
from ...services.settings_service import SettingsService
from ..deps import (
    ActiveTenantClaim,
    AdminUser,
    CurrentUser,
    SessionDep,
    SuperadminUser,
    default_tenant_id,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


@router.get("")
async def list_users(user: CurrentUser, session: SessionDep) -> dict[str, list[AdminUserOut]]:
    """Gescopte Kontoliste für die Access-Seite (Access-Modell/Superadmin-Phase, Task 3).

    Der Sicherheitsfix: vormals `user_repo.list_all(session)` instanzweit -- JEDER Tenant
    sah dieselbe volle Kontoliste (Leselecke). Jetzt pro Rolle:

    - **Superadmin** (`not is_sso and role=='superadmin'`): sieht ALLES -- alle lokalen
      Nicht-Superadmin-Konten, alle SSO-Konten, UND zusätzlich die eigene
      `superadmins`-Liste (NUR für diese Rolle im Antwortobjekt vorhanden).
    - **Lokaler Admin** (`not is_sso and role=='admin'`): NUR Konten der Tenants, die er
      selbst hält (`tenant_repo.admin_tenants`) -- SSO-Konten dieser Tenants plus lokale
      Admins/Auditoren mit einer Zuweisung auf einen dieser Tenants. NIE Superadmins, NIE
      Konten un-gehaltener Tenants. Kein `superadmins`-Schlüssel in der Antwort.
    - **Alles andere** (Auditor, SSO-Konto, unbekannter Zustand): default-deny -> leere
      Listen. Die `/access`-Seite ist zwar admin-only im Frontend, dieses Gate gilt aber
      unabhängig davon hier ebenfalls.
    """
    if not user.is_sso and user.role == "superadmin":
        rows = await user_repo.list_all(session)
        out = [AdminUserOut.model_validate(u, from_attributes=True) for u in rows]
        return {
            "local": [u for u in out if not u.is_sso and u.role != "superadmin"],
            "sso": [u for u in out if u.is_sso],
            "superadmins": [u for u in out if not u.is_sso and u.role == "superadmin"],
        }

    if not user.is_sso and user.role == "admin":
        tids = await tenant_repo.admin_tenants(session, user)
        sso_rows = await user_repo.list_sso_in_tenants(session, tids)
        local_rows = await user_repo.list_local_granted_to_tenants(session, tids)
        return {
            "local": [AdminUserOut.model_validate(u, from_attributes=True) for u in local_rows],
            "sso": [AdminUserOut.model_validate(u, from_attributes=True) for u in sso_rows],
        }

    return {"local": [], "sso": []}


@router.post("", response_model=AdminUserOut)
async def create_local(
    request: Request,
    admin: AdminUser,
    body: AdminUserCreate,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
) -> AdminUserOut:
    """Legt ein lokales Konto an -- gescopt nach Aufrufer (Task 3).

    Superadmin: uneingeschränkt, KEINE automatische Zuweisung (Tenants weist der
    Superadmin später gezielt zu, Task 4). Jeder andere Admin-Aufrufer (lokaler Admin
    oder SSO-Admin): das neue Konto wird automatisch auf den AKTIVEN Tenant des
    Aufrufers zugewiesen -- mit der zur neuen Rolle passenden Zuweisungsart
    (`role=='admin'` -> `admin_tenant`, `role=='auditor'` -> `auditor_tenant`), damit ein
    `role=='admin'`-Konto nie NUR eine `auditor_tenant`-Zuweisung hat (das würde ihm über
    das Rollen-Gate Schreibzugriff verschaffen, den die Zuweisung selbst nicht hergibt).

    Der `active_tenant`-Claim wird NICHT blind übernommen (er ist laut `ActiveTenantClaim`
    unautorisiert, nur zur Anzeige gedacht) -- stattdessen zusätzlich über
    `tenant_repo.is_allowed(..., write=True)` geprüft. Fehlt der Claim oder besteht keine
    Schreib-Mitgliedschaft, wird klar abgelehnt statt ein unsichtbares, nicht zugewiesenes
    Konto anzulegen.

    **Heim-Tenant setzen (Context-Gating v2, Task 3):** vormals bekam das neue Konto zwar
    eine `admin_tenant`/`auditor_tenant`-Zeile, aber NIE einen `tenant_id` (Heimat) -- damit
    hatte der Cross-Grant-Lock (Task 2, `tenant_repo.is_provider_account`,
    `admin_assignments.set_assignments`) keine Grundlage: ein Konto ohne Heimat gilt dort
    als Kunden-Konto mit LEERER erlaubter Menge (`tenant_id is None` -> kein Provider), also
    über-restriktiv für ein vom Superadmin angelegtes Konto UND ohne echte Kundenheimat für
    ein vom Kunden-Admin angelegtes Konto. Deshalb jetzt explizit:
    - Nicht-Superadmin-Aufrufer (lokaler/SSO-Admin für seinen aktiven Kunden): Heimat =
      `grant_tenant_id` (derselbe, bereits über `is_allowed(..., write=True)` geprüfte aktive
      Tenant) -- das neue Konto ist also kunden-beheimatet UND passend zugewiesen, damit laut
      Task 2 strukturell nicht auf einen fremden Tenant cross-grantbar.
      -- ein reines Kundenstaff-Konto.
    - Superadmin-Aufrufer: Heimat = der Default-Tenant (`deps.default_tenant_id`) -- Provider-
      Personal ist default-beheimatet, ein so angelegtes Konto bleibt daher über die
      Zuweisungs-API (Task 4/Cross-Grant-Lock Task 2) auf beliebige Kunden cross-grantbar.
      (Superadmin-Anlage eines *Superadmin*-Kontos bleibt unverändert in `create_superadmin`
      -- instanzweit, keine Heimat nötig.)
    """
    existing = await user_repo.get_by_username(session, body.username)
    if existing is not None:
        raise ConflictError("Benutzername bereits vergeben.", code="username_taken")

    is_superadmin_caller = not admin.is_sso and admin.role == "superadmin"
    grant_tenant_id: int | None = None
    if not is_superadmin_caller:
        if active_tenant is None or not await tenant_repo.is_allowed(
            session, admin, active_tenant, write=True
        ):
            raise ForbiddenError(
                "Kein aktiver Mandant mit Verwaltungsrechten.", code="tenant_required"
            )
        grant_tenant_id = active_tenant

    home_tenant_id = (
        grant_tenant_id if not is_superadmin_caller else await default_tenant_id(session)
    )

    user = await user_repo.create(
        session,
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        role=body.role,
        is_sso=False,
        tenant_id=home_tenant_id,
    )
    assert user.id is not None  # gerade committet, hat also eine id

    if grant_tenant_id is not None:
        kind = "admin" if body.role == "admin" else "auditor"
        await tenant_repo.add_grant(session, user_id=user.id, tenant_id=grant_tenant_id, kind=kind)

    detail: dict[str, object] = {"role": body.role, "sso": False, "home_tenant_id": home_tenant_id}
    if grant_tenant_id is not None:
        detail["granted_tenant_id"] = grant_tenant_id

    await audit.record(
        session,
        action=audit.USER_CREATED,
        actor=admin,
        target=body.username,
        request=request,
        detail=detail,
    )
    await session.commit()
    return AdminUserOut.model_validate(user, from_attributes=True)


@router.post("/{user_id}/role", response_model=AdminUserOut)
async def set_role(
    request: Request, admin: AdminUser, user_id: int, body: RoleUpdate, session: SessionDep
) -> AdminUserOut:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    # Ein Superadmin-Ziel läuft NIE über diesen Pfad (Task 4, Access-Modell/Superadmin-
    # Phase): dieses Gate ist `AdminUser`, nicht `SuperadminUser` -- ein PLAIN Admin
    # könnte sonst über `RoleUpdate.role='admin'` (vom Schema erlaubt) den letzten
    # Superadmin unbemerkt zu einem gewöhnlichen Admin herabstufen, ohne dass der
    # Last-Superadmin-Schutz (der nur in `set_superadmin` sitzt) je greift. Der Wechsel
    # zum/vom Superadmin läuft ausschliesslich über `set_superadmin` (superadmin-only).
    if target.role == "superadmin":
        raise ForbiddenError(
            "Superadmin-Rollenwechsel nur über die Superadmin-Verwaltung möglich.",
            code="superadmin_required",
        )
    # Cross-Tenant-Fix (Sicherheitsreview, Whole-Branch-Review Access-Modell/Superadmin-
    # Phase): Task 3 hat `list_users`/`create_local` gescopt, aber `set_role` blieb nur über
    # `AdminUser` (jeder Admin/Superadmin JEDER Tenant) gegatet und löste `target` ohne RLS
    # auf `app_user` (instanzweit) auf -- ein lokaler Admin von Tenant A konnte so die Rolle
    # eines Kontos ändern, das AUSSCHLIESSLICH zu Tenant B gehört (IDs sind sequentiell
    # enumerierbar). Ein Superadmin-Aufrufer überspringt diese Prüfung (voller Zugriff,
    # bereits durch den obigen Guard beschränkt). Für jeden anderen Aufrufer muss die GESAMTE
    # Tenant-Zugehörigkeit des Ziels innerhalb der vom Aufrufer VERWALTETEN Tenants liegen
    # (Teilmengen-Regel, nicht bloss Schnittmenge) -- `app_user` ist instanzweit, ein Konto
    # kann also zusätzlich einem Tenant angehören, den der Aufrufer nicht hält; ein reiner
    # Schnittmengen-Test würde die Rollenänderung trotzdem durchlassen und so ungewollt auch
    # den fremden Tenant treffen. Ein Ziel ganz ohne Tenant-Zugehörigkeit (leere Menge) darf
    # NUR ein Superadmin anfassen -- daher `not target_scope` als eigener Ablehnungsgrund.
    if admin.is_sso or admin.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, admin)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )
    # Den letzten Administrator nicht herabstufen — sonst kann niemand mehr verwalten.
    # Deckt auch den Selbstentzug ab, wenn man der einzige Admin ist.
    if (
        target.role == "admin"
        and body.role != "admin"
        and await user_repo.count_admins(session) <= 1
    ):
        raise ConflictError(
            "Der letzte Administrator kann nicht herabgestuft werden.",
            code="cannot_demote_last_admin",
        )
    vorher = target.role
    target.role = body.role
    await audit.record(
        session,
        action=audit.USER_ROLE_CHANGED,
        actor=admin,
        target=target.username,
        request=request,
        detail={"from": vorher, "to": body.role, "sso": target.is_sso},
    )
    await session.commit()
    await session.refresh(target)
    return AdminUserOut.model_validate(target, from_attributes=True)


@router.post("/superadmin", response_model=AdminUserOut)
async def create_superadmin(
    request: Request, admin: SuperadminUser, body: SuperadminCreate, session: SessionDep
) -> AdminUserOut:
    """Legt einen LOKALEN Superadmin an -- superadmin-only (Design §11.3: Superadmin ist
    IMMER ein lokales Konto, nie SSO). KEINE automatische Zuweisung: der Superadmin ist
    instanzweit und braucht keine `admin_tenant`/`auditor_tenant`-Zeile (anders als
    `create_local` für gewöhnliche Admin/Auditor-Konten, Task 3)."""
    if body.is_sso:
        raise ConflictError(
            "Ein Superadmin muss ein lokales Konto sein.", code="superadmin_must_be_local"
        )
    existing = await user_repo.get_by_username(session, body.username)
    if existing is not None:
        raise ConflictError("Benutzername bereits vergeben.", code="username_taken")

    user = await user_repo.create(
        session,
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        role="superadmin",
        is_sso=False,
    )
    await audit.record(
        session,
        action=audit.SUPERADMIN_CREATED,
        actor=admin,
        target=body.username,
        request=request,
    )
    await session.commit()
    return AdminUserOut.model_validate(user, from_attributes=True)


@router.post("/{user_id}/superadmin", response_model=AdminUserOut)
async def set_superadmin(
    request: Request,
    admin: SuperadminUser,
    user_id: int,
    body: SuperadminToggle,
    session: SessionDep,
) -> AdminUserOut:
    """Befördert/degradiert zum/vom Superadmin -- der EINZIGE Pfad dafür (`set_role` lehnt
    jeden Rollenwechsel eines Superadmin-Ziels hart ab, s.o.). Superadmin-only.

    Befördern: nur ein LOKALES Ziel (`not is_sso`) darf Superadmin werden (Design §11.3,
    `code="superadmin_must_be_local"`) -- seine bisherigen `admin_tenant`/
    `auditor_tenant`-Zuweisungen werden dabei geräumt (bewusste Entscheidung: der
    Superadmin sieht ohnehin alle aktiven Tenants, verwaiste Zuweisungszeilen wären reiner
    Datenmüll und würden bei einer künftigen Rückstufung sonst überraschend wieder
    aufleben).

    Degradieren: der letzte AKTIVE Superadmin darf nicht herabgestuft werden (Design §11.4,
    `code="cannot_demote_last_superadmin"`) -- sonst könnte sich niemand mehr instanzweit
    verwalten. Das Ziel fällt dabei auf `role="admin"` zurück (keine feinere Rolle unterhalb
    von Superadmin ist hier definiert)."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")

    if body.promote:
        if target.role == "superadmin":
            return AdminUserOut.model_validate(target, from_attributes=True)
        if target.is_sso:
            raise ConflictError(
                "Nur lokale Konten können zu Superadmin befördert werden.",
                code="superadmin_must_be_local",
            )
        vorher = target.role
        target.role = "superadmin"
        assert target.id is not None  # bereits persistiert (kam aus user_repo.get)
        for existing_kind in ("admin", "auditor"):
            for tid in await tenant_repo.list_grant_tenant_ids(session, target.id, existing_kind):
                await tenant_repo.remove_grant(
                    session, user_id=target.id, tenant_id=tid, kind=existing_kind
                )
        await audit.record(
            session,
            action=audit.USER_ROLE_CHANGED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"from": vorher, "to": "superadmin", "sso": target.is_sso},
        )
    else:
        if target.role != "superadmin":
            return AdminUserOut.model_validate(target, from_attributes=True)
        if await user_repo.count_superadmins(session) <= 1:
            raise ConflictError(
                "Der letzte Superadmin kann nicht herabgestuft werden.",
                code="cannot_demote_last_superadmin",
            )
        target.role = "admin"
        await audit.record(
            session,
            action=audit.USER_ROLE_CHANGED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"from": "superadmin", "to": "admin", "sso": target.is_sso},
        )

    await session.commit()
    await session.refresh(target)
    return AdminUserOut.model_validate(target, from_attributes=True)


@router.delete("/{user_id}", response_model=Message)
async def delete_user(
    request: Request, user: AdminUser, user_id: int, session: SessionDep
) -> Message:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    # Löschen gesperrt, wenn es nur einen Benutzer gibt.
    if await user_repo.count(session) <= 1:
        raise ConflictError("Der letzte Benutzer kann nicht gelöscht werden.", code="last_user")
    if target.id == user.id:
        raise ConflictError(
            "Sie können Ihr eigenes Konto nicht löschen.", code="cannot_delete_self"
        )
    # Superadmin-Ziel: NIE über einen Nicht-Superadmin-Aufrufer löschbar (Task 4,
    # Access-Modell/Superadmin-Phase -- Sicherheitsreview-Fix). Dieses Gate ist `AdminUser`,
    # nicht `SuperadminUser` -- ohne diese Prüfung könnte ein PLAIN Admin oder ein SSO-Admin
    # jeden NICHT-letzten Superadmin per Löschung entfernen, wiederholt bis zum letzten
    # (der Last-Superadmin-Schutz unten greift erst BEIM letzten). Analog zu `set_role`s
    # Schutz für den Rollenwechsel eines Superadmin-Ziels -- gleicher Fehlercode.
    if target.role == "superadmin" and (user.is_sso or user.role != "superadmin"):
        raise ForbiddenError(
            "Superadmin-Löschung nur durch einen Superadmin möglich.",
            code="superadmin_required",
        )
    # Cross-Tenant-Fix (Sicherheitsreview, Whole-Branch-Review Access-Modell/Superadmin-
    # Phase) -- analog zum Scope-Check in `set_role` oben: `delete_user` war nur über
    # `AdminUser` gegatet und löste `target` ohne RLS auf, ein lokaler Admin von Tenant A
    # konnte so einen NUR zu Tenant B gehörenden Benutzer löschen. Ein Superadmin-Aufrufer
    # überspringt diese Prüfung (voller Zugriff). Für jeden anderen Aufrufer muss die GESAMTE
    # Tenant-Zugehörigkeit des Ziels innerhalb der vom Aufrufer VERWALTETEN Tenants liegen
    # (Teilmengen-, nicht Schnittmengen-Regel -- sonst würde das Löschen eines auch-bei-B
    # zugewiesenen Kontos ungewollt Tenant B mittreffen, da `app_user` instanzweit ist). Ein
    # Ziel ganz ohne Tenant-Zugehörigkeit darf NUR ein Superadmin löschen.
    if user.is_sso or user.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, user)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )
    # Last-Superadmin-Schutz (Design §11.4) -- analog zum Last-Admin-Schutz oben, aber für
    # die instanzweite Rolle: ohne diese Prüfung könnte man den letzten Superadmin per
    # Löschung aussperren.
    if target.role == "superadmin" and await user_repo.count_superadmins(session) <= 1:
        raise ConflictError(
            "Der letzte Superadmin kann nicht gelöscht werden.",
            code="cannot_delete_last_superadmin",
        )
    await audit.record(
        session,
        action=audit.USER_DELETED,
        actor=user,
        target=target.username,
        request=request,
        detail={"role": target.role, "sso": target.is_sso},
    )
    await session.commit()
    await user_repo.delete(session, user_id)
    return Message(message="Benutzer gelöscht.")


@router.post("/sso/sync", response_model=Message)
async def sync_sso(_: AdminUser, session: SessionDep) -> Message:
    """Gleicht SSO-Benutzer PRO aktivem Mandanten ab -- jeder Kunde hat seine eigene
    ``oidc.admin_group_id``/``oidc.auditor_group_id``/``graph.*``-Konfiguration
    (Phase-3-TODO, hier geschlossen): vormals lief der Abgleich EINMAL auf der
    Owner-Session -- weil RLS für die Owner-Rolle nicht greift, läse ``get_all()`` dort ein
    undefiniertes Gemisch der ``oidc.*``-Zeilen ALLER Tenants, sobald ein zweiter existiert.

    ``app_user`` ist instanzweit (kein RLS) -- der eigentliche Schreibzugriff
    (``oidc.sync_sso_users``) läuft deshalb bewusst auf der übergebenen Owner-`session`
    (kein aktiver Tenant-Kontext an dieser Stelle: `tenant_scoped_session` bindet den
    Kontext nur für die Dauer seines eigenen `async with`-Blocks, s.u., danach ist der
    Owner-Kontext automatisch wieder aktiv) -- anders als der Hintergrund-Lauf
    (`runner.execute_run`), dessen Tenant-Schleife bereits INNERHALB eines aktiven
    `use_tenant`-Blocks steht und deshalb explizit `use_owner_context()` braucht.
    """
    from ...services import oidc

    tenants = await tenant_repo.list_active(session)
    configured = False
    synced = removed = 0
    blocked_tenants: list[str] = []
    for tenant in tenants:
        assert tenant.id is not None  # persistierte Zeile aus der DB
        async with tenant_scoped_session(tenant.id) as tsession:
            settings = await SettingsService(tsession).get_all()
        if not settings.get("oidc.enabled") or not settings.get("oidc.admin_group_id"):
            continue
        configured = True
        stats = await oidc.sync_sso_users(session, settings, tenant_id=tenant.id)
        synced += stats["synced"]
        removed += stats["removed"]
        if stats.get("removal_blocked"):
            blocked_tenants.append(tenant.name)

    if not configured:
        raise ConflictError(
            "SSO ist nicht aktiviert oder keine Admin-Gruppe hinterlegt.", code="sso_not_configured"
        )
    message = f"{synced} SSO-Benutzer synchronisiert, {removed} entfernt."
    if blocked_tenants:
        message += (
            f" Entfernen blockiert für: {', '.join(blocked_tenants)} (Schutz vor Aussperrung)."
        )
    return Message(message=message)
