"""Security Phase 5, Task 8 (M10): audit coverage for the remaining security-relevant routes.

Before this fix, several state-changing routes never wrote an `audit_log` entry at all:
`users.py` (`set_exclude`/`notify_now`/`bulk`), `runs.py` (`trigger`), `notifications.py`
(`retry`), `branding.py` (upload/delete), `settings.py` (`add_exclusion`/`delete_exclusion`),
`setup.py` (`create_admin`, the very first superadmin), `admin_users.py` (`sync_sso`), and
`auth.py` (`two_factor_setup`, which hands out the TOTP secret/QR). Each test below drives
the handler directly and then proves the expected `action` row exists -- RED before the fix
(no row at all), GREEN after.

Tenant-scoped write routes (`TenantWriteSessionDep`) need a REAL committed tenant/admin/
target row and a REAL `tenant_scoped_session` (runtime role + GUC) -- the savepoint-only
`session` fixture runs on the owner engine and never sees the runtime engine's own
connection pool, and the affected tables' `tenant_id` columns are NOT NULL with no active
context to stamp on a bare owner session (see `test_branding_tenant_scope.py`'s own
regression test for that exact failure mode). Owner-session-only routes (`setup.create_admin`,
`two_factor_setup`) use the plain savepoint `session` fixture instead, mirroring
`test_setup_superadmin.py`/`test_2fa_reenroll_guard.py`. `runs.trigger` and
`admin_users.sync_sso` resolve their own tenant via `_resolve_authorized_tenant`, so they need
a signed access-token cookie on a duck-typed request, mirroring `test_runs_trigger_scope.py`/
`test_sso_sync_caller_scope.py`.
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE
from app.api.routes import auth as auth_routes
from app.api.routes import branding, notifications, runs, settings, setup, users
from app.api.routes.admin_users import sync_sso
from app.api.routes.auth import two_factor_setup
from app.core.config import get_settings
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.db.tenant_context import open_active_session, tenant_scoped_session
from app.models.entra import EntraUser
from app.models.notification import NotificationLog
from app.repositories import tenant_repo, user_repo
from app.schemas.auth import LanguageUpdate, ProfileUpdate
from app.schemas.settings import ExclusionCreate
from app.services.notifier import NotifyOutcome
from app.services.scheduler import SchedulerService, set_scheduler
from app.services.settings_service import SettingsService
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import Headers


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Iterator[None]:
    """`setup.create_admin` and `auth.two_factor_setup` are `@limiter.limit`-decorated;
    slowapi rejects a duck-typed request unless the limiter is disabled -- same pattern as
    `test_setup_superadmin.py`/`test_2fa_reenroll_guard.py`."""
    from app.api.deps import limiter

    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeRequest:
    """Duck-typed Request -- only `.cookies`/`.headers`/`.client` are read by the routes
    under test."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value


def _slug() -> str:
    return f"t8cov-{uuid.uuid4().hex[:10]}"


def _uname(label: str) -> str:
    return f"t8cov-{label}-{uuid.uuid4().hex[:8]}"


async def _latest_action_row(
    session: AsyncSession, *, action: str, target: str | None = None
) -> Any:
    """Read back the most recent `audit_log` row for `action` (+ optional `target`) on the
    SAME session the route just wrote to -- autoflush makes the pending INSERT visible to
    this SELECT even before any explicit commit."""
    stmt = "SELECT tenant_id, target, detail FROM audit_log WHERE action = :a"
    params: dict[str, Any] = {"a": action}
    if target is not None:
        stmt += " AND target = :t"
        params["t"] = target
    stmt += " ORDER BY id DESC LIMIT 1"
    return (await session.execute(text(stmt), params)).one_or_none()


