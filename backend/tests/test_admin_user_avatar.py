"""TDD für Task B (Multi-Tenant-Feature, Profilbilder auf der Access-Seite):
`AdminUserOut.has_avatar`/`avatar_version` (Task-B-eigene Felder, dateiabgeleitet -- nicht
Spalten von `app_user`) + der neue admin-facing `GET /admin/users/{id}/avatar`-Endpunkt.

Zwei Ebenen, wie im Rest der Suite üblich:
- `test_list_users_reports_avatar_presence`: treibt `list_users` direkt an (Muster aus
  `test_admin_users_scoping.py`) auf der savepoint-isolierten `session`-Fixture -- beweist,
  dass `_admin_user_out` (`admin_users.py`) die Avatar-Felder korrekt aus einem Filesystem-
  `stat` ableitet, für ein Konto MIT und eines OHNE Datei.
- `test_get_user_avatar_http_*`: echter HTTP-Beweis über die volle `create_app()`-ASGI-App
  (Muster aus `test_public_tokens_ratelimit.py`) -- NUR so lässt sich das `AdminUser`-Gate
  (403 für einen Nicht-Admin-Aufrufer) tatsächlich beweisen; ein direkter Funktionsaufruf
  würde die FastAPI-Dependency-Injection (und damit das Gate) umgehen.

`PWNOTIFY_DATA_DIR` wird für die Dauer beider Testarten auf ein Wegwerfverzeichnis umgebogen
(Muster aus `test_branding_tenant_scope.py`s `tmp_data_dir`) -- sonst würde unter dem
produktiven `/data` gesucht/geschrieben."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, default_tenant_id
from app.api.routes.admin_users import list_users
from app.core.config import get_settings
from app.core.security import issue_token_pair
from app.main import create_app
from app.models.user import AppUser
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
    beliebiges Ziel-Konto, dessen Avatar geholt wird."""
    ids: dict[str, int] = {}
    async with migrated_engine.connect() as conn:
        for key, role in (("admin", "admin"), ("auditor", "auditor"), ("target", "admin")):
            uid = int(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (username, password_hash, role, is_active, "
                            "is_sso, failed_login_count, language, created_at, updated_at) "
                            "VALUES (:username, 'x', :role, true, false, 0, 'de', now(), now()) "
                            "RETURNING id"
                        ),
                        {"username": f"tb-http-{key}-{uuid.uuid4().hex[:8]}", "role": role},
                    )
                ).scalar_one()
            )
            ids[key] = uid
        await conn.commit()
        try:
            yield ids
        finally:
            await conn.execute(
                text("DELETE FROM app_user WHERE id = ANY(:ids)"), {"ids": list(ids.values())}
            )
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
