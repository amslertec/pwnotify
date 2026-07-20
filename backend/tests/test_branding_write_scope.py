"""TDD for I-01 (Security Audit v0.3.3): branding write-route write-gate must live on the
`svc` dependency itself, not only on the sibling `session: TenantWriteSessionDep` parameter.

Before this fix, `upload_logo`/`upload_favicon`/`delete_logo`/`delete_favicon` mutated
tenant settings via `svc: TenantSettingsDep` -- the READ-scoped settings service (its
`get_tenant_settings_service` dependency chains through `get_tenant_session`, the read
gate). The write gate only fired today because `session: TenantWriteSessionDep` is a
required sibling parameter that FastAPI resolves before the handler runs. A future
refactor that drops the seemingly-unused `session` parameter (it is only read inside
`_audit_branding_change`) would silently reopen write access to a read-only
(`auditor_tenant`-grant) account.

This suite proves, non-vacuously:
1. All four write routes' `svc` parameter is wired to `get_tenant_settings_service_write`
   (the WRITE-gated dependency), not `get_tenant_settings_service` -- a structural,
   introspection-level proof of the fix (fails against the pre-fix code, independent of
   whether the sibling `session` parameter still exists).
2. An account that passes the `AdminUser` role gate (`role == "admin"`) but whose only
   grant on the active tenant is `auditor_tenant` (read-only -- e.g. a stale grant, see
   `test_write_scoped_tenant_auth.py`) is rejected (`tenant_forbidden`) by the `svc`
   dependency ALONE, on all four routes.
3. A real write-authorized admin (`admin_tenant` grant) can still upload and delete a
   logo end-to-end through the fixed dependency chain (happy path unchanged).
"""

from __future__ import annotations

import contextlib
import io
import os
import uuid
from collections.abc import AsyncGenerator
from typing import get_args, get_type_hints

import pytest
import pytest_asyncio
from app.api.deps import (
    ACCESS_COOKIE,
    get_current_user,
    get_tenant_session_write,
    get_tenant_settings_service,
    get_tenant_settings_service_write,
)
from app.api.routes import branding
from app.core.config import get_settings
from app.core.errors import ForbiddenError
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers

WRITE_ROUTES = (
    branding.upload_logo,
    branding.upload_favicon,
    branding.delete_logo,
    branding.delete_favicon,
)

HARMLESS_SVG_LOGO = b"""<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <circle cx="5" cy="5" r="4" fill="#4F46E5"/>
</svg>"""


def _upload_file(data: bytes, content_type: str = "image/svg+xml") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename="logo.svg",
        headers=Headers({"content-type": content_type}),
    )


def _svc_dependency(route_func: object) -> object:
    """The underlying callable a route's `svc` parameter resolves through, e.g.
    `get_tenant_settings_service` vs. `get_tenant_settings_service_write`."""
    hints = get_type_hints(route_func, include_extras=True)
    args = get_args(hints["svc"])
    depends = args[1]
    return depends.dependency


# ---- 1. Structural proof: `svc` is wired to the WRITE-gated dependency -------------------- #


@pytest.mark.parametrize("route_func", WRITE_ROUTES, ids=lambda f: f.__name__)
def test_write_route_svc_is_write_scoped(route_func: object) -> None:
    assert _svc_dependency(route_func) is get_tenant_settings_service_write
    assert _svc_dependency(route_func) is not get_tenant_settings_service


# ---- 2. Behavioral proof: a read-only (auditor_tenant) grant is blocked by `svc` alone ---- #