@pytest_asyncio.fixture
async def tenant_admin_entra_user(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[dict[str, Any]]:
    """A real, committed tenant + a local admin (write-granted) + one EntraUser row homed on
    it. Used by the `users.py`/`notifications.py`/`branding.py`/`settings.py` groups below.

    Yields the `admin` `AppUser` object itself (not just its id): the runtime role behind
    `tenant_scoped_session` (RLS + GUC) has deliberately NO grants on `app_user` at all --
    it is an instance-wide table with no RLS, kept unreachable from that role even if RLS
    were ever misconfigured (defense in depth). A `user_repo.get(session, admin_id)` call
    INSIDE a `tenant_scoped_session` therefore fails with `InsufficientPrivilegeError` --
    exactly like the real routes never do that (`AdminUser`/`CurrentUser` are resolved on
    the OWNER session by FastAPI before the handler body runs). `expire_on_commit=False`
    (`get_session_factory`) keeps `admin`'s already-loaded attributes readable after this
    fixture's own session closes."""
    factory = get_session_factory()
    async with factory() as session:
        tenant = await tenant_repo.create(session, name="T8Cov Tenant", slug=_slug())
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
            await conn.execute(
                text("DELETE FROM notification_log WHERE tenant_id = :t"), {"t": tid}
            )
            await conn.execute(text("DELETE FROM exclusion WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM entra_user WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM setting WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM admin_tenant WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": admin.id})
            await conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tid})
            await conn.commit()


async def test_set_exclude_records_user_excluded(
    tenant_admin_entra_user: dict[str, Any],
) -> None:
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]
    uid = tenant_admin_entra_user["entra_user_id"]

    async with tenant_scoped_session(tid) as session:
        await users.set_exclude(
            None,
            admin,
            uid,
            users.ExcludeRequest(excluded=True),
            session,  # type: ignore[arg-type]
        )
        row = await _latest_action_row(session, action="entra_user.exclusion_changed")
        assert row is not None, "set_exclude did not write an audit_log row (RED without the fix)"
        assert row.tenant_id == tid
        assert row.detail.get("excluded") is True


