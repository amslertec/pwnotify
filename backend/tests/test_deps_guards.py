"""TDD für die Access-Modell/Superadmin-Guard-Rewrite (Task 2) in `app.api.deps`:

- `require_admin`: MUSS jetzt auch den Superadmin durchlassen (`role != "admin"` hätte ihn
  vorher fälschlich 403'd -- CRITICAL-Regression, die dieser Task behebt).
- `require_local_admin`: Admin ODER Superadmin, aber KEIN SSO-Konto.
- `require_superadmin` (neu): NUR der lokale Superadmin, sonst 403 `superadmin_required`.
- `get_audit_session`: die Owner-/Alle-Mandanten-Session gehört jetzt NUR noch dem lokalen
  Superadmin -- ein lokaler (Nicht-Super-)Admin fällt (anders als im alten Drei-Wege-Modell)
  auf die RLS-gescopte Session zurück.

Die reinen Guard-Funktionen (`require_*`) prüfen nur `user.is_sso`/`user.role` -- kein
DB-Zugriff nötig, ein roh konstruiertes `AppUser`-Objekt (nicht persistiert) genügt.
`get_audit_session` öffnet dagegen für den nicht-superadmin-Zweig über
`tenant_scoped_session` eine ECHTE, separate Verbindung (SET LOCAL ROLE + GUC) -- dafür
wird hier, wie in `test_audit_tenant_scope.py`, mit einer echt committeten Superuser-
Connection geseedet (Cleanup im `finally`).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.api.deps import (
    ACCESS_COOKIE,
    get_audit_session,
    get_current_user,
    require_admin,
    require_local_admin,
    require_superadmin,
)
from app.core.errors import ForbiddenError
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.models.user import AppUser
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def _user(*, role: str, is_sso: bool = False) -> AppUser:
    """Roh konstruiertes, NICHT persistiertes `AppUser` -- die Guards lesen nur
    `.is_sso`/`.role`, kein DB-Zugriff nötig."""
    return AppUser(
        username=f"guard-{uuid.uuid4().hex[:8]}", password_hash="x", role=role, is_sso=is_sso
    )


# ---- require_admin: Superadmin MUSS durchkommen (die CRITICAL-Regression) -------------------- #


async def test_require_admin_passes_superadmin() -> None:
    admin = await require_admin(_user(role="superadmin"))
    assert admin.role == "superadmin"


async def test_require_admin_passes_local_admin() -> None:
    admin = await require_admin(_user(role="admin"))
    assert admin.role == "admin"


async def test_require_admin_passes_sso_admin() -> None:
    admin = await require_admin(_user(role="admin", is_sso=True))
    assert admin.is_sso is True


async def test_require_admin_rejects_auditor() -> None:
    with pytest.raises(ForbiddenError) as exc_info:
        await require_admin(_user(role="auditor"))
    assert exc_info.value.code == "admin_required"


# ---- require_local_admin: Admin ODER Superadmin, aber KEIN SSO-Konto ------------------------- #


async def test_require_local_admin_passes_superadmin() -> None:
    admin = await require_local_admin(_user(role="superadmin"))
    assert admin.role == "superadmin"


async def test_require_local_admin_passes_local_admin() -> None:
    admin = await require_local_admin(_user(role="admin"))
    assert admin.role == "admin"


async def test_require_local_admin_rejects_sso_admin() -> None:
    """Ein SSO-Admin (auch role=='admin') ist mandantengebunden, kein lokales Konto --
    muss `require_local_admin` mit 403 scheitern."""
    with pytest.raises(ForbiddenError) as exc_info:
        await require_local_admin(_user(role="admin", is_sso=True))
    assert exc_info.value.code == "local_admin_required"


async def test_require_local_admin_rejects_auditor() -> None:
    with pytest.raises(ForbiddenError) as exc_info:
        await require_local_admin(_user(role="auditor"))
    assert exc_info.value.code == "local_admin_required"


# ---- require_superadmin (neu): NUR der lokale Superadmin -------------------------------------- #


async def test_require_superadmin_passes_superadmin() -> None:
    admin = await require_superadmin(_user(role="superadmin"))
    assert admin.role == "superadmin"


async def test_require_superadmin_rejects_local_admin() -> None:
    """Kernbeweis der Access-Modell-Verschärfung: ein lokaler (Nicht-Super-)Admin ist NICHT
    mehr instanzweit und besteht `require_superadmin` NICHT."""
    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin(_user(role="admin"))
    assert exc_info.value.code == "superadmin_required"


async def test_require_superadmin_rejects_sso_admin() -> None:
    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin(_user(role="admin", is_sso=True))
    assert exc_info.value.code == "superadmin_required"


async def test_require_superadmin_rejects_auditor() -> None:
    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin(_user(role="auditor"))
    assert exc_info.value.code == "superadmin_required"


# ---- get_audit_session: Owner-Session NUR für den lokalen Superadmin ------------------------- #


class _FakeRequest:
    """Duck-typed Request -- `get_current_user`/`get_audit_session` lesen nur `.cookies`."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


