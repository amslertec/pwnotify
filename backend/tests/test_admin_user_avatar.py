"""TDD für Task B (Multi-Tenant-Feature, Profilbilder auf der Access-Seite):
`AdminUserOut.has_avatar`/`avatar_version` (Task-B-eigene Felder, dateiabgeleitet -- nicht
Spalten von `app_user`) + der admin-facing `GET /admin/users/{id}/avatar`-Endpunkt.

Drei Ebenen, wie im Rest der Suite üblich:
- `test_list_users_reports_avatar_presence`: treibt `list_users` direkt an (Muster aus
  `test_admin_users_scoping.py`) auf der savepoint-isolierten `session`-Fixture -- beweist,
  dass `_admin_user_out` (`admin_users.py`) die Avatar-Felder korrekt aus einem Filesystem-
  `stat` ableitet, für ein Konto MIT und eines OHNE Datei.
- `test_get_user_avatar_http_*`: echter HTTP-Beweis über die volle `create_app()`-ASGI-App
  (Muster aus `test_public_tokens_ratelimit.py`) -- NUR so lässt sich das `AdminUser`-Gate
  (403 für einen Nicht-Admin-Aufrufer) tatsächlich beweisen; ein direkter Funktionsaufruf
  würde die FastAPI-Dependency-Injection (und damit das Gate) umgehen.
- `test_get_user_avatar_scope_*` (Task 6, M6): proves the route's subset-scope rule
  (`set_role`/`delete_user`/`send_reset` pattern). Driven like `test_audit_tenant_scope.py`'s
  `_audit_session_for` -- a real access token + `get_current_user`, then the route function
  called DIRECTLY (not via the ASGI app); the gate itself (`AdminUser` role check) is
  already proven by `test_get_user_avatar_http_*`, here it's about the tenant scope
  AFTER that, which only a genuinely committed multi-tenant seed (RLS-free superuser
  connection, see `test_audit_tenant_scope.py`) can credibly demonstrate.

`PWNOTIFY_DATA_DIR` wird für die Dauer aller Testarten auf ein Wegwerfverzeichnis umgebogen
(Muster aus `test_branding_tenant_scope.py`s `tmp_data_dir`) -- sonst würde unter dem
produktiven `/data` gesucht/geschrieben."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, default_tenant_id, get_current_user
from app.api.routes.admin_users import get_user_avatar, list_users
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.main import create_app
from app.models.user import AppUser
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest_asyncio.fixture
async def tmp_data_dir(tmp_path) -> AsyncGenerator[None]:
    """Gleiches ENV-Override-Pattern wie `test_branding_tenant_scope.py`s `tmp_data_dir` --
    Avatare landen unter `{data_dir}/avatars`."""
    prev = os.environ.get("PWNOTIFY_DATA_DIR")
    os.environ["PWNOTIFY_DATA_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    yield
    if prev is None:
        os.environ.pop("PWNOTIFY_DATA_DIR", None)
    else:
        os.environ["PWNOTIFY_DATA_DIR"] = prev
    get_settings.cache_clear()


def _write_avatar(user_id: int) -> bytes:
    avatar_dir = Path(get_settings().data_dir) / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    data = b"\x89PNG\r\n\x1a\nfake-avatar-bytes"
    (avatar_dir / f"{user_id}.png").write_bytes(data)
    return data


async def _mk_user(session: AsyncSession, *, role: str, tenant_id: int | None = None) -> AppUser:
    u = AppUser(
        username=f"tb-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=False,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


async def test_list_users_reports_avatar_presence(
    session: AsyncSession, tmp_data_dir: None
) -> None:
    """Ein Konto MIT seedeter Avatar-Datei -> `has_avatar=True` + `avatar_version > 0`;
    eines OHNE -> `False`/`0`. Beweis über den echten `list_users`-Pfad (Superadmin im
    Default-Kontext, s. `test_admin_users_scoping.py` für dasselbe Muster)."""
    default_id = await default_tenant_id(session)
    superadmin = await _mk_user(session, role="superadmin")
    with_avatar = await _mk_user(session, role="admin", tenant_id=default_id)
    without_avatar = await _mk_user(session, role="admin", tenant_id=default_id)
    assert with_avatar.id is not None and without_avatar.id is not None

    _write_avatar(with_avatar.id)

    result = await list_users(superadmin, session, default_id)
    by_id = {u.id: u for u in result["local"]}

    assert by_id[with_avatar.id].has_avatar is True
    assert by_id[with_avatar.id].avatar_version > 0

    assert by_id[without_avatar.id].has_avatar is False
    assert by_id[without_avatar.id].avatar_version == 0


@pytest_asyncio.fixture
async def avatar_route_users(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, int]]:
    """Echte, committete Konten (kein savepoint-isoliertes `session`-Fixture -- die
    ASGI-App unten öffnet ihre eigenen DB-Sessions, sieht die savepoint-Fixture also nicht):
    ein Admin-Aufrufer, ein Auditor-Aufrufer (Nicht-Admin, für den 403-Beweis) und ein
    beliebiges Ziel-Konto, dessen Avatar geholt wird. `admin` and `target` share ONE
    tenant (Task 6, M6: the route is now subset-scoped -- without a shared
    `admin_tenant` grant, the 200 case below would itself become a scope violation)."""
    ids: dict[str, int] = {}
    async with migrated_engine.connect() as conn:
        tenant_id = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) "
                        "VALUES ('AvatarRoute', :slug, true, now()) RETURNING id"
                    ),
                    {"slug": f"avatar-route-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
        )
        ids["tenant"] = tenant_id
        for key, role in (("admin", "admin"), ("auditor", "auditor"), ("target", "admin")):
            uid = int(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (username, password_hash, role, is_active, "
                            "is_sso, tenant_id, failed_login_count, language, created_at, "
                            "updated_at) VALUES (:username, 'x', :role, true, false, :tid, 0, "
                            "'de', now(), now()) RETURNING id"
                        ),
                        {
                            "username": f"tb-http-{key}-{uuid.uuid4().hex[:8]}",
                            "role": role,
                            "tid": tenant_id,
                        },
                    )
                ).scalar_one()
            )
            ids[key] = uid
        await conn.execute(
            text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:uid, :tid)"),
            {"uid": ids["admin"], "tid": tenant_id},
        )
        await conn.execute(
            text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:uid, :tid)"),
            {"uid": ids["target"], "tid": tenant_id},
        )
        await conn.commit()
        try:
            yield ids
        finally:
            await conn.execute(
                text("DELETE FROM app_user WHERE id = ANY(:ids)"),
                {"ids": [v for k, v in ids.items() if k != "tenant"]},
            )
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tenant_id})
            await conn.commit()


async def test_get_user_avatar_http_200_404_403(
    migrated_engine: AsyncEngine, avatar_route_users: dict[str, int], tmp_data_dir: None
) -> None:
    target_id = avatar_route_users["target"]
    png_bytes = _write_avatar(target_id)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    admin_token = issue_token_pair(str(avatar_route_users["admin"])).access_token
    auditor_token = issue_token_pair(str(avatar_route_users["auditor"])).access_token

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 200 + Bildbytes + Ein-Stunden-Cache (die URL trägt `avatar_version` als
        # Cache-Buster, s. `admin_users.get_user_avatar`-Doku).
        resp = await client.get(
            f"/api/admin/users/{target_id}/avatar", cookies={ACCESS_COOKIE: admin_token}
        )
        assert resp.status_code == 200
        assert resp.content == png_bytes
        assert resp.headers["cache-control"] == "max-age=3600"

        # 404 `no_avatar` für ein Konto ohne Datei.
        no_avatar_id = avatar_route_users["auditor"]
        resp = await client.get(
            f"/api/admin/users/{no_avatar_id}/avatar", cookies={ACCESS_COOKIE: admin_token}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "no_avatar"

        # 403 für einen Nicht-Admin-Aufrufer (Auditor) -- beweist das `AdminUser`-Gate.
        resp = await client.get(
            f"/api/admin/users/{target_id}/avatar", cookies={ACCESS_COOKIE: auditor_token}
        )
        assert resp.status_code == 403


# ---- get_user_avatar: subset-scope (Task 6, M6) -------------------------------------------- #
#
# THE bug this block proves: `get_user_avatar` was gated ONLY by `AdminUser` (any admin of
# ANY tenant) and resolved the avatar file straight off `user_id` -- no tenant check at all.
# A local admin of tenant A could read the cached profile photo of ANY account, including
# accounts homed exclusively in a foreign tenant B or a superadmin's own photo (`user_id`s
# are sequential and enumerable). The fix applies the same subset-scope rule as
# `set_role`/`delete_user`/`send_reset`, but denies with `NotFoundError` (not
# `ForbiddenError`) so an out-of-scope account is indistinguishable from "no photo" -- a
# 403-vs-404 split here would itself be an existence oracle for `user_id`.


class _FakeRequest:
    """Duck-typed Request -- `get_current_user` only reads `.cookies`."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


