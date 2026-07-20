"""Red-team remediation: three small authz/error findings, one commit.

M1 -- `POST /admin/users/sso/sync` (`admin_users.sync_sso`) is a WRITE path (it creates,
     re-activates, overwrites roles on and DELETES SSO accounts), yet it authorized its
     tenant with the READ gate (`_resolve_authorized_tenant(...)` without `write=True`). A
     `role=='admin'` account holding only an `auditor_tenant` grant on the tenant could
     therefore trigger a destructive reconcile from a read-only grant. Sister route
     `runs.trigger` already passes `write=True`.

M10 -- `users.notify_now` re-raised the raw `outcome.error` (SMTP/Graph transport text:
      server banners, internal hostnames, tenant GUIDs) straight to the client. The response
      must carry a generic German message; the detail belongs in the log only.

L6 -- `admin_users.list_users` computed the superadmin short-circuit with a raw
     `user.role == "superadmin"` compare instead of the shared `is_superadmin()` predicate
     (`not is_sso and role == "superadmin"`). An SSO account that somehow held
     `role=='superadmin'` would skip `tenant_repo.is_allowed` while `tid` comes from the
     UNAUTHORIZED active-tenant claim -- one line from a cross-tenant read.

Route functions are driven directly, mirroring `test_write_scoped_tenant_auth.py` (M1),
`test_audit_coverage.py` (M10) and `test_admin_users_scoping.py` (L6).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, get_current_user
from app.api.routes import users
from app.api.routes.admin_users import list_users, sync_sso
from app.core.errors import ForbiddenError, NotFoundError
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.db.tenant_context import tenant_scoped_session
from app.models.entra import EntraUser
from app.repositories import tenant_repo, user_repo
from app.services.notifier import NotifyOutcome
from app.services.settings_service import SettingsService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


class _FakeRequest:
    """Duck-typed Request -- the routes under test only read `.cookies`/`.headers`/`.client`."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _uname(label: str) -> str:
    return f"authz-{label}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
# M1 -- sync_sso must use the WRITE gate (auditor-only grant -> 403)
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def stale_auditor_admin(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int]]:
    """A committed local `role=='admin'` account holding ONLY an `auditor_tenant` grant on a
    fresh tenant -- the read-only-grant state M1 is about. `migrated_engine` (own connection,
    real commit) because `sync_sso` resolves the caller/tenant on its own `get_session_factory`
    session and would not see the savepoint-isolated `session` fixture's uncommitted rows."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Authz Stale', :slug, true, now()) RETURNING id"
                    ),
                    {"slug": _uname("t")},
                )
            ).scalar_one()
        )
        uid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO app_user "
                        "(username, password_hash, role, is_active, is_sso, "
                        "failed_login_count, language, created_at, updated_at) VALUES "
                        "(:username, 'x', 'admin', true, false, 0, 'de', now(), now()) "
                        "RETURNING id"
                    ),
                    {"username": _uname("admin")},
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO auditor_tenant (user_id, tenant_id, source) VALUES "
                "(:uid, :tid, 'manual')"
            ),
            {"uid": uid, "tid": tid},
        )
        await conn.commit()
    try:
        yield uid, tid
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM auditor_tenant WHERE user_id = :u"), {"u": uid})
            await conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": uid})
            await conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tid})
            await conn.commit()


async def test_sync_sso_rejects_auditor_only_grant(
    stale_auditor_admin: tuple[int, int],
) -> None:
    """RED before the fix: the read gate accepts the auditor grant, so `sync_sso` proceeds
    past authorization; GREEN after `write=True` rejects it with `tenant_forbidden`."""
    uid, tid = stale_auditor_admin
    request = _FakeRequest(
        {ACCESS_COOKIE: issue_token_pair(str(uid), active_tenant=tid).access_token}
    )
    async with get_session_factory()() as session:
        user = await get_current_user(request, session)
        with pytest.raises(ForbiddenError) as exc_info:
            await sync_sso(request, user, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "tenant_forbidden"


# --------------------------------------------------------------------------- #
# M10 -- notify_now must not leak the raw transport error to the client
# --------------------------------------------------------------------------- #

_SECRET_ERROR = "smtp.internal.corp:587 said 550 5.7.1 tenant-guid=abcd-1234 relay denied"


class _CapturingLogger:
    """Records `warning` calls so the test can prove the detail is logged, not returned."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))


