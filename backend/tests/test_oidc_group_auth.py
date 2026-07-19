"""SICHERHEITSKRITISCH (Group-Roles Task 4 + Provider-only SSO, Tenant-Refinements Task 4):
SSO-Login-AUTORISIERUNG für Provider-Personal.

Im Multi-Tenant-Mode ist SSO ausschliesslich ein Provider-Personal-Login: JEDER SSO-Login
(auch mit Kunden-`tid`) entscheidet über Team-Mitgliedschaften (`AssignmentGroup`) und wird
auf dem DEFAULT-Tenant beheimatet -- NICHT über die per-Kunde-Rollen-Gruppen-Settings
(`oidc.resolve_role`). Nur der SINGLE-TENANT-Mode bleibt byte-genau bei `resolve_role`.

Angriffs-orientiert:
- Provider ohne passendes Team -> fail-closed abgelehnt (kein Settings-Fallback).
- Auditor-Team -> `auditor`; Admin- + Auditor-Team -> `admin` (Admin gewinnt).
- Kunden-`tid` im Multi-Tenant-Mode -> group-basiert + default-beheimatet (nicht der Kunde);
  ohne Team abgelehnt, selbst wenn die Settings-Admin-Gruppe des Kunden matcht.
- Single-Tenant-Mode ignoriert Teams komplett (Settings entscheiden).
- Der Verweigerungs-Grund wird in BEIDEN Zweigen auditiert.

Seed-Muster wie `test_oidc_role_per_tenant.py`: der Callback öffnet echte Zweit-Verbindungen
(`tenant_scoped_session`, `read_mode`, `get_session_factory`), daher muss ALLES echt committet
sein. Der Default-Tenant wird geteilt -- sein `entra_tenant_id`/seine Settings werden im
Finally exakt zurückgesetzt, und jeder Test setzt den Multi-Tenant-Mode selbst (kein
persistenter Fixture-Zustand, der die Reihenfolge der Tests verletzen könnte).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from app.api.deps import OIDC_FLOW_COOKIE
from app.api.routes.auth import oidc_callback
from app.db.session import get_session_factory
from app.models.audit import AuditLog
from app.models.user import UserSession
from app.repositories import user_repo
from app.services import oidc
from app.services.audit import LOGIN_FAILED
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

_USER_DOMAIN = "@groupauth.test"

_TID_DEFAULT = "group-auth-default-tid"
_TID_CUSTOMER = "group-auth-customer-tid"

# Provider-Teams (instanzweit, kein Tenant-Scope).
_TEAM_ADMIN_GROUP = "group-auth-team-admins"
_TEAM_AUDITOR_GROUP = "group-auth-team-auditors"

# Settings-Rollen-Gruppen (per-Kunde) -- bewusst DISJUNKT von den Team-Gruppen.
_DEFAULT_SETTINGS_ADMIN_GROUP = "group-auth-default-settings-admins"
_CUSTOMER_SETTINGS_ADMIN_GROUP = "group-auth-customer-settings-admins"


class _FakeRequest:
    """Duck-typed Request -- `oidc_callback`/`audit.record` lesen nur diese Attribute."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client: object | None = None
        self.cookies: dict[str, str] = {}


async def _call_oidc_callback(session: AsyncSession, result: oidc.OidcResult) -> RedirectResponse:
    request = _FakeRequest()
    request.cookies = {OIDC_FLOW_COOKIE: oidc.encode_flow_cookie({"state": "x", "nonce": "n"})}
    with patch("app.services.oidc.exchange_and_verify", new=AsyncMock(return_value=result)):
        resp = await oidc_callback(
            request,  # type: ignore[arg-type]
            session,
            code="fake-code",
            state="x",
            error=None,
        )
    assert isinstance(resp, RedirectResponse)
    return resp


class _Env:
    default_id: int
    customer_id: int


async def _write_mode(engine: AsyncEngine, default_id: int, value: bool) -> None:
    """Multi-Tenant-Mode default-tenant-gescopt setzen (committet, damit der Callback ihn
    über seine eigene `read_mode`-Verbindung sieht). Jeder Test setzt ihn selbst -- so hängt
    kein Test von der Reihenfolge ab."""
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:tid, 'instance.multi_tenant_mode', to_jsonb(CAST(:val AS boolean)), "
                "false, now()) "
                "ON CONFLICT (tenant_id, key) DO UPDATE "
                "SET value = EXCLUDED.value, updated_at = now()"
            ),
            {"tid": default_id, "val": value},
        )
        await conn.commit()


