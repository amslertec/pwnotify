"""SSO `tid` -> Tenant-Mapping (Phase 4a Task 4) + per-Kunde-Rollenauflösung (Phase 4c Task 4).

`oidc_callback` bindet ein SSO-Konto an GENAU den Tenant, dessen `entra_tenant_id` dem
`tid`-Claim des ID-Tokens entspricht -- niemals an "irgendeinen" Tenant. Drei Fälle:

1. Ein bekannter, fremder `tid` (ein ZWEITER Tenant hat genau diesen `entra_tenant_id`
   gesetzt) -> das Konto wird an DIESEN Tenant gebunden, der Token-Claim trägt seine Id.
2. `tid` entspricht der für DIESE Instanz konfigurierten `graph.tenant_id`, der
   Default-Tenant hat aber noch keinen `entra_tenant_id` (Übergang Single- -> Multi-Tenant)
   -> der Default-Tenant wird verwendet UND auf diesen `tid` gebootstrapt.
3. Ein unbekannter/fremder `tid` (kein Treffer, ungleich `graph.tenant_id`) -> die
   Anmeldung wird verweigert, mit Audit-Eintrag `LOGIN_FAILED`/`unknown_tenant`, ohne
   Sitzung.

Seit dem Sicherheitsfix aus Phase 4c Task 4 (Rolle wird NACH der Tenant-Auflösung
autoritativ gegen die Settings DES gefundenen Kunden neu bestimmt, siehe
`test_oidc_role_per_tenant.py`) liest `oidc_callback` diese Settings über
`tenant_scoped_session(tenant.id)` -- das öffnet eine EIGENE, echte Verbindung und sieht
daher KEINE uncommitteten Daten (empirisch bestätigt in `test_isolation_attack.py`). Die
vormals hier verwendete savepoint-isolierte `session`-Fixture (siehe conftest.py) reicht
darum nicht mehr aus: Tenants + Settings müssen echt committet werden, sonst würde die
Neuauflösung nur die (leeren) Default-Settings sehen und jede Anmeldung als "nicht in der
Gruppe" ablehnen. Seed/Cleanup folgt daher dem Muster aus `test_isolation_attack.py` /
`test_sso_sync_tenant_scope.py`: echte Superuser-Connection auf `migrated_engine`, echt
committet, Aufräumen im `finally` (inkl. Reset des geteilten Default-Tenants, dessen
`entra_tenant_id` Test 2 bewusst mutiert).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from app.api.deps import OIDC_FLOW_COOKIE
from app.api.routes.auth import oidc_callback
from app.core.security import decode_token
from app.db.session import get_session_factory
from app.models.audit import AuditLog
from app.repositories import tenant_repo, user_repo
from app.services import oidc
from app.services.audit import LOGIN_FAILED
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

from tests.test_oidc_group_auth import _oidc_limiter_disabled  # noqa: F401

_INSTANCE_TID = "instance-tid-configured-for-this-app"
_USER_DOMAIN = "@oidcmap.test"


class _FakeRequest:
    """Duck-typed Request -- `oidc_callback`/`audit.record` lesen nur diese Attribute."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client: object | None = None
        self.cookies: dict[str, str] = {}


def _extract_cookie(resp: RedirectResponse, name: str) -> str | None:
    for raw in resp.headers.getlist("set-cookie"):
        jar: SimpleCookie = SimpleCookie()
        jar.load(raw)
        if name in jar:
            return jar[name].value
    return None


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


class _MappingTenants:
    default_id: int
    tenant_b: int
    tenant_known: int


@pytest_asyncio.fixture
async def mapping_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[_MappingTenants]:
    """Default-Tenant (instanzweit konfiguriert, `entra_tenant_id` noch ungebunden) + Tenant
    B (bekannter `tid`, eigene Admin-Gruppe) + Tenant "known" (anderer, ebenfalls bekannter,
    aber für die Test-tid irrelevanter `tid`) -- alle echt committet."""
    async with migrated_engine.connect() as conn:
        default_id = (
            await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))
        ).scalar_one()
        tenant_b = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                    "VALUES ('OidcMapTenantB','oidc-map-tenant-b','tenant-b-tid',true,now()) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        tenant_known = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                    "VALUES ('OidcMapTenantKnown','oidc-map-tenant-known',"
                    "'some-other-known-tid',true,now()) RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:default_id, 'graph.tenant_id', to_jsonb(CAST(:instance_tid AS text)), "
                "false, now()), "
                "(:default_id, 'oidc.admin_group_id', to_jsonb('default-admins'::text), "
                "false, now()), "
                "(:tenant_b, 'oidc.admin_group_id', to_jsonb('b-admins'::text), false, now())"
            ),
            {"default_id": default_id, "instance_tid": _INSTANCE_TID, "tenant_b": tenant_b},
        )
        await conn.commit()
        fx = _MappingTenants()
        fx.default_id, fx.tenant_b, fx.tenant_known = default_id, tenant_b, tenant_known
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
                text(
                    "DELETE FROM setting WHERE tenant_id = :default_id "
                    "AND key IN ('graph.tenant_id', 'oidc.admin_group_id')"
                ),
                {"default_id": default_id},
            )
            # tenant-Delete kaskadiert per ON DELETE CASCADE auf deren eigene `setting`-Zeilen.
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b)"),
                {"a": tenant_b, "b": tenant_known},
            )
            await conn.execute(
                text("UPDATE tenant SET entra_tenant_id = NULL WHERE id = :default_id"),
                {"default_id": default_id},
            )
            await conn.commit()