@pytest_asyncio.fixture
async def tenant_admin_entra_user(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[dict[str, Any]]:
    """A committed tenant + write-granted local admin + one EntraUser homed on it. Mirrors the
    `test_audit_coverage.py` fixture: the tenant-scoped (runtime) role has no `app_user`
    access, so `admin` is yielded as an object (resolved on the owner session, as FastAPI
    does) rather than re-fetched inside the scoped session."""
    factory = get_session_factory()
    async with factory() as session:
        tenant = await tenant_repo.create(session, name="Authz M10 Tenant", slug=_uname("t"))
        assert tenant.id is not None
        tid = tenant.id
        admin = await user_repo.create(
            session, username=_uname("admin"), password_hash="x", role="admin", is_sso=False
        )
        assert admin.id is not None
        await tenant_repo.add_grant(session, user_id=admin.id, tenant_id=tid, kind="admin")
        entra_user = EntraUser(
            tenant_id=tid, entra_id=f"e-{uuid.uuid4().hex[:8]}", upn=_uname("target")
        )
        session.add(entra_user)
        await session.commit()
        await session.refresh(entra_user)
        assert entra_user.id is not None
    try:
        yield {"tenant_id": tid, "admin": admin, "entra_user_id": entra_user.id}
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM entra_user WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM setting WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM admin_tenant WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": admin.id})
            await conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tid})
            await conn.commit()


async def test_notify_now_hides_raw_transport_error(
    tenant_admin_entra_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before the fix: the raw `outcome.error` is re-raised verbatim to the client.
    GREEN after: generic user-facing message, raw detail only in the log."""
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]
    uid = tenant_admin_entra_user["entra_user_id"]

    async def _fake_notify(*args: Any, **kwargs: Any) -> NotifyOutcome:
        return NotifyOutcome(action="failed", stage=7, error=_SECRET_ERROR)

    class _FakeSender:
        backend = "fake"

        async def send(self, **kwargs: Any) -> None:
            return None

    capturing = _CapturingLogger()
    monkeypatch.setattr(users, "notify_user", _fake_notify)
    monkeypatch.setattr(users, "build_sender", lambda _settings: _FakeSender())
    monkeypatch.setattr(users, "log", capturing)

    async with tenant_scoped_session(tid) as session:
        svc = SettingsService(session)
        with pytest.raises(NotFoundError) as exc_info:
            await users.notify_now(None, admin, uid, session, svc)  # type: ignore[arg-type]

    assert exc_info.value.code == "send_failed"
    assert _SECRET_ERROR not in exc_info.value.message, "raw transport error leaked to the client"
    # The detail must survive somewhere the operator can reach it -- the log.
    assert any(_SECRET_ERROR in str(kw.get("error")) for _, kw in capturing.warnings), (
        "raw transport error was neither returned (good) nor logged (bad)"
    )


# --------------------------------------------------------------------------- #
# L6 -- list_users must use is_superadmin(), not a raw role compare
# --------------------------------------------------------------------------- #


async def test_list_users_sso_superadmin_is_not_short_circuited(session: AsyncSession) -> None:
    """An SSO account carrying `role=='superadmin'` (set artificially -- not reachable today)
    must NOT be treated as a superadmin: `is_allowed` must still gate the active tenant.

    RED before the fix: the raw `role == 'superadmin'` compare is True, `is_allowed` is
    skipped, and the tenant's homed accounts are returned. GREEN after `is_superadmin(user)`:
    the SSO flag disqualifies the short-circuit, `is_allowed` runs, and with no grant the
    result is default-deny (empty lists)."""
    tenant = await tenant_repo.create(session, name="Authz L6 Tenant", slug=_uname("t"))
    assert tenant.id is not None
    tid = tenant.id
    # A local admin HOMED in the tenant -- a non-empty payload iff the short-circuit fires.
    await user_repo.create(
        session,
        username=_uname("homed"),
        password_hash="x",
        role="admin",
        is_sso=False,
        tenant_id=tid,
    )
    # The caller: an SSO "superadmin" with NO grant on the tenant.
    caller = await user_repo.create(
        session, username=_uname("ssosuper"), password_hash="x", role="superadmin", is_sso=True
    )

    out = await list_users(caller, session, tid)  # type: ignore[arg-type]

    assert out["local"] == [], "SSO role=='superadmin' wrongly short-circuited is_allowed (L6)"
    assert out["sso"] == []