@pytest_asyncio.fixture
async def audit_session_seed(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, int]]:
    """Ein aktiver Tenant + ein lokaler Superadmin (kein Tenant gebunden) + ein lokaler
    (Nicht-Super-)Admin MIT `admin_tenant`-Grant auf genau diesen Tenant -- echt committet
    (Superuser-Connection, RLS-frei), Cleanup im `finally`."""
    async with migrated_engine.connect() as conn:
        tenant_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                    "('GuardT','guard-t',true,now()) RETURNING id"
                )
            )
        ).scalar_one()
        superadmin_id = (
            await conn.execute(
                text(
                    "INSERT INTO app_user "
                    "(username, password_hash, role, is_active, is_sso, "
                    "failed_login_count, language, created_at, updated_at) VALUES "
                    "('guard-superadmin@local', 'x', 'superadmin', true, false, 0, 'de', "
                    "now(), now()) RETURNING id"
                )
            )
        ).scalar_one()
        local_admin_id = (
            await conn.execute(
                text(
                    "INSERT INTO app_user "
                    "(username, password_hash, role, is_active, is_sso, "
                    "failed_login_count, language, created_at, updated_at) VALUES "
                    "('guard-local-admin@local', 'x', 'admin', true, false, 0, 'de', "
                    "now(), now()) RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:uid, :tid)"),
            {"uid": local_admin_id, "tid": tenant_id},
        )
        await conn.commit()
        try:
            yield {
                "tenant_id": int(tenant_id),
                "superadmin_id": int(superadmin_id),
                "local_admin_id": int(local_admin_id),
            }
        finally:
            await conn.execute(
                text("DELETE FROM app_user WHERE id IN (:x, :y)"),
                {"x": superadmin_id, "y": local_admin_id},
            )
            await conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tenant_id})
            await conn.commit()


@contextlib.asynccontextmanager
async def _audit_session_for(
    uid: int, *, active_tenant: int | None = None
) -> AsyncGenerator[tuple[AsyncSession, AsyncSession]]:
    """Treibt `get_audit_session` exakt wie FastAPI es pro Request täte und gibt sowohl die
    Owner-`session` als auch die von der Dependency gelieferte Session zurück -- so lässt
    sich prüfen, ob letztere IDENTISCH mit der Owner-Session ist (Superadmin) oder eine
    ANDERE, RLS-gescopte Session ist (jedes andere Konto)."""
    pair = issue_token_pair(str(uid), active_tenant=active_tenant)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_audit_session(request, user, owner)
        try:
            yield owner, await anext(gen)
        finally:
            await gen.aclose()


async def test_get_audit_session_gives_owner_session_only_to_local_superadmin(
    audit_session_seed: dict[str, int],
) -> None:
    async with _audit_session_for(audit_session_seed["superadmin_id"]) as (owner, audit):
        assert audit is owner, "Superadmin muss exakt die Owner-Session bekommen (alle Mandanten)"
        guc = (
            await audit.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()
        assert guc in (None, ""), f"Owner-Session darf keinen Tenant-GUC gesetzt haben: {guc}"


async def test_get_audit_session_scopes_local_admin_not_owner(
    audit_session_seed: dict[str, int],
) -> None:
    """Nicht-vakuoser Beweis der Kernänderung: ein lokaler (Nicht-Super-)Admin bekommt NICHT
    mehr die Owner-/Alle-Mandanten-Sicht (altes Drei-Wege-Modell), sondern eine
    RLS-gescopte Session auf genau seinen `admin_tenant`-gewährten Tenant."""
    tid = audit_session_seed["tenant_id"]
    async with _audit_session_for(audit_session_seed["local_admin_id"], active_tenant=tid) as (
        owner,
        audit,
    ):
        assert audit is not owner, "Lokaler Admin darf NICHT die Owner-Session bekommen"
        guc = (
            await audit.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()
        assert guc == str(tid), f"Lokaler Admin sollte RLS-gescoped auf {tid} sein, GUC={guc!r}"