@pytest_asyncio.fixture
async def avatar_scope_seed(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, int]]:
    """Two tenants (A, B) + a local admin granted ONLY on A + one target account homed AND
    `admin_tenant`-granted on A (own-scope control) + one target account homed AND granted
    on B (cross-tenant proof) + a superadmin (no tenant grant, instance-wide bypass). Real,
    committed rows over an RLS-free superuser connection -- pattern from
    `test_audit_tenant_scope.py`'s `audit_seed`."""
    ids: dict[str, int] = {}
    async with migrated_engine.connect() as conn:
        a, b = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('AvatarScopeA', :sa, true, now()), ('AvatarScopeB', :sb, true, now()) "
                        "RETURNING id"
                    ),
                    {
                        "sa": f"avs-a-{uuid.uuid4().hex[:8]}",
                        "sb": f"avs-b-{uuid.uuid4().hex[:8]}",
                    },
                )
            )
            .scalars()
            .all()
        )
        ids["a"], ids["b"] = a, b
        for key, role, tid in (
            ("local_admin", "admin", a),
            ("target_a", "admin", a),
            ("target_b", "admin", b),
            ("superadmin", "superadmin", None),
        ):
            uid = int(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (username, password_hash, role, is_active, "
                            "is_sso, tenant_id, failed_login_count, language, created_at, "
                            "updated_at) VALUES (:username, 'x', :role, true, false, :tid, 0, "
                            "'de', now(), now()) RETURNING id"
                        ),
                        {
                            "username": f"avs-{key}-{uuid.uuid4().hex[:8]}",
                            "role": role,
                            "tid": tid,
                        },
                    )
                ).scalar_one()
            )
            ids[key] = uid
        for key, tid in (("local_admin", a), ("target_a", a), ("target_b", b)):
            await conn.execute(
                text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:uid, :tid)"),
                {"uid": ids[key], "tid": tid},
            )
        await conn.commit()
        try:
            yield ids
        finally:
            await conn.execute(
                text("DELETE FROM app_user WHERE id = ANY(:ids)"),
                {"ids": [v for k, v in ids.items() if k not in ("a", "b")]},
            )
            await conn.execute(text("DELETE FROM tenant WHERE id = ANY(:ids)"), {"ids": [a, b]})
            await conn.commit()


