"""Tests für Task 4: Hintergrund-Lauf pro Tenant (Scheduler + Runner).

Vor diesem Task schlug JEDER Lauf (Cron-Job wie manueller Trigger) fehl: `execute_run`
öffnete eine reine Owner-Session (kein aktiver Tenant-Kontext), `run_repo.create` legt
aber `Run(tenant_id=current_tenant_or_none())` an -- ohne Kontext resolvt das zu `None`,
und `run.tenant_id` ist NOT NULL -> die INSERT schlug fehl. Jetzt liest `SchedulerService`
die aktiven Kunden auf einer Owner-Session und führt `execute_run` je Kunde innerhalb
`use_tenant(tenant_id)` aus -- die im Runner geöffnete Session wird dadurch automatisch
tenant-gescopt (Begin-Listener), `run_repo.create` stempelt den aktiven Tenant korrekt.

`oidc.sync_sso_users` schreibt `app_user` (instanzweit, kein RLS) und muss deshalb auf
einer separaten OWNER-Session laufen, auch während der umschliessende Lauf tenant-gescopt
ist -- das wird hier direkt geprüft (`current_user`/GUC in der Session, die der Runner an
den (gemockten) `sync_sso_users`-Aufruf übergibt).

Graph-/Mail-Aufrufe sind für einen echten End-to-End-Lauf zu schwer (echtes Netzwerk) --
`sync_users`, `oidc.sync_sso_users`, `_resolve_excluded_ids` und der Admin-Alert werden
daher gemockt. Das Ziel dieser Suite ist die Verdrahtung (Tenant-Schleife, Owner-Session
für den SSO-Abgleich, Trigger-Pfad wirft nicht mehr), nicht der fachliche Sync selbst
(der ist an anderer Stelle getestet).

Seed-/Cleanup-Muster wie in `test_route_tenant_scoping.py`: echte Superuser-Connection auf
`migrated_engine`, echt committet, Cleanup im `finally` (die savepoint-isolierte
`session`-Fixture eignet sich hier nicht -- der Begin-Listener braucht ein echtes BEGIN).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.db.session import get_session_factory
from app.services.scheduler import SchedulerService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    """Unabhängige Kontrollabfrage (nicht über einen Modul-Cache)."""
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


@pytest_asyncio.fixture
async def inactive_tenant(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Ein zweiter, INAKTIVER Tenant -- die Lauf-Schleife darf ihn nicht anfassen."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Sts4Inactive','sts4-inactive',false,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()
        try:
            yield tid
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tid})
            await conn.commit()


def _patch_heavy_dependencies(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    async def _fake_sync_users(session: AsyncSession, settings: dict[str, Any]) -> dict[str, int]:
        return {"checked": 0}

    async def _fake_sso_sync(session: AsyncSession, settings: dict[str, Any]) -> dict[str, int]:
        # Der eigentliche Beweis für Punkt 2 des Tasks: diese Funktion muss vom Runner mit
        # einer OWNER-Session aufgerufen werden -- trotz eines aktiven use_tenant(...)
        # rundherum. `app_user` ist instanzweit, kein Rollenwechsel/GUC hier erlaubt.
        captured["oidc_role"] = (await session.execute(text("SELECT current_user"))).scalar_one()
        captured["oidc_guc"] = (
            await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()
        return {"synced": 0, "removed": 0}

    async def _no_excluded(session: Any, settings: dict[str, Any]) -> set[str]:
        return set()

    async def _no_alert(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.services.runner.sync_users", _fake_sync_users)
    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sso_sync)
    monkeypatch.setattr("app.services.runner._resolve_excluded_ids", _no_excluded)
    monkeypatch.setattr("app.services.alerts.maybe_send_run_alert", _no_alert)


async def test_trigger_now_creates_run_stamped_with_active_tenant_id(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kernbeweis des Bugfixes: `trigger_now` wirft nicht mehr (NOT-NULL-Verletzung) und
    der angelegte Lauf trägt die `tenant_id` des aktiven (Default-)Tenants."""
    dtid = await _real_default_tenant_id(migrated_engine)
    captured: dict[str, Any] = {}
    _patch_heavy_dependencies(monkeypatch, captured)

    service = SchedulerService(get_session_factory(), base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    try:
        assert run.tenant_id == dtid, f"Lauf nicht dem aktiven Tenant zugeordnet: {run.tenant_id}"
        assert captured["oidc_role"] == "pwnotify", (
            f"sync_sso_users lief nicht als Owner-Rolle: {captured['oidc_role']}"
        )
        assert captured["oidc_guc"] is None, (
            "Owner-Session darf kein Tenant-GUC gesetzt haben (SET LOCAL ist "
            f"transaktionsgebunden): {captured['oidc_guc']!r}"
        )
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
            await conn.commit()


async def test_trigger_now_does_not_touch_inactive_tenant(
    migrated_engine: AsyncEngine, inactive_tenant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Tenant-Schleife liest nur AKTIVE Kunden -- ein inaktiver Tenant bekommt keinen
    eigenen Lauf, obwohl er in der `tenant`-Tabelle existiert."""
    dtid = await _real_default_tenant_id(migrated_engine)
    captured: dict[str, Any] = {}
    _patch_heavy_dependencies(monkeypatch, captured)

    service = SchedulerService(get_session_factory(), base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    try:
        assert run.tenant_id == dtid
        async with migrated_engine.connect() as conn:
            foreign_runs = (
                await conn.execute(
                    text("SELECT count(*) FROM run WHERE tenant_id = :tid"),
                    {"tid": inactive_tenant},
                )
            ).scalar_one()
        assert foreign_runs == 0, "Ein inaktiver Tenant hat trotzdem einen Lauf bekommen"
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
            await conn.commit()
