"""Test für Task 5: `admin_users.sync_sso` läuft jetzt PRO aktivem Tenant statt einmal auf
der Owner-Session (Phase-3-TODO geschlossen) -- jeder Kunde hat seine eigene
`oidc.admin_group_id`/`oidc.enabled`-Konfiguration. Vormals (Owner-Session, `select(Setting)`
ohne Tenant-Filter, weil RLS für die Owner-Rolle nicht greift) hätte ein zweiter Tenant zu
einem undefinierten Gemisch der `oidc.*`-Werte geführt.

`oidc.sync_sso_users` selbst (Graph-Aufrufe etc.) ist an anderer Stelle getestet -- hier
wird nur die Verdrahtung geprüft (welche Settings pro Tenant tatsächlich ankommen), analog
zu `test_scheduler_tenant_scope.py`. Seed-Pattern wie dort: echte Superuser-Connection auf
`migrated_engine`, echt committet, Cleanup im `finally`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.api.routes.admin_users import sync_sso
from app.db.session import get_session_factory
from app.repositories import user_repo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class _FakeRequest:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


@pytest_asyncio.fixture
async def two_tenants_one_configured(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int]]:
    """Ein aktiver Tenant mit `oidc.enabled`+`oidc.admin_group_id`, ein zweiter aktiver
    Tenant ganz ohne oidc-Konfiguration -- der zweite darf NICHT synchronisiert werden."""
    async with migrated_engine.connect() as conn:
        configured = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Sso5Configured','sso5-configured',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        unconfigured = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Sso5Unconfigured','sso5-unconfigured',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:tid, 'oidc.enabled', to_jsonb(true), false, now()), "
                "(:tid, 'oidc.admin_group_id', to_jsonb('group-configured'::text), false, now())"
            ),
            {"tid": configured},
        )
        await conn.commit()
        try:
            yield configured, unconfigured
        finally:
            await conn.execute(
                text("DELETE FROM setting WHERE tenant_id IN (:a, :b)"),
                {"a": configured, "b": unconfigured},
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b)"),
                {"a": configured, "b": unconfigured},
            )
            await conn.commit()


async def test_sync_sso_only_syncs_the_tenant_with_its_own_oidc_config(
    two_tenants_one_configured: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    async def _fake_sync(
        session: Any, settings: dict[str, Any], *, tenant_id: int
    ) -> dict[str, int]:
        seen.append(settings)
        return {"synced": 1, "removed": 0}

    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sync)

    async with get_session_factory()() as session:
        superadmin = await user_repo.create(
            session,
            username=f"sso5-super-{uuid.uuid4().hex[:8]}",
            password_hash="x",
            role="superadmin",
            is_sso=False,
        )
        try:
            msg = await sync_sso(_FakeRequest(), superadmin, session)  # type: ignore[arg-type]
        finally:
            await user_repo.delete(session, superadmin.id)

    group_ids = [s.get("oidc.admin_group_id") for s in seen]
    assert group_ids == ["group-configured"], (
        f"sync_sso_users lief nicht (nur) für den konfigurierten Tenant: {group_ids}"
    )
    assert "1 SSO-Benutzer synchronisiert" in msg.message