@pytest_asyncio.fixture
async def env(migrated_engine: AsyncEngine) -> AsyncGenerator[_Env]:
    async with migrated_engine.connect() as conn:
        default_id = (
            await conn.execute(text("SELECT id FROM tenant WHERE is_default"))
        ).scalar_one()
        orig_entra = (
            await conn.execute(
                text("SELECT entra_tenant_id FROM tenant WHERE id = :id"), {"id": default_id}
            )
        ).scalar_one()

        # Default-Tenant an einen bekannten `tid` binden, damit ein Provider-Login darauf matcht.
        await conn.execute(
            text("UPDATE tenant SET entra_tenant_id = :tid WHERE id = :id"),
            {"tid": _TID_DEFAULT, "id": default_id},
        )
        # Default-Settings-Admin-Gruppe (nur der Single-Tenant-Pfad nutzt sie).
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:id, 'oidc.admin_group_id', to_jsonb(CAST(:g AS text)), false, now()) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"id": default_id, "g": _DEFAULT_SETTINGS_ADMIN_GROUP},
        )
        # Kunde A: eigener `tid` + eigene Settings-Admin-Gruppe (Kunden-Pfad).
        customer_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                    "VALUES ('GroupAuthCustomer','group-auth-customer',:tid,true,now()) "
                    "RETURNING id"
                ),
                {"tid": _TID_CUSTOMER},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:id, 'oidc.admin_group_id', to_jsonb(CAST(:g AS text)), false, now())"
            ),
            {"id": customer_id, "g": _CUSTOMER_SETTINGS_ADMIN_GROUP},
        )
        # Provider-Teams (instanzweit).
        await conn.execute(
            text(
                "INSERT INTO assignment_group (name, entra_group_id, role, created_at) VALUES "
                "('AdminTeam', :admin, 'admin', now()), "
                "('AuditorTeam', :auditor, 'auditor', now())"
            ),
            {"admin": _TEAM_ADMIN_GROUP, "auditor": _TEAM_AUDITOR_GROUP},
        )
        await conn.commit()

        fx = _Env()
        fx.default_id, fx.customer_id = default_id, customer_id
        try:
            yield fx
        finally:
            await conn.execute(
                text(f"DELETE FROM audit_log WHERE actor_username LIKE '%{_USER_DOMAIN}'")
            )
            await conn.execute(
                text(
                    "DELETE FROM user_session WHERE user_id IN "
                    f"(SELECT id FROM app_user WHERE username LIKE '%{_USER_DOMAIN}')"
                )
            )
            await conn.execute(text(f"DELETE FROM app_user WHERE username LIKE '%{_USER_DOMAIN}'"))
            await conn.execute(
                text("DELETE FROM assignment_group WHERE entra_group_id IN (:admin, :auditor)"),
                {"admin": _TEAM_ADMIN_GROUP, "auditor": _TEAM_AUDITOR_GROUP},
            )
            # Default-Tenant exakt zurücksetzen (geteilter State).
            await conn.execute(
                text(
                    "DELETE FROM setting WHERE tenant_id = :id AND key IN "
                    "('oidc.admin_group_id', 'instance.multi_tenant_mode')"
                ),
                {"id": default_id},
            )
            await conn.execute(
                text("UPDATE tenant SET entra_tenant_id = :orig WHERE id = :id"),
                {"orig": orig_entra, "id": default_id},
            )
            # Kunde A kaskadiert seine Settings per ON DELETE CASCADE.
            await conn.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": customer_id})
            await conn.commit()


def _result(username: str, tid: str, groups: list[str]) -> oidc.OidcResult:
    return oidc.OidcResult(
        username=username,
        display_name="X",
        allowed=True,  # Instanz-Gemisch -- darf NIE über die Zulassung entscheiden
        role="admin",  # fix codiert -- der group-basierte Pfad darf das NICHT übernehmen
        tid=tid,
        groups=groups,
    )


# ---- Provider ohne Team -> fail-closed abgelehnt ---------------------------------------- #


async def test_provider_no_team_denied(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"no-team{_USER_DOMAIN}"

    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_DEFAULT, ["unknown-grp"]))

    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, username) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == username))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.outcome == "failure"
        assert row.detail.get("sso") is True
        assert row.detail.get("reason") == "not_in_any_team"


# ---- Auditor-Team -> auditor ------------------------------------------------------------ #


async def test_provider_auditor_team_gets_auditor(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"auditor{_USER_DOMAIN}"

    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(username, _TID_DEFAULT, [_TEAM_AUDITOR_GROUP])
        )
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.tenant_id == env.default_id
        assert user.role == "auditor"