async def test_bulk_exclude_and_notify_record_distinct_actions(
    tenant_admin_entra_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]
    uid = tenant_admin_entra_user["entra_user_id"]

    async def _fake_notify(*args: Any, **kwargs: Any) -> NotifyOutcome:
        return NotifyOutcome(
            action="sent", stage=7, recipient="target@example.com", channel="primary"
        )

    class _FakeSender:
        backend = "fake"

        async def send(self, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(users, "notify_user", _fake_notify)
    monkeypatch.setattr(users, "build_sender", lambda _settings: _FakeSender())

    async with tenant_scoped_session(tid) as session:
        svc = SettingsService(session)

        await users.bulk(
            None,
            admin,
            users.BulkRequest(ids=[uid], action="exclude"),
            session,
            svc,  # type: ignore[arg-type]
        )
        exclude_row = await _latest_action_row(session, action="entra_user.exclusion_changed")
        assert exclude_row is not None, "bulk exclude did not write an audit_log row (RED)"
        assert exclude_row.detail.get("kind") == "bulk"

        await users.bulk(
            None,
            admin,
            users.BulkRequest(ids=[uid], action="notify"),
            session,
            svc,  # type: ignore[arg-type]
        )
        notify_row = await _latest_action_row(session, action="notification.manual_send")
        assert notify_row is not None, "bulk notify did not write an audit_log row (RED)"
        assert notify_row.detail.get("count") == 1


async def test_notify_now_records_notification_sent_manual(
    tenant_admin_entra_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]
    uid = tenant_admin_entra_user["entra_user_id"]

    async def _fake_notify(*args: Any, **kwargs: Any) -> NotifyOutcome:
        return NotifyOutcome(
            action="sent", stage=7, recipient="target@example.com", channel="primary"
        )

    class _FakeSender:
        backend = "fake"

        async def send(self, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(users, "notify_user", _fake_notify)
    monkeypatch.setattr(users, "build_sender", lambda _settings: _FakeSender())

    async with tenant_scoped_session(tid) as session:
        svc = SettingsService(session)
        await users.notify_now(None, admin, uid, session, svc)  # type: ignore[arg-type]
        row = await _latest_action_row(session, action="notification.manual_send")
        assert row is not None, "notify_now did not write an audit_log row (RED without the fix)"
        assert row.tenant_id == tid


async def test_notification_retry_records_action(
    tenant_admin_entra_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]
    uid = tenant_admin_entra_user["entra_user_id"]

    class _FakeSender:
        backend = "fake"

        async def send(self, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(notifications, "build_sender", lambda _settings: _FakeSender())

    async with tenant_scoped_session(tid) as session:
        svc = SettingsService(session)
        log_entry = NotificationLog(
            tenant_id=tid,
            entra_user_id=uid,
            reminder_day=7,
            expiry_cycle="2099-01-01",
            channel="primary",
            backend="fake",
            recipient="target@example.com",
            language="en",
            status="failed",
            error="boom",
        )
        session.add(log_entry)
        await session.commit()
        await session.refresh(log_entry)
        assert log_entry.id is not None

        await notifications.retry(None, admin, log_entry.id, session, svc)  # type: ignore[arg-type]
        row = await _latest_action_row(session, action="notification.retried")
        assert row is not None, "retry did not write an audit_log row (RED without the fix)"
        assert row.tenant_id == tid


async def test_branding_upload_logo_records_branding_changed(
    tenant_admin_entra_user: dict[str, Any], tmp_path: Any
) -> None:
    prev = os.environ.get("PWNOTIFY_DATA_DIR")
    os.environ["PWNOTIFY_DATA_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    try:
        tid = tenant_admin_entra_user["tenant_id"]
        admin = tenant_admin_entra_user["admin"]
        logo = b"""<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <circle cx="5" cy="5" r="4" fill="#123456"/>
</svg>"""
        upload = UploadFile(
            file=io.BytesIO(logo),
            filename="logo.svg",
            headers=Headers({"content-type": "image/svg+xml"}),
        )
        async with tenant_scoped_session(tid) as session:
            svc = SettingsService(session)
            await branding.upload_logo(None, admin, svc, session, upload)  # type: ignore[arg-type]
            row = await _latest_action_row(session, action="branding.changed")
            assert row is not None, "upload_logo did not write an audit_log row (RED)"
            assert row.tenant_id == tid
            assert row.detail.get("asset") == "logo"
            assert row.detail.get("op") == "upload"
    finally:
        if prev is None:
            os.environ.pop("PWNOTIFY_DATA_DIR", None)
        else:
            os.environ["PWNOTIFY_DATA_DIR"] = prev
        get_settings.cache_clear()


async def test_settings_add_and_delete_exclusion_record_user_excluded(
    tenant_admin_entra_user: dict[str, Any],
) -> None:
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]

    async with tenant_scoped_session(tid) as session:
        exc = await settings.add_exclusion(
            None,
            admin,
            ExclusionCreate(kind="user", value="excluded@example.com"),
            session,  # type: ignore[arg-type]
        )
        add_row = await _latest_action_row(
            session, action="entra_user.exclusion_changed", target="excluded@example.com"
        )
        assert add_row is not None, "add_exclusion did not write an audit_log row (RED)"
        assert add_row.detail.get("excluded") is True

        await settings.delete_exclusion(None, admin, exc.id, session)  # type: ignore[arg-type]
        del_row = await _latest_action_row(
            session, action="entra_user.exclusion_changed", target="excluded@example.com"
        )
        assert del_row is not None
        assert del_row.detail.get("excluded") is False, (
            "delete_exclusion did not write its own audit_log row (RED without the fix)"
        )


async def test_setup_create_admin_records_superadmin_created(session: AsyncSession) -> None:
    body = setup.AdminCreate(username=_uname("firstsetup"), password="Str0ng!Passw0rd1")
    response = _FakeResponse()
    request = _FakeRequest()

    out = await setup.create_admin(body, response, request, session)  # type: ignore[arg-type]

    row = await _latest_action_row(session, action="user.superadmin_created", target=out.username)
    assert row is not None, "create_admin did not write an audit_log row (RED without the fix)"
    assert row.detail.get("first_setup") is True


async def test_two_factor_setup_records_action(session: AsyncSession) -> None:
    user = await user_repo.create(
        session, username=_uname("2fa"), password_hash="x", role="admin", is_sso=False
    )
    request = _FakeRequest()

    out = await two_factor_setup(request, user, session)  # type: ignore[arg-type]
    assert out.otpauth_uri

    row = await _latest_action_row(session, action="auth.2fa_setup_started")
    assert row is not None, "two_factor_setup did not write an audit_log row (RED without the fix)"
    assert row.detail == {}, "the TOTP secret/QR must never be logged into the audit detail"


@pytest_asyncio.fixture
async def customer_and_admin(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int]]:
    """A real, committed tenant + a write-granted local admin. Used by the `runs.trigger`
    and `admin_users.sync_sso` groups, which resolve their own tenant internally via
    `_resolve_authorized_tenant` and therefore need a signed access-token cookie, mirroring
    `test_runs_trigger_scope.py`/`test_sso_sync_caller_scope.py`."""
    async with migrated_engine.connect() as conn:
        cid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('T8CovCustomer','t8cov-customer',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()

    factory = get_session_factory()
    async with factory() as s:
        admin = await user_repo.create(
            s, username=_uname("runadmin"), password_hash="x", role="admin", is_sso=False
        )
        assert admin.id is not None
        await tenant_repo.add_grant(s, user_id=admin.id, tenant_id=cid, kind="admin")
        admin_id = admin.id
    try:
        yield cid, admin_id
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :c"), {"c": cid})
            await conn.execute(text("DELETE FROM run WHERE tenant_id = :c"), {"c": cid})
            await conn.execute(text("DELETE FROM setting WHERE tenant_id = :c"), {"c": cid})
            await conn.execute(text("DELETE FROM admin_tenant WHERE tenant_id = :c"), {"c": cid})
            await conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": admin_id})
            await conn.execute(text("DELETE FROM tenant WHERE id = :c"), {"c": cid})
            await conn.commit()


def _patch_heavy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sync_users(session: Any, settings: dict[str, Any]) -> dict[str, int]:
        return {"checked": 0}

    async def _fake_sso_sync(
        session: Any, settings: dict[str, Any], *, tenant_id: int
    ) -> dict[str, int]:
        return {"synced": 0, "removed": 0}

    async def _no_excluded(session: Any, settings: dict[str, Any]) -> set[str]:
        return set()

    async def _no_alert(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.services.runner.sync_users", _fake_sync_users)
    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sso_sync)
    monkeypatch.setattr("app.services.runner._resolve_excluded_ids", _no_excluded)
    monkeypatch.setattr("app.services.alerts.maybe_send_run_alert", _no_alert)


async def test_run_trigger_records_action_with_tenant(
    customer_and_admin: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, admin_id = customer_and_admin
    _patch_heavy(monkeypatch)
    set_scheduler(SchedulerService(open_active_session, base_url="http://test.local"))

    factory = get_session_factory()
    async with factory() as session:
        admin = await user_repo.get(session, admin_id)
        assert admin is not None
        request = _FakeRequest(
            {ACCESS_COOKIE: issue_token_pair(str(admin_id), active_tenant=cid).access_token}
        )
        run = await runs.trigger(request, runs.TriggerRequest(dry_run=True), admin, session)  # type: ignore[arg-type]

        row = await _latest_action_row(session, action="run.triggered")
        assert row is not None, "trigger did not write an audit_log row (RED without the fix)"
        assert row.tenant_id == cid, (
            "trigger's audit row was not stamped with the caller's own tenant "
            "(T7 tenant_id override not used)"
        )
        assert row.detail.get("scope") == "tenant"

    async with factory() as session:
        await session.execute(text("DELETE FROM run WHERE id = :r"), {"r": run.id})
        await session.commit()


async def test_sync_sso_records_action(
    customer_and_admin: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, admin_id = customer_and_admin

    async with get_session_factory()() as conn_session:
        await conn_session.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:t, 'oidc.enabled', to_jsonb(true), false, now()), "
                "(:t, 'oidc.admin_group_id', to_jsonb('grp'::text), false, now())"
            ),
            {"t": cid},
        )
        await conn_session.commit()

    async def _fake_sync(
        session: Any, settings: dict[str, Any], *, tenant_id: int
    ) -> dict[str, int]:
        return {"synced": 2, "removed": 1}

    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sync)

    factory = get_session_factory()
    async with factory() as session:
        admin = await user_repo.get(session, admin_id)
        assert admin is not None
        request = _FakeRequest(
            {ACCESS_COOKIE: issue_token_pair(str(admin_id), active_tenant=cid).access_token}
        )
        await sync_sso(request, admin, session)  # type: ignore[arg-type]

        row = await _latest_action_row(session, action="user.sso_synced")
        assert row is not None, "sync_sso did not write an audit_log row (RED without the fix)"
        assert row.tenant_id == cid, "sync_sso's single-tenant call must stamp that tenant"
        assert row.detail.get("synced") == 2
        assert row.detail.get("removed") == 1


# --- L3: audit coverage for the export + self-service routes ------------------------------- #


async def test_export_users_records_users_exported(
    tenant_admin_entra_user: dict[str, Any],
) -> None:
    """The mass-PII export (`GET /users/export`) must leave exactly one audit row with the
    row count/format and NO PII in the detail. RED without the fix (no row at all)."""
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]

    async with tenant_scoped_session(tid) as session:
        resp = await users.export_users(None, admin, session, fmt="csv")  # type: ignore[arg-type]
        assert resp is not None

        row = await _latest_action_row(session, action="entra_user.exported")
        assert row is not None, "export_users did not write an audit_log row (RED without the fix)"
        assert row.tenant_id == tid
        assert row.detail.get("format") == "csv"
        # The one committed EntraUser from the fixture must be counted.
        assert row.detail.get("count") == 1
        # Hard guarantee: no per-user PII (upn/mail/display_name) leaks into the audit detail.
        blob = str(row.detail).lower()
        for pii_marker in ("upn", "mail", "display", "@"):
            assert pii_marker not in blob, f"export audit detail leaked PII: {pii_marker}"


