"""SSO `tid` -> Tenant-Mapping (Phase 4a Task 4).

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

Läuft auf der gewöhnlichen, savepoint-isolierten `session`-Fixture (wie in
`test_tenant_authorization.py` und Test 3 aus `test_active_tenant_resolution.py`
begründet): `oidc_callback` führt seine Queries ausschliesslich über die übergebene
`AsyncSession` aus -- anders als `get_tenant_session` öffnet es KEINE eigene Verbindung
über `get_session_factory()`. Die Savepoint-Rücksetzung aus `conftest.py` räumt daher
rückstandsfrei auf, ein echtes Superuser-Commit-Setup ist hier nicht nötig.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, patch

from app.api.routes.auth import oidc_callback
from app.core.security import decode_token
from app.models.audit import AuditLog
from app.models.setting import Setting
from app.models.tenant import Tenant
from app.models.user import UserSession
from app.repositories import tenant_repo, user_repo
from app.services import oidc
from app.services.audit import LOGIN_FAILED
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

_INSTANCE_TID = "instance-tid-configured-for-this-app"


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


async def _seed_configured_tenant_id(session: AsyncSession) -> None:
    """`graph.tenant_id` steht auf der Default-Tenant-Zeile -- der Tenant-Bezug der
    Setting-Zeile selbst spielt hier keine Rolle: `oidc_callback` liest die Settings mit
    einer NICHT tenant-gescopten Session (siehe dessen Kommentar zu Task 3/4), das ist ein
    bekannter, ausserhalb dieses Tasks liegender Punkt (Scope-Note der Aufgabe)."""
    default = await tenant_repo.default_tenant(session)
    session.add(
        Setting(tenant_id=default.id, key="graph.tenant_id", value=_INSTANCE_TID, is_secret=False)
    )
    await session.flush()


async def _mk_tenant(session: AsyncSession, *, slug: str, entra_tenant_id: str) -> Tenant:
    t = Tenant(name=slug, slug=slug, entra_tenant_id=entra_tenant_id, is_active=True)
    session.add(t)
    await session.flush()
    return t


async def _call_oidc_callback(session: AsyncSession, result: oidc.OidcResult) -> RedirectResponse:
    request = _FakeRequest()
    state = oidc.sign_state()
    with patch("app.services.oidc.exchange_and_verify", new=AsyncMock(return_value=result)):
        resp = await oidc_callback(
            request,  # type: ignore[arg-type]
            session,
            code="fake-code",
            state=state,
            error=None,
        )
    assert isinstance(resp, RedirectResponse)
    return resp


# ---- 1. Bekannter `tid` eines ZWEITEN Tenants -------------------------------------------- #


async def test_known_tid_binds_user_to_that_tenant(session: AsyncSession) -> None:
    await _seed_configured_tenant_id(session)
    tenant_b = await _mk_tenant(session, slug="oidc-tenant-b", entra_tenant_id="tenant-b-tid")

    result = oidc.OidcResult(
        username="alice@tenantb.example",
        display_name="Alice",
        allowed=True,
        role="admin",
        tid="tenant-b-tid",
    )
    resp = await _call_oidc_callback(session, result)

    assert "sso_denied" not in resp.headers["location"]
    assert "sso_error" not in resp.headers["location"]

    user = await user_repo.get_by_username(session, "alice@tenantb.example")
    assert user is not None
    assert user.tenant_id == tenant_b.id, "Muss an den per tid gefundenen Tenant B gebunden sein"

    access_token = _extract_cookie(resp, "pwnotify_access")
    assert access_token is not None
    payload = decode_token(access_token, expected_type="access")
    assert payload["active_tenant"] == tenant_b.id, "Token muss active_tenant=Tenant-B tragen"

    us = (
        await session.execute(select(UserSession).where(UserSession.user_id == user.id))
    ).scalar_one()
    assert us.active_tenant_id == tenant_b.id


# ---- 2. tid == graph.tenant_id, Default-Tenant noch ohne entra_tenant_id (Bootstrap) ----- #


async def test_configured_tid_falls_back_to_default_tenant_and_bootstraps(
    session: AsyncSession,
) -> None:
    await _seed_configured_tenant_id(session)
    default = await tenant_repo.default_tenant(session)
    assert default.entra_tenant_id is None, "Testvoraussetzung: Default-Tenant noch ungebunden"

    result = oidc.OidcResult(
        username="bob@instance.example",
        display_name="Bob",
        allowed=True,
        role="admin",
        tid=_INSTANCE_TID,
    )
    resp = await _call_oidc_callback(session, result)

    assert "sso_denied" not in resp.headers["location"]
    assert "sso_error" not in resp.headers["location"]

    user = await user_repo.get_by_username(session, "bob@instance.example")
    assert user is not None
    assert user.tenant_id == default.id

    access_token = _extract_cookie(resp, "pwnotify_access")
    assert access_token is not None
    payload = decode_token(access_token, expected_type="access")
    assert payload["active_tenant"] == default.id

    # Bootstrap: die Default-Tenant-Zeile trägt jetzt den tid -- künftige Logins matchen
    # direkt über `get_by_entra_tid`, ohne erneuten Fallback.
    refreshed = await tenant_repo.get(session, default.id)  # type: ignore[arg-type]
    assert refreshed is not None
    assert refreshed.entra_tenant_id == _INSTANCE_TID


# ---- 3. Unbekannter/fremder tid -> verweigert, Audit, keine Sitzung --------------------- #


async def test_unknown_tid_is_refused_with_audit_and_no_session(session: AsyncSession) -> None:
    await _seed_configured_tenant_id(session)
    await _mk_tenant(session, slug="oidc-tenant-known", entra_tenant_id="some-other-known-tid")

    result = oidc.OidcResult(
        username="eve@foreign.example",
        display_name="Eve",
        allowed=True,
        role="admin",
        tid="totally-unknown-foreign-tid",
    )
    resp = await _call_oidc_callback(session, result)

    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    # Kein Konto angelegt, keine Sitzung -- der Angriff/Fehlversuch bleibt folgenlos.
    user = await user_repo.get_by_username(session, "eve@foreign.example")
    assert user is None

    row = (
        await session.execute(
            select(AuditLog).where(AuditLog.actor_username == "eve@foreign.example")
        )
    ).scalar_one()
    assert row.action == LOGIN_FAILED
    assert row.outcome == "failure"
    assert row.detail.get("reason") == "unknown_tenant"

    # Keine Set-Cookie-Header fürs Access-/Refresh-Token -- keine Anmeldung ausgestellt.
    assert _extract_cookie(resp, "pwnotify_access") is None
