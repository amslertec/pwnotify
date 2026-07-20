"""TDD für Task 5 (Access-Modell/Superadmin-Phase): den instanzweiten Multi-Tenant-Mode-
Schalter (`/admin/instance`), die Default-Tenant-Umbenennung, `UserOut.multi_tenant_mode`
+ das Default-zuerst-Sortieren von `switchable_tenants`, und den Guard, der die generische
Pro-Tenant-Settings-Route (`PUT /settings`) davon abhält, `instance.*` zu schreiben.

Cross-Connection-Hinweis (wie `test_tenant_write_default.py`): `instance_settings.read_mode`/
`write_mode` öffnen für den Zugriff auf `instance.multi_tenant_mode` bewusst eine EIGENE
`tenant_scoped_session` (App-Rolle + RLS-GUC auf dem Default-Tenant) -- das ist eine ECHTE,
von der savepoint-isolierten `session`-Fixture UNABHÄNGIGE Verbindung auf dieselbe (echte)
Test-DB. Ein `write_mode(..., True)` COMMITTET daher wirklich und überlebt das Rollback der
Fixture -- jeder Test, der den Schalter schreibt (direkt oder über `update_instance`),
räumt ihn deshalb in einem `finally` wieder auf `False` zurück. Die Default-Tenant-
Umbenennung dagegen läuft über `tenant_repo.update(session, ...)` auf der GEWÖHNLICHEN
Fixture-Session (kein eigener `tenant_scoped_session`-Aufruf in der Route) -- die rollt mit
der Fixture ganz normal zurück, kein manuelles Aufräumen nötig (wie in `test_admin_tenants.py`).
"""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import ACCESS_COOKIE, require_superadmin
from app.api.routes.admin_instance import get_instance, update_instance
from app.api.routes.auth import me
from app.api.routes.settings import update as settings_update
from app.core.errors import ForbiddenError
from app.core.security import issue_token_pair
from app.models.setting import Setting
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.instance import InstanceUpdate
from app.schemas.settings import SettingsUpdate
from app.services import instance_settings
from app.services.settings_service import SettingsService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeRequest:
    """Duck-typed Request -- the GET route only reads `.cookies` (via
    `_resolve_authorized_tenant`), same shape as `test_matrix_b_route_gating.py`."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _request_with_claim(user_id: int, tenant_id: int | None) -> _FakeRequest:
    pair = issue_token_pair(str(user_id), active_tenant=tenant_id)
    return _FakeRequest({ACCESS_COOKIE: pair.access_token})


def _slug() -> str:
    return f"ti-{uuid.uuid4().hex[:10]}"


async def _mk_superadmin(session: AsyncSession) -> AppUser:
    user = AppUser(
        username=f"ti-superadmin-{uuid.uuid4().hex[:8]}", password_hash="x", role="superadmin"
    )
    session.add(user)
    await session.flush()
    return user


async def _mk_admin(session: AsyncSession) -> AppUser:
    """Lokaler (NICHT-Super-)Admin -- besteht `require_superadmin` NICHT (Design §6)."""
    user = AppUser(username=f"ti-admin-{uuid.uuid4().hex[:8]}", password_hash="x", role="admin")
    session.add(user)
    await session.flush()
    return user


async def _mk_auditor(session: AsyncSession) -> AppUser:
    user = AppUser(username=f"ti-auditor-{uuid.uuid4().hex[:8]}", password_hash="x", role="auditor")
    session.add(user)
    await session.flush()
    return user


async def _mk_tenant(session: AsyncSession, *, name: str | None = None) -> Tenant:
    slug = _slug()
    return await tenant_repo.create(session, name=name or slug, slug=slug)


# ---- PUT /admin/instance: Superadmin-only, toggelt + benennt um -------------------------- #


async def test_superadmin_toggles_mode_and_renames_default_tenant(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    try:
        out = await update_instance(
            None,  # type: ignore[arg-type]
            superadmin,
            InstanceUpdate(multi_tenant_mode=True, default_tenant_name="Neue Firma"),
            session,
        )
        assert out.multi_tenant_mode is True
        assert out.default_tenant_name == "Neue Firma"

        # Zurückgelesen über GET -- als Default-Kontext-Superadmin (kein active_tenant-Claim
        # -> resolve_initial_tenant liefert den Default-Tenant) kommt der echte Name zurück.
        got = await get_instance(_FakeRequest(), superadmin, session)  # type: ignore[arg-type]
        assert got.multi_tenant_mode is True
        assert got.default_tenant_name == "Neue Firma"

        # Slug bleibt immer 'default' (kein Slug-Feld in InstanceUpdate).
        default = await tenant_repo.default_tenant(session)
        assert default.slug == "default"
        assert default.name == "Neue Firma"
    finally:
        await instance_settings.write_mode(session, False)


async def test_local_admin_put_instance_is_forbidden(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin(admin)
        await update_instance(
            None,  # type: ignore[arg-type]
            guarded,
            InstanceUpdate(multi_tenant_mode=True),
            session,
        )
    assert exc_info.value.code == "superadmin_required"
    # Kein Teilschreiben -- der Schalter bleibt beim Default (aus).
    assert (await instance_settings.read_mode(session)) is False


# ---- GET /admin/instance: Mode für jedes Konto, default_tenant_name nur Provider ---------- #


async def test_get_instance_reflects_mode_for_any_account_but_hides_provider_name(
    session: AsyncSession,
) -> None:
    """`multi_tenant_mode` (UI-Gating) sieht jedes Konto; `default_tenant_name` ist Provider-
    Metadatum und wird einem Kunden-Konto (hier: Auditor) NICHT offengelegt (I5)."""
    auditor = await _mk_auditor(session)
    try:
        await instance_settings.write_mode(session, True)
        out = await get_instance(_FakeRequest(), auditor, session)  # type: ignore[arg-type]
        assert out.multi_tenant_mode is True
        assert out.default_tenant_name is None
    finally:
        await instance_settings.write_mode(session, False)


async def test_get_instance_exposes_name_only_to_default_context_superadmin(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    default = await tenant_repo.default_tenant(session)
    assert default.id is not None
    customer = await _mk_tenant(session, name="Kunde AG")
    assert customer.id is not None

    # Default-Kontext (Claim == Default-Tenant): echter Name.
    got_default = await get_instance(
        _request_with_claim(superadmin.id, default.id), superadmin, session
    )  # type: ignore[arg-type]
    assert got_default.default_tenant_name == default.name

    # In einen Kunden-Kontext umgeschaltet: Provider-Metadatum verschwindet (Matrix B).
    got_customer = await get_instance(
        _request_with_claim(superadmin.id, customer.id), superadmin, session
    )  # type: ignore[arg-type]
    assert got_customer.default_tenant_name is None


# ---- Isolation: der Schalter lebt NUR auf dem Default-Tenant ------------------------------- #


async def test_mode_write_affects_default_tenant_setting_row_only(
    session: AsyncSession,
) -> None:
    tenant_b = await _mk_tenant(session)
    assert tenant_b.id is not None
    superadmin = await _mk_superadmin(session)
    try:
        await update_instance(
            None,  # type: ignore[arg-type]
            superadmin,
            InstanceUpdate(multi_tenant_mode=True),
            session,
        )

        default = await tenant_repo.default_tenant(session)
        default_row = (
            await session.execute(
                select(Setting).where(
                    Setting.tenant_id == default.id,
                    Setting.key == instance_settings.MULTI_TENANT_MODE_KEY,
                )
            )
        ).scalar_one()
        assert default_row.value is True

        other_row = (
            await session.execute(
                select(Setting).where(
                    Setting.tenant_id == tenant_b.id,
                    Setting.key == instance_settings.MULTI_TENANT_MODE_KEY,
                )
            )
        ).scalar_one_or_none()
        assert other_row is None
    finally:
        await instance_settings.write_mode(session, False)


# ---- /auth/me spiegelt den Schalter ---------------------------------------------------------- #


async def test_auth_me_reflects_multi_tenant_mode(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    try:
        out = await me(admin, session, None)  # type: ignore[arg-type]
        assert out.multi_tenant_mode is False

        await instance_settings.write_mode(session, True)
        out2 = await me(admin, session, None)  # type: ignore[arg-type]
        assert out2.multi_tenant_mode is True
    finally:
        await instance_settings.write_mode(session, False)


# ---- switchable_tenants: Default-Tenant zuerst ------------------------------------------------ #


async def test_switchable_tenants_returns_default_tenant_first(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    await _mk_tenant(session, name="AAA Corp")
    await _mk_tenant(session, name="Zzz Inc")

    out = await me(superadmin, session, None)  # type: ignore[arg-type]
    default = await tenant_repo.default_tenant(session)

    assert len(out.switchable_tenants) >= 3
    assert out.switchable_tenants[0].id == default.id

    rest_names = [t.name for t in out.switchable_tenants[1:]]
    assert rest_names == sorted(rest_names)


# ---- Guard: instance.* NICHT über die generische Pro-Tenant-Settings-Route schreibbar --------- #


async def test_generic_settings_route_cannot_write_instance_key(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    svc = SettingsService(session)
    body = SettingsUpdate(values={"instance.multi_tenant_mode": True, "app.update_check": False})

    with pytest.raises(ForbiddenError) as exc_info:
        await settings_update(None, admin, body, svc, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "instance_setting_forbidden"

    # Die Anfrage wird als Ganzes abgelehnt, BEVOR `svc.set_many` überhaupt läuft -- weder
    # der instanzweite Schalter noch der (harmlose) Begleit-Key wurden geschrieben.
    assert (await instance_settings.read_mode(session)) is False