def _assert_file_response_bytes(resp: FileResponse, expected: bytes) -> None:
    """Plain (non-async) helper -- keeps the blocking `pathlib` I/O out of the async test
    body (ruff ASYNC240), pattern from `test_branding_cross_tenant_isolation.py`'s
    `_assert_file_contains`."""
    assert Path(str(resp.path)).read_bytes() == expected


async def _get_avatar_as(uid: int, target_id: int) -> FileResponse:
    """Resolves `uid` into a live admin exactly like FastAPI would (real access token +
    `get_current_user`), then calls `get_user_avatar` DIRECTLY on the same owner session --
    the route's `session: SessionDep` IS an owner session (no RLS role switch), so this
    matches what the real dependency chain hands the route. Identical pattern to
    `test_audit_tenant_scope.py`'s `_audit_session_for`, just without the context-manager
    wrapper (the route itself, not a generator dependency, is under test here)."""
    pair = issue_token_pair(str(uid))
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        admin = await get_current_user(request, owner)  # type: ignore[arg-type]
        return await get_user_avatar(request, admin, target_id, owner)  # type: ignore[arg-type]


async def test_get_user_avatar_cross_tenant_admin_denied_no_oracle(
    avatar_scope_seed: dict[str, int], tmp_data_dir: None
) -> None:
    """A's local admin must NOT be able to fetch B-only target's cached avatar. Non-vacuous:
    the target's photo genuinely exists on disk -- the denial is a real scope decision, not
    an accidental 'file missing'."""
    _write_avatar(avatar_scope_seed["target_b"])

    with pytest.raises(NotFoundError) as exc_info:
        await _get_avatar_as(avatar_scope_seed["local_admin"], avatar_scope_seed["target_b"])
    assert exc_info.value.code == "no_avatar"


async def test_get_user_avatar_own_tenant_admin_gets_the_image(
    avatar_scope_seed: dict[str, int], tmp_data_dir: None
) -> None:
    """Regression guard: the new scope check only blocks FOREIGN tenants -- within the
    caller's own scope, the route still serves the real image bytes."""
    png_bytes = _write_avatar(avatar_scope_seed["target_a"])

    resp = await _get_avatar_as(avatar_scope_seed["local_admin"], avatar_scope_seed["target_a"])

    _assert_file_response_bytes(resp, png_bytes)


async def test_get_user_avatar_superadmin_bypasses_scope(
    avatar_scope_seed: dict[str, int], tmp_data_dir: None
) -> None:
    """Superadmin bypass, same as `set_role`/`delete_user`/`send_reset`: full cross-tenant
    access, no scope check applied."""
    png_bytes = _write_avatar(avatar_scope_seed["target_b"])

    resp = await _get_avatar_as(avatar_scope_seed["superadmin"], avatar_scope_seed["target_b"])

    _assert_file_response_bytes(resp, png_bytes)
