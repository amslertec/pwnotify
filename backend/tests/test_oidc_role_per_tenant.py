"""Sicherheitsfix 2 (Phase 4c Task 4): SSO-Rolle wird AUTORITATIV aus den Gruppen-Settings
DES per `tid` gefundenen Kunden bestimmt -- nicht aus einem instanzweiten Gemisch.

Hintergrund: `oidc_callback` liest die Settings zunächst mit `SettingsService(session).get_all()`
auf der ÜBERGEBENEN (Owner-)Session -- weil RLS für die Owner-Rolle nicht greift, liefert das
sobald ≥2 SSO-Kunden existieren ein UNDEFINIERTES Gemisch der `oidc.admin_group_id`/
`oidc.auditor_group_id`-Zeilen ALLER Kunden. `oidc.exchange_and_verify` berechnete daraus
bisher direkt `role`/`allowed` -- ein Benutzer könnte so in Kunde B als Admin durchgehen, weil
er (nur) in Kunde A's Admin-Gruppe ist (oder umgekehrt fälschlich abgelehnt werden). Der Fix
bestimmt die Rolle NACH der `tid` -> Tenant-Auflösung ein zweites Mal, AUTORITATIV, gegen die
per `tenant_scoped_session(tenant.id)` gelesenen Settings DES GEFUNDENEN Kunden
(`oidc.resolve_role(result.groups, tenant_settings)`).

Kernbeweis (Test 1, nicht vakuum): DERSELBE Benutzer ist Mitglied von Kunde A's Admin-Gruppe
UND Kunde B's Auditor-Gruppe (disjunkte Gruppen-IDs je Kunde). Ein und dasselbe, FESTE
`OidcResult` (`role="admin"`, `allowed=True` -- das wäre das Ergebnis eines instanzweiten
Gemischs, das z. B. zufällig A's Zeile "gewinnt") wird für den Login gegen Kunde A UND gegen
Kunde B verwendet. Nach Kunde A muss die Rolle "admin" sein (aus A's eigener Gruppe), nach
Kunde B muss sie "auditor" sein (aus B's eigener Gruppe) -- NICHT die im `OidcResult` fest
codierte "admin"-Rolle. Vor dem Fix übernahm der Callback `result.role` unverändert für BEIDE
Logins -- der Test schlägt daher am Kunde-B-Fall zwingend fehl, wenn der Fix nicht greift.

Test 2 (Verweigerung): ein Benutzer mit Gruppen-Claim, der aber in KEINER der beiden Gruppen
DES per `tid` gefundenen Kunden ist, wird abgelehnt (auditiert), obwohl `OidcResult.allowed`
(Instanz-Gemisch) `True` sagt -- vor dem Fix hätte der Callback das ungeprüft übernommen und
angemeldet.

Seed/Cleanup wie in `test_oidc_tenant_mapping.py` begründet: `tenant_scoped_session` (im
Callback, nach der Tenant-Auflösung) öffnet eine ECHTE, zweite Verbindung -- Tenants + deren
Gruppen-Settings müssen daher echt committet sein (Muster aus `test_isolation_attack.py`).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from app.api.deps import OIDC_FLOW_COOKIE
from app.api.routes.auth import oidc_callback
from app.db.session import get_session_factory
from app.models.audit import AuditLog
from app.repositories import user_repo
from app.services import oidc
from app.services.audit import LOGIN_FAILED
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

from tests.test_oidc_group_auth import _oidc_limiter_disabled  # noqa: F401

_USER_DOMAIN = "@roletenant.test"

_TID_A = "role-per-tenant-tid-a"
_TID_B = "role-per-tenant-tid-b"
_A_ADMIN_GROUP = "role-per-tenant-a-admins"
_B_ADMIN_GROUP = "role-per-tenant-b-admins"  # bewusst verschieden von A's Gruppe
_B_AUDITOR_GROUP = "role-per-tenant-b-auditors"


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


class _RoleTenants:
    a: int
    b: int


@pytest_asyncio.fixture
async def role_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[_RoleTenants]:
    """Kunde A (nur Admin-Gruppe konfiguriert) + Kunde B (eigene, DISJUNKTE Admin- UND
    Auditor-Gruppe) -- echt committet, damit `tenant_scoped_session` (eigene Verbindung im
    Callback) sie sieht."""
    async with migrated_engine.connect() as conn:
        a = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                    "VALUES ('RolePerTenantA','role-per-tenant-a',:tid,true,now()) RETURNING id"
                ),
                {"tid": _TID_A},
            )
        ).scalar_one()
        b = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                    "VALUES ('RolePerTenantB','role-per-tenant-b',:tid,true,now()) RETURNING id"
                ),
                {"tid": _TID_B},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:a, 'oidc.admin_group_id', to_jsonb(CAST(:a_admin AS text)), false, now()), "
                "(:b, 'oidc.admin_group_id', to_jsonb(CAST(:b_admin AS text)), false, now()), "
                "(:b, 'oidc.auditor_group_id', to_jsonb(CAST(:b_auditor AS text)), false, now())"
            ),
            {
                "a": a,
                "b": b,
                "a_admin": _A_ADMIN_GROUP,
                "b_admin": _B_ADMIN_GROUP,
                "b_auditor": _B_AUDITOR_GROUP,
            },
        )
        await conn.commit()
        fx = _RoleTenants()
        fx.a, fx.b = a, b
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
            # tenant-Delete kaskadiert per ON DELETE CASCADE auf deren eigene `setting`-Zeilen.
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


# ---- Kernbeweis: dieselbe Person -> admin in A (A's eigene Gruppe), auditor in B (B's) ---- #


async def test_same_user_gets_role_from_matched_tenants_own_groups(
    role_tenants: _RoleTenants,
) -> None:
    username = f"shared-user{_USER_DOMAIN}"
    session_factory = get_session_factory()

    # Der Benutzer ist Mitglied von A's Admin-Gruppe UND B's Auditor-Gruppe -- ein instanzweites
    # Gemisch (vor dem Fix) hätte für BEIDE Logins dieselbe, hier fest codierte Rolle
    # ("admin") verwendet, unabhängig vom tatsächlich gematchten Kunden.
    groups = [_A_ADMIN_GROUP, _B_AUDITOR_GROUP]

    result_a = oidc.OidcResult(
        username=username,
        display_name="Shared User",
        allowed=True,
        role="admin",  # fix codiert -- der Fix darf das NICHT einfach übernehmen
        tid=_TID_A,
        groups=groups,
    )
    async with session_factory() as session:
        resp_a = await _call_oidc_callback(session, result_a)
    assert "sso_denied" not in resp_a.headers["location"]
    assert "sso_error" not in resp_a.headers["location"]

    async with session_factory() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.tenant_id == role_tenants.a
        assert user.role == "admin", "Muss aus A's EIGENER Admin-Gruppe kommen"

    # DIESELBE Person, DASSELBE fest codierte `OidcResult.role="admin"` -- diesmal aber mit
    # tid=B. Ohne den Fix bliebe die Rolle "admin" (aus `result.role` übernommen). Mit dem
    # Fix wird sie NACH der Tenant-Auflösung gegen B's EIGENE Gruppen neu bestimmt: der
    # Benutzer ist in B's Auditor-Gruppe (nicht in B's -- andersartiger -- Admin-Gruppe),
    # muss also "auditor" werden.
    result_b = oidc.OidcResult(
        username=username,
        display_name="Shared User",
        allowed=True,
        role="admin",
        tid=_TID_B,
        groups=groups,
    )
    async with session_factory() as session:
        resp_b = await _call_oidc_callback(session, result_b)
    assert "sso_denied" not in resp_b.headers["location"]
    assert "sso_error" not in resp_b.headers["location"]

    async with session_factory() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.tenant_id == role_tenants.b
        assert user.role == "auditor", (
            "Muss aus B's EIGENER Auditor-Gruppe kommen, NICHT der instanzweit-Gemisch-Rolle "
            "'admin' aus dem OidcResult"
        )


# ---- Verweigerung: in keiner Gruppe DES gefundenen Kunden -> Login abgelehnt ------------- #


async def test_user_not_in_matched_tenants_groups_is_denied(role_tenants: _RoleTenants) -> None:
    username = f"denied-user{_USER_DOMAIN}"
    session_factory = get_session_factory()

    result = oidc.OidcResult(
        username=username,
        display_name="Denied User",
        allowed=True,  # Instanz-Gemisch sagt "erlaubt" -- darf NICHT über Kunde B entscheiden
        role="admin",
        tid=_TID_B,
        groups=["some-unrelated-group"],  # weder B's Admin- noch B's Auditor-Gruppe
    )
    async with session_factory() as session:
        resp = await _call_oidc_callback(session, result)

    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with session_factory() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is None, "Ohne Mitgliedschaft in B's Gruppen darf kein Konto entstehen"

        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == username))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.outcome == "failure"
        assert row.detail.get("sso") is True
