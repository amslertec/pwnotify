"""Benutzerverwaltung: lokale Konten (CRUD) + SSO-Konten (aus Entra-Gruppe)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.errors import ConflictError, NotFoundError
from ...core.security import hash_password
from ...db.tenant_context import tenant_scoped_session
from ...repositories import tenant_repo, user_repo
from ...schemas.auth import AdminUserCreate, AdminUserOut, RoleUpdate
from ...schemas.common import Message
from ...services import audit
from ...services.settings_service import SettingsService
from ..deps import AdminUser, CurrentUser, SessionDep

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


@router.get("")
async def list_users(_: CurrentUser, session: SessionDep) -> dict[str, list[AdminUserOut]]:
    rows = await user_repo.list_all(session)
    out = [AdminUserOut.model_validate(u, from_attributes=True) for u in rows]
    return {
        "local": [u for u in out if not u.is_sso],
        "sso": [u for u in out if u.is_sso],
    }


@router.post("", response_model=AdminUserOut)
async def create_local(
    request: Request, admin: AdminUser, body: AdminUserCreate, session: SessionDep
) -> AdminUserOut:
    existing = await user_repo.get_by_username(session, body.username)
    if existing is not None:
        raise ConflictError("Benutzername bereits vergeben.", code="username_taken")
    user = await user_repo.create(
        session,
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        role=body.role,
        is_sso=False,
    )
    await audit.record(
        session,
        action=audit.USER_CREATED,
        actor=admin,
        target=body.username,
        request=request,
        detail={"role": body.role, "sso": False},
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
        stats = await oidc.sync_sso_users(session, settings)
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
