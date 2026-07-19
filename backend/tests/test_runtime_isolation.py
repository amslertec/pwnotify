"""Task 6: Isolations-Regressionstests für die ECHTEN Laufzeitpfade (Routes + Scheduler).

Bisherige Suiten (`test_isolation_attack.py`, `test_route_tenant_scoping.py`,
`test_scheduler_tenant_scope.py`) belegen den Mechanismus (`tenant_scoped_session`) und
je EINEN aktiven (Default-)Tenant gegen einen fremden/inaktiven Tenant. Dieser Task
schliesst zwei echte Lücken für die Phase-4-Realität (mehrere GLEICHZEITIG aktive Kunden):

1. Zwei AKTIVE Tenants A und B, je mit `run`/`entra_user`/`notification_log` seedet --
   sowohl Lese- als auch Schreibpfad (echter ORM-Writer `run_repo.create` unter
   `use_tenant(...)`, wie ihn `execute_run` produktiv nutzt) bleiben in BEIDE Richtungen
   isoliert, nicht nur A-gegen-fremd.
2. Der Owner-Pfad (Login/admin_users-artiger Zugriff auf `app_user`, instanzweit, kein
   RLS) sieht weiterhin über alle Tenants hinweg -- auch wenn er (wie im echten Runner
   bei `oidc.sync_sso_users`) über `use_owner_context()` INNERHALB eines aktiven
   `use_tenant(...)`-Blocks geöffnet wird. Das beweist den Owner/Tenant-Split in beide
   Richtungen: Tenant-Pfade isoliert, Owner-Pfade global.
3. Der echte Scheduler-Lauf (`SchedulerService.trigger_now`) erzeugt bei zwei aktiven
   Tenants GENAU einen Lauf pro Tenant, korrekt gestempelt -- Erweiterung von
   `test_scheduler_tenant_scope.py`s Einzeltenant-Fall auf echte Mehrmandantenschleife.

Seed-/Cleanup-Muster wie in `test_isolation_attack.py`: echte Superuser-Connection auf
`migrated_engine`, echt committet, Cleanup im `finally` (die savepoint-isolierte
`session`-Fixture eignet sich hier nicht -- siehe Kommentar dort).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from typing import Any, TypedDict

import pytest
import pytest_asyncio
from app.db.session import get_session_factory
from app.db.tenant_context import (
    open_active_session,
    tenant_scoped_session,
    use_owner_context,
    use_tenant,
)
from app.repositories import run_repo, user_repo
from app.services.scheduler import SchedulerService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class _RowSet(TypedDict):
    run: int
    entra_user: int
    notification_log: int


class _TwoTenants(TypedDict):
    a: int
    b: int
    rows_a: _RowSet
    rows_b: _RowSet


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    """Unabhängige Kontrollabfrage (nicht über einen Modul-Cache)."""
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


async def _seed_row_set(conn: Any, tenant_id: int, tag: str) -> _RowSet:
    """Seedet je eine `run`/`entra_user`/`notification_log`-Zeile für einen Tenant."""
    run_id = (
        await conn.execute(
            text(
                "INSERT INTO run "
                "(tenant_id, trigger, dry_run, status, started_at, "
                "checked_users, sent, failed, skipped, detail_log) VALUES "
                "(:tid,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb) RETURNING id"
            ),
            {"tid": tenant_id},
        )
    ).scalar_one()
    entra_user_id = (
        await conn.execute(
            text(
                "INSERT INTO entra_user "
                "(tenant_id, entra_id, upn, display_name, other_mails, "
                "account_enabled, password_never_expires, excluded, is_shared, "
                "raw, last_synced_at) VALUES "
                "(:tid, :eid, :upn, '', '[]'::jsonb, true, false, false, false, "
                "'{}'::jsonb, now()) RETURNING id"
            ),
            {"tid": tenant_id, "eid": f"rti-{tag}-entra", "upn": f"rti-{tag}@example.com"},
        )
    ).scalar_one()
    notification_log_id = (
        await conn.execute(
            text(
                "INSERT INTO notification_log "
                "(tenant_id, entra_user_id, run_id, reminder_day, expiry_cycle, "
                "channel, backend, recipient, language, status, created_at) VALUES "
                "(:tid, :euid, :rid, 7, '2026-01-01', 'primary', 'smtp', :rcpt, "
                "'de', 'sent', now()) RETURNING id"
            ),
            {
                "tid": tenant_id,
                "euid": entra_user_id,
                "rid": run_id,
                "rcpt": f"rti-{tag}@example.com",
            },
        )
    ).scalar_one()
    return {
        "run": int(run_id),
        "entra_user": int(entra_user_id),
        "notification_log": int(notification_log_id),
    }


@pytest_asyncio.fixture
async def two_active_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[_TwoTenants]:
    """Zwei ECHT aktive Tenants (nicht Default-vs-fremd), je mit vollem Datensatz."""
    async with migrated_engine.connect() as conn:
        a, b = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('RtiA','rti-a',true,now()), ('RtiB','rti-b',true,now()) "
                        "RETURNING id"
                    )
                )
            )
            .scalars()
            .all()
        )
        await conn.commit()
        rows_a = await _seed_row_set(conn, a, "a")
        rows_b = await _seed_row_set(conn, b, "b")
        await conn.commit()
        try:
            yield {"a": int(a), "b": int(b), "rows_a": rows_a, "rows_b": rows_b}
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


# ---- 1. Zwei aktive Tenants: Lese- und Schreibpfad bleiben in beide Richtungen isoliert -- #


async def test_two_active_tenants_read_isolated_in_both_directions(
    two_active_tenants: _TwoTenants,
) -> None:
    """Der echte Sitzungspfad (`tenant_scoped_session`, von `get_tenant_session` und dem
    Scheduler-Runner genutzt) zeigt innerhalb `use_tenant(A)` NUR A's Zeilen -- über alle
    drei RLS-Tabellen hinweg -- und innerhalb `use_tenant(B)` NUR B's. Nicht nur A-gegen-
    fremd wie in `test_isolation_attack.py`, sondern zwei GLEICHZEITIG aktive Kunden."""
    a, b = two_active_tenants["a"], two_active_tenants["b"]
    rows_a, rows_b = two_active_tenants["rows_a"], two_active_tenants["rows_b"]

    async with tenant_scoped_session(a) as s:
        runs = set((await s.execute(text("SELECT id FROM run"))).scalars().all())
        entra = set((await s.execute(text("SELECT id FROM entra_user"))).scalars().all())
        notif = set((await s.execute(text("SELECT id FROM notification_log"))).scalars().all())
    assert rows_a["run"] in runs and rows_b["run"] not in runs
    assert rows_a["entra_user"] in entra and rows_b["entra_user"] not in entra
    assert rows_a["notification_log"] in notif and rows_b["notification_log"] not in notif

    async with tenant_scoped_session(b) as s:
        runs = set((await s.execute(text("SELECT id FROM run"))).scalars().all())
        entra = set((await s.execute(text("SELECT id FROM entra_user"))).scalars().all())
        notif = set((await s.execute(text("SELECT id FROM notification_log"))).scalars().all())
    assert rows_b["run"] in runs and rows_a["run"] not in runs
    assert rows_b["entra_user"] in entra and rows_a["entra_user"] not in entra
    assert rows_b["notification_log"] in notif and rows_a["notification_log"] not in notif


async def test_two_active_tenants_write_path_stamps_and_stays_isolated(
    migrated_engine: AsyncEngine, two_active_tenants: _TwoTenants
) -> None:
    """Der echte ORM-Schreibpfad (`run_repo.create`, genau das, was `execute_run` pro Lauf
    aufruft) unter `use_tenant(...)`: stempelt automatisch den jeweils aktiven Tenant
    (`default_factory=current_tenant_or_none`) und die neue Zeile bleibt für den ANDEREN
    aktiven Tenant unsichtbar -- in beide Richtungen."""
    a, b = two_active_tenants["a"], two_active_tenants["b"]

    async with use_tenant(a), get_session_factory()() as s:
        run_a = await run_repo.create(s, trigger="manual", dry_run=True)
    async with use_tenant(b), get_session_factory()() as s:
        run_b = await run_repo.create(s, trigger="manual", dry_run=True)

    try:
        assert run_a.tenant_id == a, f"Schreibpfad stempelte falschen Tenant: {run_a.tenant_id}"
        assert run_b.tenant_id == b, f"Schreibpfad stempelte falschen Tenant: {run_b.tenant_id}"

        async with tenant_scoped_session(a) as s:
            ids = set((await s.execute(text("SELECT id FROM run"))).scalars().all())
        assert run_a.id in ids and run_b.id not in ids, f"Leck Richtung A: {ids}"

        async with tenant_scoped_session(b) as s:
            ids = set((await s.execute(text("SELECT id FROM run"))).scalars().all())
        assert run_b.id in ids and run_a.id not in ids, f"Leck Richtung B: {ids}"
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(
                text("DELETE FROM run WHERE id IN (:ra, :rb)"),
                {"ra": run_a.id, "rb": run_b.id},
            )
            await conn.commit()


# ---- 2. Owner-Pfad bleibt instanzweit, auch verschachtelt in einem aktiven Tenant ------- #


async def test_owner_path_sees_app_user_across_tenants_even_nested_in_tenant_context(
    migrated_engine: AsyncEngine, two_active_tenants: _TwoTenants
) -> None:
    """`app_user` trägt zwar `tenant_id` (SSO-Konten sind an einen Kunden gebunden), ist
    aber NICHT in `RLS_TABLES` (Migration `c4d5e6f7a8b9`) -- Login/`admin_users` laufen
    auf der Owner-Rolle und sehen instanzweit. Der Test öffnet die Owner-Session bewusst
    über `use_owner_context()` INNERHALB eines aktiven `use_tenant(A)`-Blocks -- exakt das
    Muster, das `runner.py` für `oidc.sync_sso_users` produktiv nutzt -- und beweist: die
    Rolle bleibt Owner (kein GUC), und `user_repo.list_all` liefert BEIDE SSO-Konten
    (Tenant A und Tenant B), nicht nur das des aktiven Tenants."""
    a, b = two_active_tenants["a"], two_active_tenants["b"]
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO app_user "
                "(username, password_hash, role, is_active, failed_login_count, "
                "tenant_id, created_at, updated_at) VALUES "
                "('rti-sso-a', 'x', 'admin', true, 0, :a, now(), now()), "
                "('rti-sso-b', 'x', 'admin', true, 0, :b, now(), now())"
            ),
            {"a": a, "b": b},
        )
        await conn.commit()
    try:
        async with use_tenant(a):
            with use_owner_context():
                async with get_session_factory()() as owner_session:
                    role, guc = (
                        await owner_session.execute(
                            text("SELECT current_user, current_setting('app.current_tenant', true)")
                        )
                    ).one()
                    users = await user_repo.list_all(owner_session)

        assert role == "pwnotify", f"Owner-Pfad lief nicht als Owner-Rolle: {role}"
        # NULL (nie gesetzt) ODER '' (Reset-Wert eines Custom-GUC-Platzhalters, siehe
        # Postgres-Doku: einmal in DIESER physischen Verbindung per SET LOCAL genutzt,
        # bleibt der Reset-Wert danach '' statt NULL) zählen beide als "kein Tenant" --
        # exakt die Fail-safe-Konvention der App selbst (`NULLIF(current_setting(...), '')`
        # in der RLS-Policy, Migration `c4d5e6f7a8b9`).
        assert not guc, f"Owner-Pfad hat ein Tenant-GUC gesehen (Leck): {guc!r}"
        usernames = {u.username for u in users}
        assert {"rti-sso-a", "rti-sso-b"}.issubset(usernames), (
            f"Owner-Pfad sah nicht über beide Tenants hinweg: {usernames}"
        )
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(
                text("DELETE FROM app_user WHERE username IN ('rti-sso-a', 'rti-sso-b')")
            )
            await conn.commit()


# ---- 3. Scheduler-Schleife über zwei aktive Tenants: ein Lauf pro Tenant ---------------- #


def _patch_heavy_run_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Netzwerk-/Mail-lastige Schritte des Runners für den Scheduler-Test abschalten --
    das Ziel ist die Tenant-Schleife, nicht der fachliche Sync (siehe
    `test_scheduler_tenant_scope.py` für die gleiche Begründung)."""

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


