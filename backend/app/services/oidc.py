"""SSO/OIDC via Microsoft Entra (Authorization-Code-Flow).

Nutzt dieselbe App-Registrierung wie Graph. Nur Mitglieder der konfigurierten
Entra-Admin-Gruppe dürfen sich anmelden — die Gruppenprüfung erfolgt über den
``groups``-Claim im ID-Token (App-Manifest: ``groupMembershipClaims``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import msal
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import decrypt, encrypt
from ..core.errors import AuthError, PwNotifyError
from ..core.logging import get_logger
from ..core.security import hash_password
from ..repositories import assignment_group_repo, user_repo
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
    role: str = "admin"
    reason: str | None = None
    tid: str | None = None
    """Entra-Tenant-ID (`tid`-Claim) des ID-Tokens -- Grundlage für das SSO-Tenant-Mapping
    (Phase 4a Task 4). ``None`` nur, wenn das Token den Claim ausnahmsweise nicht enthält."""
    groups: list[str] | None = None
    """Roher Gruppen-Claim (bzw. Graph-Rückfrage-Ergebnis) des Tokens -- ``None`` nur, wenn
    keine Gruppeninformation ermittelbar war. Grundlage für die AUTORITATIVE, per-Kunde
    erfolgende Rollen-Neuauflösung im Callback (Sicherheitsfix, Phase 4c Task 4): `role`/
    `allowed` oben sind gegen die OWNER-/Instanz-Settings berechnet (Übergang, s. u.) und
    dürfen NICHT unbesehen für die Rolle in einem per `tid` gefundenen Kunden übernommen
    werden, sobald ≥2 SSO-Kunden existieren -- `resolve_role(groups, tenant_settings)` muss
    dafür erneut aufgerufen werden."""


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


# -- Flow (MSAL native auth-code-flow: PKCE S256 + nonce + state, all built-in) ---------- #
def encode_flow_cookie(flow: dict[str, Any]) -> str:
    """Encrypt the MSAL auth-code flow dict for the short-lived, browser-bound state cookie."""
    return encrypt(json.dumps(flow))


def decode_flow_cookie(value: str) -> dict[str, Any]:
    """Decrypt+parse the flow cookie. Raises AuthError on any tampering/format error."""
    try:
        data: dict[str, Any] = json.loads(decrypt(value))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError(
            "Ungültiger oder abgelaufener SSO-State.", code="sso_state_invalid"
        ) from exc
    return data


def initiate_login(
    settings: dict[str, Any], redirect_uri: str, login_hint: str | None = None
) -> tuple[str, str]:
    """Start an OIDC auth-code flow with PKCE+nonce. Returns (auth_url, encrypted_flow_cookie)."""
    if not is_configured(settings):
        raise PwNotifyError("SSO ist nicht vollständig konfiguriert.", code="oidc_not_configured")
    flow = _app(settings).initiate_auth_code_flow(
        _SCOPES, redirect_uri=redirect_uri, prompt="select_account", login_hint=login_hint or None
    )
    return flow["auth_uri"], encode_flow_cookie(flow)


def resolve_role(
    groups: list[str] | None, settings: dict[str, Any]
) -> tuple[str, bool, str | None]:
    """Bestimmt Rolle + Zugriff EINES per-Kunde-Settings-Dicts gegen einen Gruppen-Claim.

    Rein/zustandslos, damit sie zweimal aufgerufen werden kann: einmal in
    `exchange_and_verify` gegen die Owner-/Instanz-Settings (Übergangspfad, Token-Austausch
    selbst läuft noch über die instanzweite App-Registrierung), und AUTORITATIV ein zweites
    Mal im Callback gegen die Settings DES per `tid` gefundenen Kunden (Sicherheitsfix,
    Phase 4c Task 4) -- erst dieser zweite Aufruf entscheidet über `user.role`/Zulassung.
    Admin-Gruppe hat Vorrang vor Auditor-Gruppe.

    Gibt ``(role, allowed, reason)`` zurück; ``reason`` ist ``None`` bei Erfolg.
    """
    if groups is None:
        return (
            "admin",
            False,
            "Keine Gruppeninformationen im Token und Rückfrage bei Microsoft Graph "
            "nicht möglich. Bitte im App-Manifest 'groupMembershipClaims' auf "
            "'SecurityGroup' setzen.",
        )
    admin_group = str(settings.get("oidc.admin_group_id") or "")
    auditor_group = str(settings.get("oidc.auditor_group_id") or "")
    if admin_group and admin_group in groups:
        return "admin", True, None
    if auditor_group and auditor_group in groups:
        return "auditor", True, None
    return "admin", False, "Nicht Mitglied einer berechtigten Gruppe."


async def resolve_group_role(session: AsyncSession, groups: list[str] | None) -> tuple[str, bool]:
    """Rolle + Zugriff für PROVIDER-Personal im Multi-Tenant-Mode aus der TEAM-Mitgliedschaft.

    WARUM es existiert: SSO ist im Multi-Tenant-Mode ein PROVIDER-Feature. Läuft eine Instanz
    im Multi-Tenant-Mode, entscheidet NICHT mehr die per-Kunde-Settings-Rollen-Gruppe
    (`resolve_role`), sondern die Mitgliedschaft in einem Team (`AssignmentGroup`) über Zulassung
    und Rolle -- das gesamte SSO-Personal wird über Teams verwaltet, nicht über die
    `oidc.admin_group_id`/`oidc.auditor_group_id`-Settings. Diese Funktion ist hinter dem
    Callback-Gate für JEDEN Multi-Tenant-SSO-Login erreichbar (`if multi_tenant`, unabhängig vom
    gematchten `tid`); das Konto homet dabei stets auf dem Default-Tenant. Nur im SINGLE-Tenant-
    Mode bleibt der Login bei `resolve_role`.

    Fail-closed OHNE Settings-Fallback: kein Token-`groups`-Claim oder KEIN Claim, der auf
    IRGENDEIN Team zeigt, heisst NICHT autorisiert (`("admin", False)`). Sonst gewinnt Admin --
    matcht der Claim ein Admin-Team, wird die Rolle `"admin"`, andernfalls (nur Auditor-Teams)
    `"auditor"`.

    Gibt `(role, allowed)` zurück -- KEIN `reason` (der Callback liefert den festen Grund-String
    bei Verweigerung selbst)."""
    if not groups:
        return "admin", False
    roles = await assignment_group_repo.group_roles_for_entra_groups(session, set(groups))
    if not roles:
        return "admin", False
    if "admin" in roles:
        return "admin", True
    return "auditor", True


async def exchange_and_verify(
    settings: dict[str, Any], flow: dict[str, Any], auth_response: dict[str, Any]
) -> OidcResult:
    """Complete the MSAL auth-code flow started by :func:`initiate_login`.

    Delegates state match (CSRF), nonce match against the id_token (replay), and the PKCE
    ``code_verifier`` exchange entirely to MSAL in one call -- a ``state`` mismatch raises
    ``ValueError`` before any network call (verified against msal 1.37.0).
    """
    app = _app(settings)
    try:
        result = await asyncio.to_thread(app.acquire_token_by_auth_code_flow, flow, auth_response)
    except ValueError as exc:  # MSAL raises on state mismatch before any network call
        raise AuthError("SSO-State stimmt nicht überein.", code="sso_state_mismatch") from exc
    if "access_token" not in result:
        desc = result.get("error_description", result.get("error", "unbekannt"))
        raise AuthError(desc, code="sso_token_exchange_failed")

    claims: dict[str, Any] = result.get("id_token_claims", {})
    username = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""
    display_name = claims.get("name") or username
    tid = claims.get("tid")

    admin_group = str(settings.get("oidc.admin_group_id") or "")
    auditor_group = str(settings.get("oidc.auditor_group_id") or "")
    groups = claims.get("groups")
    if not isinstance(groups, list):
        # Zwei Ursachen: 'groupMembershipClaims' ist im App-Manifest nicht gesetzt — oder
        # der Benutzer ist in mehr als 200 Gruppen, dann liefert Entra statt der Liste nur
        # einen Verweis ("Overage"). Letzteres trifft ausgerechnet Konten mit vielen
        # Mitgliedschaften, also typischerweise Administratoren. Deshalb wird hier gezielt
        # nachgefragt, statt die Anmeldung pauschal abzulehnen.
        groups = await _groups_via_graph(
            settings, claims.get("oid") or username, [admin_group, auditor_group]
        )
    role, allowed, reason = resolve_role(groups, settings)
    return OidcResult(
        username=username,
        display_name=display_name,
        allowed=allowed,
        role=role,
        reason=reason,
        tid=tid,
        groups=groups,
    )


async def _groups_via_graph(
    settings: dict[str, Any], user_id: str, group_ids: list[str]
) -> list[str] | None:
    """Mitgliedschaft direkt bei Graph erfragen, wenn das Token keine Gruppen liefert.

    Gibt die Teilmenge der geprüften Gruppen zurück, ``None`` wenn die Rückfrage nicht
    möglich war (kein Secret, keine Gruppen konfiguriert, Graph-Fehler) — dann bleibt es
    beim bisherigen Verhalten: Anmeldung ablehnen statt im Zweifel Rechte vergeben.
    """
    gesucht = [g for g in group_ids if g]
    if not (user_id and gesucht and settings.get("graph.client_secret")):
        return None
    graph = GraphClient(
        GraphConfig(
            tenant_id=str(settings.get("graph.tenant_id") or ""),
            client_id=str(settings.get("graph.client_id") or ""),
            client_secret=str(settings.get("graph.client_secret") or ""),
            cloud=str(settings.get("graph.cloud") or "global"),
        )
    )
    try:
        treffer = await graph.check_member_groups(user_id, gesucht)
        log.info("oidc_groups_via_graph", user=user_id, matched=len(treffer))
        return list(treffer)
    except Exception as exc:
        log.warning("oidc_group_lookup_failed", user=user_id, error=str(exc))
        return None
    finally:
        await graph.aclose()


_MAX_REMOVAL_RATIO = 0.5
"""Ab welchem Anteil ein Sync als Fehlkonfiguration statt als Abgang gilt."""


def removal_blocked_reason(
    *, desired_count: int, existing_count: int, removal_count: int
) -> str | None:
    """Prüft, ob eine geplante Löschung plausibel ist. Gibt den Grund zurück, wenn nicht.

    Der Sync entfernt SSO-Benutzer, die in keiner berechtigten Gruppe mehr sind. Zwei
    Fälle sind fast immer ein Konfigurationsfehler statt ein echter Abgang — und beide
    enden damit, dass sich der Betreiber aus der eigenen Anwendung aussperrt:

    * Die Soll-Menge ist leer (leergeräumte Gruppe, falsche Group-ID): niemand entfernt
      absichtlich alle Admins auf einmal.
    * Der Sync will die Mehrheit aller SSO-Benutzer entfernen.

    Im Zweifel wird nicht gelöscht: ein zu viel behaltener Benutzer ist reparabel,
    ein Aussperren aller Administratoren nicht.
    """
    if removal_count == 0:
        return None
    if existing_count == 0:
        return None
    if desired_count == 0:
        return (
            "Die Admin-/Auditor-Gruppe ist leer — kein Abgleich möglich. "
            "Es wurde nichts entfernt (Schutz vor Aussperrung). "
            "Gruppen-ID und Mitgliedschaften in den Einstellungen prüfen."
        )
    if removal_count > existing_count * _MAX_REMOVAL_RATIO:
        return (
            f"Der Abgleich würde {removal_count} von {existing_count} SSO-Benutzern "
            "entfernen — das sieht nach einer Fehlkonfiguration aus. Es wurde nichts "
            "entfernt (Schutz vor Aussperrung)."
        )
    return None


async def sync_sso_users(
    session: AsyncSession, settings: dict[str, Any], *, tenant_id: int
) -> dict[str, int]:
    """Gleicht die SSO-Benutzer EINES Mandanten mit dessen Entra-Admin- und -Auditor-Gruppe ab.

    Mitglieder der Admin-Gruppe erhalten die Rolle ``admin``, Mitglieder der
    Auditor-Gruppe ``auditor`` (Admin hat Vorrang). Frühere SSO-Benutzer, die in
    keiner der Gruppen mehr sind, werden entfernt — abgesichert durch
    :func:`removal_blocked_reason`.

    Sicherheitsfix: sowohl das Anlegen (``tenant_id`` auf dem neuen Konto) als auch die
    Entfernungsmenge (``list_sso_for_tenant`` statt der instanzweiten ``list_sso``) sind
    strikt auf ``tenant_id`` gescoped. Vorher hätte ein Sync für Kunde A die SSO-Konten
    JEDES anderen Kunden als "in keiner Gruppe mehr" gesehen (deren UPNs stehen ja nicht
    in A's ``desired``) und gelöscht -- inklusive deren Administratoren. `tenant_id` ist
    deshalb Pflichtparameter, kein Default: ein Aufruf ohne aktiven Tenant muss beim
    Aufrufer scheitern/übersprungen werden, nicht hier still instanzweit laufen.
    """
    admin_group = str(settings.get("oidc.admin_group_id") or "")
    auditor_group = str(settings.get("oidc.auditor_group_id") or "")
    if not (settings.get("oidc.enabled") and admin_group and settings.get("graph.client_secret")):
        return {"synced": 0, "removed": 0}

    graph = GraphClient(
        GraphConfig(
            tenant_id=settings.get("graph.tenant_id") or "",
            client_id=settings.get("graph.client_id") or "",
            client_secret=settings.get("graph.client_secret") or "",
            cloud=settings.get("graph.cloud") or "global",
        )
    )

    # upn_lower -> (Original-UPN, Rolle, Anzeigename); Admin-Gruppe überschreibt Auditor.
    desired: dict[str, tuple[str, str, str]] = {}
    if auditor_group:
        for m in await graph.get_group_members(auditor_group):
            upn = m.get("userPrincipalName")
            if upn:
                desired[upn.lower()] = (upn, "auditor", m.get("displayName") or upn)
    for m in await graph.get_group_members(admin_group):
        upn = m.get("userPrincipalName")
        if upn:
            desired[upn.lower()] = (upn, "admin", m.get("displayName") or upn)

    synced = 0
    for upn, role, name in desired.values():
        user = await user_repo.get_by_username(session, upn)
        if user is None:
            await user_repo.create(
                session,
                username=upn,
                password_hash=hash_password(uuid.uuid4().hex),
                display_name=name,
                role=role,
                is_sso=True,
                tenant_id=tenant_id,
            )
        elif not user.is_sso:
            # A local (non-SSO) account owns this UPN. Never adopt it into SSO (would silently
            # let group membership take over a local admin/superadmin). Skip it entirely.
            log.warning("sso_sync_local_account_conflict", username=upn, role=user.role)
            continue
        else:
            user.is_sso = True
            user.display_name = name
            user.role = role
            user.is_active = True
        synced += 1

    # SSO-Benutzer, die in keiner berechtigten Gruppe mehr sind, entfernen -- NUR die
    # dieses Mandanten (siehe Sicherheitsfix-Hinweis im Docstring oben).
    existing_sso = await user_repo.list_sso_for_tenant(session, tenant_id)
    to_remove = [u.id for u in existing_sso if u.username.lower() not in desired]

    blocked = removal_blocked_reason(
        desired_count=len(desired),
        existing_count=len(existing_sso),
        removal_count=len(to_remove),
    )
    if blocked:
        await session.commit()  # Rollen-/Namensabgleich oben behalten, nur nicht löschen
        log.error(
            "sso_removal_blocked",
            reason=blocked,
            desired=len(desired),
            existing=len(existing_sso),
            would_remove=len(to_remove),
        )
        return {"synced": synced, "removed": 0, "removal_blocked": 1}

    for uid in to_remove:
        if uid is not None:
            await user_repo.delete(session, uid)

    await session.commit()
    log.info("sso_users_synced", synced=synced, removed=len(to_remove))
    return {"synced": synced, "removed": len(to_remove)}
