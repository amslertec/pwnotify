"""SSO/OIDC via Microsoft Entra (Authorization-Code-Flow).

Nutzt dieselbe App-Registrierung wie Graph. Nur Mitglieder der konfigurierten
Entra-Admin-Gruppe dürfen sich anmelden — die Gruppenprüfung erfolgt über den
``groups``-Claim im ID-Token (App-Manifest: ``groupMembershipClaims``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
import msal
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import resolve_secret_key
from ..core.errors import AuthError, PwNotifyError
from ..core.logging import get_logger
from ..core.security import hash_password
from ..repositories import user_repo
from .graph import GraphClient, GraphConfig

log = get_logger("oidc")

_LOGIN = {
    "global": "https://login.microsoftonline.com",
    "usgov": "https://login.microsoftonline.us",
    "china": "https://login.chinacloudapi.cn",
}
_SCOPES = ["User.Read"]  # liefert gültiges Token; groups-Claim kommt aus dem Manifest


@dataclass
class OidcResult:
    username: str
    display_name: str
    allowed: bool
    reason: str | None = None


def is_configured(settings: dict[str, Any]) -> bool:
    return bool(
        settings.get("oidc.enabled")
        and settings.get("graph.tenant_id")
        and settings.get("graph.client_id")
        and settings.get("graph.client_secret")
        and settings.get("oidc.admin_group_id")
    )


def _authority(settings: dict[str, Any]) -> str:
    login = _LOGIN.get(settings.get("graph.cloud") or "global", _LOGIN["global"])
    return f"{login}/{settings.get('graph.tenant_id')}"


def _app(settings: dict[str, Any]) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        settings.get("graph.client_id"),
        authority=_authority(settings),
        client_credential=settings.get("graph.client_secret"),
    )


# -- CSRF-State (signiertes, kurzlebiges Token) ------------------------------- #
def _state_key() -> bytes:
    return hmac.new(resolve_secret_key(), b"pwnotify-oidc-state", hashlib.sha256).digest()


def sign_state() -> str:
    now = dt.datetime.now(dt.UTC)
    payload = {"nonce": uuid.uuid4().hex, "exp": int((now + dt.timedelta(minutes=10)).timestamp())}
    return jwt.encode(payload, _state_key(), algorithm="HS256")


def verify_state(state: str) -> None:
    try:
        jwt.decode(state, _state_key(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("Ungültiger oder abgelaufener SSO-State.") from exc


# -- Flow --------------------------------------------------------------------- #
def build_login_url(
    settings: dict[str, Any], redirect_uri: str, state: str, login_hint: str | None = None
) -> str:
    if not is_configured(settings):
        raise PwNotifyError("SSO ist nicht vollständig konfiguriert.", code="oidc_not_configured")
    url = _app(settings).get_authorization_request_url(
        _SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        prompt="select_account",
        login_hint=login_hint or None,
    )
    return str(url)


async def exchange_and_verify(settings: dict[str, Any], code: str, redirect_uri: str) -> OidcResult:
    app = _app(settings)
    result = await asyncio.to_thread(
        app.acquire_token_by_authorization_code,
        code,
        scopes=_SCOPES,
        redirect_uri=redirect_uri,
    )
    if "access_token" not in result:
        desc = result.get("error_description", result.get("error", "unbekannt"))
        raise AuthError(f"SSO-Token-Austausch fehlgeschlagen: {desc}")

    claims: dict[str, Any] = result.get("id_token_claims", {})
    username = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""
    display_name = claims.get("name") or username

    admin_group = str(settings.get("oidc.admin_group_id") or "")
    groups = claims.get("groups")
    if not isinstance(groups, list):
        # Overage (User in >200 Gruppen) oder groupMembershipClaims nicht gesetzt.
        return OidcResult(
            username=username,
            display_name=display_name,
            allowed=False,
            reason="Keine Gruppeninformationen im Token. Bitte im App-Manifest "
            "'groupMembershipClaims' auf 'SecurityGroup' setzen.",
        )
    allowed = admin_group in groups
    return OidcResult(
        username=username,
        display_name=display_name,
        allowed=allowed,
        reason=None if allowed else "Nicht Mitglied der Admin-Gruppe.",
    )


async def sync_sso_users(session: AsyncSession, settings: dict[str, Any]) -> dict[str, int]:
    """Gleicht die SSO-Benutzer mit der Entra-Admin-Gruppe ab.

    Mitglieder werden als SSO-Benutzer angelegt/aktualisiert; frühere SSO-Benutzer,
    die nicht mehr in der Gruppe sind, werden deaktiviert (Login gesperrt).
    """
    group_id = str(settings.get("oidc.admin_group_id") or "")
    if not (settings.get("oidc.enabled") and group_id and settings.get("graph.client_secret")):
        return {"synced": 0, "deactivated": 0}

    graph = GraphClient(
        GraphConfig(
            tenant_id=settings.get("graph.tenant_id") or "",
            client_id=settings.get("graph.client_id") or "",
            client_secret=settings.get("graph.client_secret") or "",
            cloud=settings.get("graph.cloud") or "global",
        )
    )
    members = await graph.get_group_members(group_id)

    present: set[str] = set()
    synced = 0
    for m in members:
        upn = m.get("userPrincipalName")
        if not upn:
            continue
        present.add(upn.lower())
        name = m.get("displayName") or upn
        user = await user_repo.get_by_username(session, upn)
        if user is None:
            await user_repo.create(
                session,
                username=upn,
                password_hash=hash_password(uuid.uuid4().hex),
                display_name=name,
                is_sso=True,
            )
        else:
            user.is_sso = True
            user.display_name = name
            user.is_active = True
        synced += 1

    # SSO-Benutzer, die nicht mehr in der Gruppe sind, komplett entfernen.
    to_remove = [
        u.id for u in await user_repo.list_sso(session) if u.username.lower() not in present
    ]
    for uid in to_remove:
        if uid is not None:
            await user_repo.delete(session, uid)

    await session.commit()
    log.info("sso_users_synced", synced=synced, removed=len(to_remove))
    return {"synced": synced, "removed": len(to_remove)}
