"""Tests für Phase 4a Task 5: `POST /auth/switch-tenant`, `UserOut.active_tenant`/
`switchable_tenants`, und dass `refresh` den aktiven Mandanten über die Rotation hinweg
beibehält.

`switch_tenant`/`refresh`/`me` nehmen die Session als Parameter entgegen und öffnen KEINE
eigene (anders als `get_tenant_session`, das über `get_session_factory()` eine eigene
Verbindung aufmacht) -- deshalb reicht hier überall die gewöhnliche, savepoint-isolierte
`session`-Fixture: alles läuft auf derselben Verbindung, kein Cross-Connection-Problem
(vgl. Kommentar in `test_active_tenant_resolution.py` zu deren drittem Test). Kein
Cleanup nötig, die Fixture rollt am Testende zurück.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE
from app.api.routes.auth import me, refresh, switch_tenant
from app.core.config import get_settings
from app.core.errors import AuthError, ForbiddenError
from app.core.security import decode_token, hash_token, issue_token_pair
from app.models._base import utcnow
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import UserSession
from app.repositories import user_repo
from app.schemas.auth import SwitchTenantRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeRequest:
    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}
        self.deleted_cookies: set[str] = set()

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value

    def delete_cookie(self, name: str, **_: object) -> None:
        self.deleted_cookies.add(name)


async def _make_tenant(session: AsyncSession, *, name: str, slug: str, active: bool = True) -> int:
    t = Tenant(name=name, slug=slug, is_active=active)
    session.add(t)
    await session.commit()
    await session.refresh(t)
    assert t.id is not None
    return t.id


async def _make_local_admin(
    session: AsyncSession, *, username: str, admin_tenant_ids: list[int] | None = None
) -> int:
    """Lokaler (Nicht-Super-)Admin. Access-Modell-Design §2: seit der Verschärfung ist er
    NICHT mehr instanzweit -- ohne `admin_tenant_ids` hat er ZERO zugreifbare Tenants
    (`is_allowed` denyt jeden Tenant). Tests, die den alten "Admin sieht/darf alles"-Pfad
    treiben wollen, müssen die betroffenen Tenants hier explizit zuweisen; ein `superadmin`
    (siehe `_make_superadmin`) ist instanzweit und braucht keine Zuweisung."""
    user = await user_repo.create(
        session, username=username, password_hash="x", role="admin", is_sso=False
    )
    assert user.id is not None
    for tid in admin_tenant_ids or []:
        session.add(AdminTenant(user_id=user.id, tenant_id=tid))
    if admin_tenant_ids:
        await session.commit()
    return user.id


async def _make_superadmin(session: AsyncSession, *, username: str) -> int:
    user = await user_repo.create(
        session, username=username, password_hash="x", role="superadmin", is_sso=False
    )
    assert user.id is not None
    return user.id


async def _make_local_auditor(session: AsyncSession, *, username: str, tenant_id: int) -> int:
    user = await user_repo.create(
        session, username=username, password_hash="x", role="auditor", is_sso=False
    )
    assert user.id is not None
    session.add(AuditorTenant(user_id=user.id, tenant_id=tenant_id))
    await session.commit()
    return user.id


async def _make_sso_user(session: AsyncSession, *, username: str, tenant_id: int) -> int:
    user = await user_repo.create(
        session, username=username, password_hash="x", role="admin", is_sso=True
    )
    assert user.id is not None
    user.tenant_id = tenant_id
    await session.commit()
    return user.id


async def _seed_session(
    session: AsyncSession, *, user_id: int, active_tenant_id: int | None
) -> tuple[UserSession, str]:
    """Legt eine echte `user_session`-Zeile an und gibt sie + den passenden Refresh-Token
    zurück (für den `_FakeRequest`-Cookie)."""
    pair = issue_token_pair(str(user_id), active_tenant=active_tenant_id)
    us = await user_repo.create_session(
        session,
        user_id=user_id,
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=None,
        ip=None,
        active_tenant_id=active_tenant_id,
    )
    return us, pair.refresh_token


# ---- switch-tenant: erlaubter Wechsel ---------------------------------------------- #


async def test_switch_to_allowed_tenant_reissues_token_and_persists(session: AsyncSession) -> None:
    a = await _make_tenant(session, name="SwtA", slug="swt-a")
    b = await _make_tenant(session, name="SwtB", slug="swt-b")
    admin_id = await _make_local_admin(session, username="swt-admin@local", admin_tenant_ids=[a, b])
    user = await user_repo.get(session, admin_id)
    assert user is not None
    us, refresh_token = await _seed_session(session, user_id=admin_id, active_tenant_id=a)
    # Wert VORAB als reiner String festhalten -- `us`/`row` sind (dieselbe Session, gleiche
    # PK) über SQLAlchemys Identity-Map am Ende dasselbe Python-Objekt; ein Attribut-
    # Vergleich `row.x != us.x` wäre sonst immer trivial gleich (Vergleich mit sich selbst).
    original_jti = us.refresh_jti

    request = _FakeRequest({REFRESH_COOKIE: refresh_token})
    response = _FakeResponse()
    out = await switch_tenant(
        request,  # type: ignore[arg-type]
        response,  # type: ignore[arg-type]
        SwitchTenantRequest(tenant_id=b),
        user,
        session,
    )

    assert out.active_tenant is not None
    assert out.active_tenant.id == b

    access_token = response.cookie_values[ACCESS_COOKIE]
    payload = decode_token(access_token, expected_type="access")
    assert payload["active_tenant"] == b

    row = (await session.execute(select(UserSession).where(UserSession.id == us.id))).scalar_one()
    assert row.active_tenant_id == b, "user_session.active_tenant_id wurde nicht aktualisiert"
    assert row.refresh_jti != original_jti, "Refresh-Token wurde nicht rotiert"


# ---- switch-tenant: verweigerter Wechsel -> 403, keine Änderung -------------------- #


async def test_switch_to_disallowed_tenant_is_forbidden_and_does_not_mutate(
    session: AsyncSession,
) -> None:
    a = await _make_tenant(session, name="SwtC", slug="swt-c")
    b_foreign = await _make_tenant(session, name="SwtDForeign", slug="swt-d-foreign")
    auditor_id = await _make_local_auditor(session, username="swt-auditor@local", tenant_id=a)
    user = await user_repo.get(session, auditor_id)
    assert user is not None
    us, refresh_token = await _seed_session(session, user_id=auditor_id, active_tenant_id=a)
    original_jti = us.refresh_jti

    request = _FakeRequest({REFRESH_COOKIE: refresh_token})
    response = _FakeResponse()

    with pytest.raises(ForbiddenError):
        await switch_tenant(
            request,  # type: ignore[arg-type]
            response,  # type: ignore[arg-type]
            SwitchTenantRequest(tenant_id=b_foreign),
            user,
            session,
        )

    assert not response.cookie_values, "Bei 403 dürfen keine Cookies gesetzt worden sein"
    row = (await session.execute(select(UserSession).where(UserSession.id == us.id))).scalar_one()
    assert row.active_tenant_id == a, "Verweigerter Wechsel darf die Sitzung nicht ändern"
    assert row.refresh_jti == original_jti, "Verweigerter Wechsel darf das Token nicht rotieren"


# ---- refresh() erhält den aktiven Mandanten ----------------------------------------- #


async def test_refresh_preserves_active_tenant(session: AsyncSession) -> None:
    a = await _make_tenant(session, name="SwtE", slug="swt-e")
    admin_id = await _make_local_admin(session, username="swt-refresh-admin@local")
    us, refresh_token = await _seed_session(session, user_id=admin_id, active_tenant_id=a)

    request = _FakeRequest({REFRESH_COOKIE: refresh_token})
    response = _FakeResponse()
    out = await refresh(request, response, session)  # type: ignore[arg-type]

    assert out.active_tenant is not None
    assert out.active_tenant.id == a

    access_token = response.cookie_values[ACCESS_COOKIE]
    payload = decode_token(access_token, expected_type="access")
    assert payload["active_tenant"] == a, (
        f"active_tenant-Claim ging beim Refresh verloren: {payload.get('active_tenant')}"
    )

    row = (await session.execute(select(UserSession).where(UserSession.id == us.id))).scalar_one()
    assert row.active_tenant_id == a


async def test_refresh_without_active_tenant_stays_none(session: AsyncSession) -> None:
    """Randfall: eine Sitzung ohne aktiven Mandanten (z. B. Auditor ohne Zuweisung) darf
    beim Refresh keinen Claim erfinden."""
    admin_id = await _make_local_admin(session, username="swt-refresh-none@local")
    _, refresh_token = await _seed_session(session, user_id=admin_id, active_tenant_id=None)

    request = _FakeRequest({REFRESH_COOKIE: refresh_token})
    response = _FakeResponse()
    out = await refresh(request, response, session)  # type: ignore[arg-type]

    assert out.active_tenant is None
    access_token = response.cookie_values[ACCESS_COOKIE]
    payload = decode_token(access_token, expected_type="access")
    assert "active_tenant" not in payload


# ---- UserOut.switchable_tenants ------------------------------------------------------ #


async def test_switchable_tenants_superadmin_sees_all_active(session: AsyncSession) -> None:
    a = await _make_tenant(session, name="SwtF", slug="swt-f")
    b = await _make_tenant(session, name="SwtG", slug="swt-g")
    inactive = await _make_tenant(session, name="SwtHInactive", slug="swt-h-inactive", active=False)
    superadmin_id = await _make_superadmin(session, username="swt-list-superadmin@local")
    user = await user_repo.get(session, superadmin_id)
    assert user is not None

    out = await me(user, session, None)  # type: ignore[arg-type]

    ids = {t.id for t in out.switchable_tenants}
    assert a in ids
    assert b in ids
    assert inactive not in ids, "Ein inaktiver Tenant darf nicht umschaltbar sein"


async def test_switchable_tenants_local_admin_sees_only_granted(session: AsyncSession) -> None:
    """Nicht-vakuoser Beweis der Access-Modell-Verhaltensänderung: ein lokaler
    (Nicht-Super-)Admin sieht NICHT mehr alle aktiven Tenants -- nur seine
    `admin_tenant`-Grants."""
    a = await _make_tenant(session, name="SwtF2", slug="swt-f2")
    b_foreign = await _make_tenant(session, name="SwtG2Foreign", slug="swt-g2-foreign")
    admin_id = await _make_local_admin(
        session, username="swt-list-admin@local", admin_tenant_ids=[a]
    )
    user = await user_repo.get(session, admin_id)
    assert user is not None

    out = await me(user, session, None)  # type: ignore[arg-type]

    ids = {t.id for t in out.switchable_tenants}
    assert ids == {a}
    assert b_foreign not in ids, "Regression: lokaler Admin sah fremden, ungewährten Tenant"


async def test_switchable_tenants_auditor_sees_only_assigned(session: AsyncSession) -> None:
    a = await _make_tenant(session, name="SwtI", slug="swt-i")
    _b_foreign = await _make_tenant(session, name="SwtJForeign", slug="swt-j-foreign")
    auditor_id = await _make_local_auditor(session, username="swt-list-auditor@local", tenant_id=a)
    user = await user_repo.get(session, auditor_id)
    assert user is not None

    out = await me(user, session, None)  # type: ignore[arg-type]

    assert [t.id for t in out.switchable_tenants] == [a]


async def test_switchable_tenants_sso_sees_only_its_own_tenant(session: AsyncSession) -> None:
    c = await _make_tenant(session, name="SwtK", slug="swt-k")
    _other = await _make_tenant(session, name="SwtLForeign", slug="swt-l-foreign")
    sso_id = await _make_sso_user(session, username="swt-sso@c", tenant_id=c)
    user = await user_repo.get(session, sso_id)
    assert user is not None

    out = await me(user, session, c)  # type: ignore[arg-type]

    assert [t.id for t in out.switchable_tenants] == [c]
    assert out.active_tenant is not None
    assert out.active_tenant.id == c


# ---- Randfall: Refresh-Token fehlt/kennt keine Sitzung mehr ------------------------- #


async def test_switch_tenant_without_refresh_cookie_raises_auth_error(
    session: AsyncSession,
) -> None:
    a = await _make_tenant(session, name="SwtM", slug="swt-m")
    admin_id = await _make_local_admin(
        session, username="swt-noref-admin@local", admin_tenant_ids=[a]
    )
    user = await user_repo.get(session, admin_id)
    assert user is not None

    request = _FakeRequest({})
    response = _FakeResponse()
    with pytest.raises(AuthError):
        await switch_tenant(
            request,  # type: ignore[arg-type]
            response,  # type: ignore[arg-type]
            SwitchTenantRequest(tenant_id=a),
            user,
            session,
        )


# ---- Fix 2 (Task 5 whole-branch review): switch-tenant respektiert den Idle-Timeout - #


async def test_switch_tenant_ends_idle_session(session: AsyncSession) -> None:
    """`switch-tenant` validierte die Sitzungszeile bisher, ohne wie `refresh` auch
    `_end_if_idle` aufzurufen -- ein Client hätte die Sitzung damit unbegrenzt am Leben
    halten können, indem er statt `refresh` einfach `switch-tenant` aufruft (der
    Idle-Timeout griffe nie). Mirror von `test_refresh_preserves_active_tenant`s Setup,
    nur mit einem künstlich veralteten `last_used_at` -- exakt die Bedingung, die
    `_end_if_idle` in `refresh` bereits abfängt."""
    a = await _make_tenant(session, name="SwtIdleA", slug="swt-idle-a")
    b = await _make_tenant(session, name="SwtIdleB", slug="swt-idle-b")
    admin_id = await _make_local_admin(
        session, username="swt-idle-admin@local", admin_tenant_ids=[a, b]
    )
    user = await user_repo.get(session, admin_id)
    assert user is not None
    us, refresh_token = await _seed_session(session, user_id=admin_id, active_tenant_id=a)

    idle_min = get_settings().idle_timeout_min
    us.last_used_at = utcnow() - dt.timedelta(minutes=idle_min + 1)
    await session.commit()

    request = _FakeRequest({REFRESH_COOKIE: refresh_token})
    response = _FakeResponse()
    with pytest.raises(AuthError) as exc_info:
        await switch_tenant(
            request,  # type: ignore[arg-type]
            response,  # type: ignore[arg-type]
            SwitchTenantRequest(tenant_id=b),
            user,
            session,
        )
    assert exc_info.value.code == "session_idle_timeout"

    # Beendet heisst gelöscht (wie beim Refresh-Pfad), nicht bloss revoked -- und keine
    # neuen Auth-Cookies dürfen gesetzt worden sein.
    row = (
        await session.execute(select(UserSession).where(UserSession.id == us.id))
    ).scalar_one_or_none()
    assert row is None, "Idle-Sitzung hätte gelöscht werden müssen"
    assert ACCESS_COOKIE in response.deleted_cookies
    assert REFRESH_COOKIE in response.deleted_cookies
    assert not response.cookie_values, "Bei Idle-Timeout dürfen keine neuen Cookies gesetzt werden"
