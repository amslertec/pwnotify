"""Benutzerverwaltung: lokale Konten (CRUD) + SSO-Konten (aus Entra-Gruppe)."""

from __future__ import annotations

from fastapi import APIRouter

from ...core.errors import ConflictError, NotFoundError
from ...core.security import hash_password
from ...repositories import user_repo
from ...schemas.auth import AdminUserCreate, AdminUserOut
from ...schemas.common import Message
from ..deps import CurrentUser, SessionDep, SettingsDep

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
async def create_local(_: CurrentUser, body: AdminUserCreate, session: SessionDep) -> AdminUserOut:
    existing = await user_repo.get_by_username(session, body.username)
    if existing is not None:
        raise ConflictError("Benutzername bereits vergeben.", code="username_taken")
    user = await user_repo.create(
        session,
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_sso=False,
    )
    return AdminUserOut.model_validate(user, from_attributes=True)


@router.delete("/{user_id}", response_model=Message)
async def delete_user(user: CurrentUser, user_id: int, session: SessionDep) -> Message:
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
    await user_repo.delete(session, user_id)
    return Message(message="Benutzer gelöscht.")


@router.post("/sso/sync", response_model=Message)
async def sync_sso(_: CurrentUser, session: SessionDep, svc: SettingsDep) -> Message:
    from ...services import oidc

    settings = await svc.get_all()
    if not settings.get("oidc.enabled") or not settings.get("oidc.admin_group_id"):
        raise ConflictError(
            "SSO ist nicht aktiviert oder keine Admin-Gruppe hinterlegt.", code="sso_not_configured"
        )
    stats = await oidc.sync_sso_users(session, settings)
    return Message(
        message=(f"{stats['synced']} SSO-Benutzer synchronisiert, {stats['removed']} entfernt.")
    )