# ---- 1. Bekannter `tid` eines ZWEITEN Tenants -------------------------------------------- #


async def test_known_tid_binds_user_to_that_tenant(mapping_tenants: _MappingTenants) -> None:
    result = oidc.OidcResult(
        username=f"alice{_USER_DOMAIN}",
        display_name="Alice",
        allowed=True,
        role="admin",
        tid="tenant-b-tid",
        groups=["b-admins"],
    )
    session_factory = get_session_factory()
    async with session_factory() as session:
        resp = await _call_oidc_callback(session, result)

    assert "sso_denied" not in resp.headers["location"]
    assert "sso_error" not in resp.headers["location"]

    async with session_factory() as session:
        user = await user_repo.get_by_username(session, f"alice{_USER_DOMAIN}")
        assert user is not None
        assert user.tenant_id == mapping_tenants.tenant_b, (
            "Muss an den per tid gefundenen Tenant B gebunden sein"
        )
        assert user.role == "admin", "Rolle muss aus Tenant B's eigener Admin-Gruppe kommen"

    access_token = _extract_cookie(resp, "pwnotify_access")
    assert access_token is not None
    payload = decode_token(access_token, expected_type="access")
    assert payload["active_tenant"] == mapping_tenants.tenant_b, (
        "Token muss active_tenant=Tenant-B tragen"
    )


# ---- 2. tid == graph.tenant_id, Default-Tenant noch ohne entra_tenant_id (Bootstrap) ----- #


async def test_configured_tid_falls_back_to_default_tenant_and_bootstraps(
    mapping_tenants: _MappingTenants,
) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        default = await tenant_repo.default_tenant(session)
        assert default.entra_tenant_id is None, "Testvoraussetzung: Default-Tenant noch ungebunden"

    result = oidc.OidcResult(
        username=f"bob{_USER_DOMAIN}",
        display_name="Bob",
        allowed=True,
        role="admin",
        tid=_INSTANCE_TID,
        groups=["default-admins"],
    )
    async with session_factory() as session:
        resp = await _call_oidc_callback(session, result)

    assert "sso_denied" not in resp.headers["location"]
    assert "sso_error" not in resp.headers["location"]

    async with session_factory() as session:
        user = await user_repo.get_by_username(session, f"bob{_USER_DOMAIN}")
        assert user is not None
        assert user.tenant_id == mapping_tenants.default_id
        assert user.role == "admin"

    access_token = _extract_cookie(resp, "pwnotify_access")
    assert access_token is not None
    payload = decode_token(access_token, expected_type="access")
    assert payload["active_tenant"] == mapping_tenants.default_id

    # Bootstrap: die Default-Tenant-Zeile trägt jetzt den tid -- künftige Logins matchen
    # direkt über `get_by_entra_tid`, ohne erneuten Fallback.
    async with session_factory() as session:
        refreshed = await tenant_repo.get(session, mapping_tenants.default_id)  # type: ignore[arg-type]
        assert refreshed is not None
        assert refreshed.entra_tenant_id == _INSTANCE_TID


# ---- 3. Unbekannter/fremder tid -> verweigert, Audit, keine Sitzung --------------------- #


async def test_unknown_tid_is_refused_with_audit_and_no_session(
    mapping_tenants: _MappingTenants,
) -> None:
    result = oidc.OidcResult(
        username=f"eve{_USER_DOMAIN}",
        display_name="Eve",
        allowed=True,
        role="admin",
        tid="totally-unknown-foreign-tid",
        groups=["irrelevant-group"],
    )
    session_factory = get_session_factory()
    async with session_factory() as session:
        resp = await _call_oidc_callback(session, result)

    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with session_factory() as session:
        # Kein Konto angelegt, keine Sitzung -- der Angriff/Fehlversuch bleibt folgenlos.
        user = await user_repo.get_by_username(session, f"eve{_USER_DOMAIN}")
        assert user is None

        row = (
            await session.execute(
                select(AuditLog).where(AuditLog.actor_username == f"eve{_USER_DOMAIN}")
            )
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.outcome == "failure"
        assert row.detail.get("reason") == "unknown_tenant"

    # Keine Set-Cookie-Header fürs Access-/Refresh-Token -- keine Anmeldung ausgestellt.
    assert _extract_cookie(resp, "pwnotify_access") is None