async def test_scheduler_creates_one_run_per_active_tenant(
    migrated_engine: AsyncEngine,
    two_active_tenants: _TwoTenants,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erweiterung von `test_scheduler_tenant_scope.py` auf ECHTE Mehrmandantenschleife:
    mit zwei zusätzlichen aktiven Kunden (neben dem immer vorhandenen Default-Tenant)
    erzeugt ein einziger `trigger_now()`-Aufruf GENAU einen Lauf PRO aktivem Tenant,
    korrekt gestempelt -- kein gemeinsamer, kein fehlender, kein vertauschter Lauf."""
    dtid = await _real_default_tenant_id(migrated_engine)
    a, b = two_active_tenants["a"], two_active_tenants["b"]
    _patch_heavy_run_dependencies(monkeypatch)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    before = dt.datetime.now(dt.UTC)
    await service.trigger_now(dry_run_override=True)

    async with migrated_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, tenant_id FROM run WHERE started_at >= :ts"), {"ts": before}
            )
        ).all()
    try:
        tenant_ids = [int(r.tenant_id) for r in rows]
        for expected in (dtid, a, b):
            assert tenant_ids.count(expected) == 1, (
                f"Erwartet genau einen Lauf für Tenant {expected}, sah {tenant_ids}"
            )
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(
                text("DELETE FROM run WHERE id = ANY(:ids)"), {"ids": [int(r.id) for r in rows]}
            )
            await conn.commit()