class _FakeRequest:
    """Duck-typed Request -- the guards driven here only read `.cookies`."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


def _slug() -> str:
    return f"bws-{uuid.uuid4().hex[:10]}"


@contextlib.asynccontextmanager
async def _tenant_write_chain_for(uid: int, *, claim: int | None):
    """Drives `get_tenant_session_write` -> `get_tenant_settings_service_write` exactly like
    FastAPI builds branding.py's (post-fix) `svc: TenantWriteSettingsDep` parameter, and
    reuses the same scoped session for the sibling `session: TenantWriteSessionDep`
    parameter (mirrors FastAPI's per-request dependency caching for identical callables)."""
    pair = issue_token_pair(str(uid), active_tenant=claim)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_tenant_session_write(request, user, owner)
        try:
            scoped = await anext(gen)
            svc = await get_tenant_settings_service_write(scoped)
            yield user, svc, scoped
        finally:
            await gen.aclose()


@pytest_asyncio.fixture
async def stale_grant_admin(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int]]:
    """A committed local `role=="admin"` account holding ONLY an `auditor_tenant` (read-only)
    grant on a freshly created tenant -- same construction as
    `test_write_scoped_tenant_auth.py::stale_grant_admin`, duplicated here so this suite
    stays self-contained. `migrated_engine` (own connection, real commit) because
    `get_tenant_session_write` opens its own connection via `get_session_factory()` and
    would not see uncommitted rows from the savepoint-isolated `session` fixture."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "(:name, :slug, true, now()) RETURNING id"
                    ),
                    {"name": "Bws Stale", "slug": _slug()},
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
                    {"username": f"bws-stale-{uuid.uuid4().hex[:8]}"},
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
            await conn.execute(
                text("DELETE FROM auditor_tenant WHERE user_id = :uid"), {"uid": uid}
            )
            await conn.execute(text("DELETE FROM app_user WHERE id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tid})
            await conn.commit()


@pytest.mark.parametrize("route_func", WRITE_ROUTES, ids=lambda f: f.__name__)
async def test_stale_grant_admin_blocked_on_all_write_routes(
    stale_grant_admin: tuple[int, int],
    route_func: object,
) -> None:
    uid, tid = stale_grant_admin
    extra_args = (
        (_upload_file(HARMLESS_SVG_LOGO),) if route_func.__name__.startswith("upload") else ()  # type: ignore[attr-defined]
    )
    with pytest.raises(ForbiddenError) as exc_info:
        async with _tenant_write_chain_for(uid, claim=tid) as (user, svc, scoped):
            # The WRITE gate raises on context entry above (constructing `svc`/`scoped`
            # already resolves `get_tenant_session_write`) -- this call never actually runs,
            # kept only to document the route's real call shape.
            await route_func(None, user, svc, scoped, *extra_args)  # type: ignore[operator]
    assert exc_info.value.code == "tenant_forbidden"


# ---- 3. Happy path: a real write-authorized admin can still upload/delete a logo ---------- #


@pytest_asyncio.fixture
async def tmp_data_dir(tmp_path) -> AsyncGenerator[None]:
    """Branding uploads land under `{data_dir}/branding` -- redirect to a throwaway directory
    for the duration of the test (same pattern as `test_branding_tenant_scope.py`)."""
    prev = os.environ.get("PWNOTIFY_DATA_DIR")
    os.environ["PWNOTIFY_DATA_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    yield
    if prev is None:
        os.environ.pop("PWNOTIFY_DATA_DIR", None)
    else:
        os.environ["PWNOTIFY_DATA_DIR"] = prev
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def write_grant_admin(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int]]:
    """A committed local `role=="admin"` account holding a real `admin_tenant` (write) grant
    on a freshly created tenant -- the happy-path control for `stale_grant_admin` above."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "(:name, :slug, true, now()) RETURNING id"
                    ),
                    {"name": "Bws Write", "slug": _slug()},
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
                    {"username": f"bws-write-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO admin_tenant (user_id, tenant_id, source) VALUES "
                "(:uid, :tid, 'manual')"
            ),
            {"uid": uid, "tid": tid},
        )
        await conn.commit()
        try:
            yield uid, tid
        finally:
            await conn.execute(
                text(
                    "DELETE FROM audit_log WHERE action = 'branding.changed' AND tenant_id = :tid"
                ),
                {"tid": tid},
            )
            await conn.execute(text("DELETE FROM admin_tenant WHERE user_id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM app_user WHERE id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tid})
            await conn.commit()


async def test_write_grant_admin_can_upload_and_delete_logo(
    tmp_data_dir: None,
    write_grant_admin: tuple[int, int],
) -> None:
    uid, tid = write_grant_admin
    async with _tenant_write_chain_for(uid, claim=tid) as (user, svc, scoped):
        msg = await branding.upload_logo(None, user, svc, scoped, _upload_file(HARMLESS_SVG_LOGO))  # type: ignore[arg-type]
        assert msg.message
        assert await svc.get("branding.logo_path") is not None

        del_msg = await branding.delete_logo(None, user, svc, scoped)  # type: ignore[arg-type]
        assert del_msg.message
        assert await svc.get("branding.logo_path") is None