async def test_template_reset_records_action(
    tenant_admin_entra_user: dict[str, Any],
) -> None:
    tid = tenant_admin_entra_user["tenant_id"]
    admin = tenant_admin_entra_user["admin"]

    async with tenant_scoped_session(tid) as session:
        svc = SettingsService(session)
        await settings.template_reset(None, admin, svc, session)  # type: ignore[arg-type]
        row = await _latest_action_row(session, action="settings.template_reset")
        assert row is not None, "template_reset did not write an audit_log row (RED)"
        assert row.tenant_id == tid


async def test_update_profile_records_action(session: AsyncSession) -> None:
    user = await user_repo.create(
        session, username=_uname("prof"), password_hash="x", role="admin", is_sso=False
    )
    request = _FakeRequest()
    await auth_routes.update_profile(
        request,
        ProfileUpdate(display_name="New Name"),
        user,
        session,
        None,  # type: ignore[arg-type]
    )
    row = await _latest_action_row(session, action="auth.profile_updated")
    assert row is not None, "update_profile did not write an audit_log row (RED)"


async def test_set_language_records_action(session: AsyncSession) -> None:
    user = await user_repo.create(
        session, username=_uname("lang"), password_hash="x", role="admin", is_sso=False
    )
    request = _FakeRequest()
    await auth_routes.set_language(
        request,
        LanguageUpdate(language="en"),
        user,
        session,
        None,  # type: ignore[arg-type]
    )
    row = await _latest_action_row(session, action="auth.language_changed")
    assert row is not None, "set_language did not write an audit_log row (RED)"
    assert row.detail.get("language") == "en"


async def test_delete_avatar_records_action(session: AsyncSession) -> None:
    user = await user_repo.create(
        session, username=_uname("av"), password_hash="x", role="admin", is_sso=False
    )
    request = _FakeRequest()
    await auth_routes.delete_my_avatar(request, user, session, None)  # type: ignore[arg-type]
    row = await _latest_action_row(session, action="auth.avatar_changed")
    assert row is not None, "delete_my_avatar did not write an audit_log row (RED)"
    assert row.detail.get("op") == "delete"