# ---- Admin-Team gewinnt (Admin + Auditor) ----------------------------------------------- #


async def test_provider_admin_team_wins(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"admin-wins{_USER_DOMAIN}"

    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(username, _TID_DEFAULT, [_TEAM_ADMIN_GROUP, _TEAM_AUDITOR_GROUP])
        )
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.role == "admin"


# ---- Kunden-`tid` im Multi-Tenant-Mode -> group-basiert + default-beheimatet ------------ #


async def test_customer_tid_in_mt_is_group_based_and_default_homed(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    """FLIP (Provider-only SSO Task 4): früher entschieden bei einem Kunden-`tid` die Settings
    des Kunden. Jetzt ist SSO im Multi-Tenant-Mode ausschliesslich Provider-Login -- der
    Kunden-`tid` wird group-basiert autorisiert und auf dem DEFAULT-Tenant beheimatet."""
    await _write_mode(migrated_engine, env.default_id, True)

    # (a) Provider-Team-Mitglied mit Kunden-`tid`: über TEAM autorisiert, Rolle folgt
    #     Admin-gewinnt, Heimat = DEFAULT-Tenant (NICHT der Kunde).
    ok = f"customer-tid-team-member{_USER_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(ok, _TID_CUSTOMER, [_TEAM_ADMIN_GROUP, _TEAM_AUDITOR_GROUP])
        )
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, ok)
        assert user is not None
        assert user.role == "admin"  # Admin-Team gewinnt gegen Auditor-Team
        assert user.tenant_id == env.default_id  # default-beheimatet, NICHT der Kunde

    # (b) In Kunde A's Settings-Admin-Gruppe, aber in KEINEM Team -> jetzt DENIED (kein
    #     Settings-Fallback im Multi-Tenant-Mode). Deny wird als `not_in_any_team` auditiert.
    denied = f"customer-settings-no-team{_USER_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(denied, _TID_CUSTOMER, [_CUSTOMER_SETTINGS_ADMIN_GROUP])
        )
    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, denied) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == denied))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.outcome == "failure"
        assert row.detail.get("sso") is True
        # Grund kommt jetzt aus dem TEAM-Pfad, nicht mehr aus `resolve_role`.
        assert row.detail.get("reason") == "not_in_any_team"


# ---- Unbekannter `tid` -> DENIED (unverändert, äussere Verteidigung) -------------------- #


async def test_unknown_tid_denied(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"unknown-tid{_USER_DOMAIN}"

    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(username, "group-auth-unknown-tid", [_TEAM_ADMIN_GROUP])
        )
    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, username) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == username))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.detail.get("reason") == "unknown_tenant"


# ---- Heimat + Sitzungs-`active_tenant` stimmen überein (== Default) ---------------------- #


async def test_home_and_session_active_tenant_agree_on_default(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    """Nach JEDEM erfolgreichen Multi-Tenant-SSO-Login MUSS `user.tenant_id` ==
    `user_session.active_tenant_id` == Default sein -- sonst bootet die Sitzung in einen
    Kontext, für den das Konto ggf. keinen Grant hält (siehe `resolve_initial_tenant`)."""
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"home-active-agree{_USER_DOMAIN}"

    # Kunden-`tid`, autorisiert über ein Provider-Team -> Heimat + active_tenant == Default.
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(username, _TID_CUSTOMER, [_TEAM_ADMIN_GROUP])
        )
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.tenant_id == env.default_id
        row = (
            await session.execute(select(UserSession).where(UserSession.user_id == user.id))
        ).scalar_one()
        assert row.active_tenant_id == env.default_id
        assert user.tenant_id == row.active_tenant_id == env.default_id


# ---- Single-Tenant-Mode ignoriert Teams (Settings entscheiden) -------------------------- #


async def test_single_tenant_mode_uses_settings(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, False)  # Multi-Tenant AUS

    # (a) Nur Team-Mitglied (nicht in der Settings-Admin-Gruppe) -> DENIED.
    denied = f"team-only{_USER_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(denied, _TID_DEFAULT, [_TEAM_ADMIN_GROUP])
        )
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, denied) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == denied))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.detail.get("reason")  # Verweigerungs-Grund auch hier auditiert

    # (b) In der Settings-Admin-Gruppe -> erlaubt (wie bisher).
    ok = f"settings-admin{_USER_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(ok, _TID_DEFAULT, [_DEFAULT_SETTINGS_ADMIN_GROUP])
        )
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, ok)
        assert user is not None
        assert user.role == "admin"
