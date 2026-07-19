"""TDD für Task 1 der Tenant-Refinements: `sync_users` darf bei fehlender
Graph-Konfiguration KEINEN MSAL-Token-Versuch mehr unternehmen.

Bug: `sync_users` baute den `GraphClient` (und damit die MSAL-Authority) IMMER, auch mit
leerem `graph.tenant_id`. Die Authority landete dann ohne Mandanten-Segment
(``.../login.microsoftonline.com/``), MSAL warf einen rohen, englischen Fehler, und
`execute_run` verbuchte den ALS `status="partial"` + `run.error` UND als
`{"step":"sync","error":...}` im `detail_log` -- doppelt und auf Englisch in der
Runs-UI.

Fix: `is_graph_configured(settings)` (Mandant + Client + Secret, alle nicht-leer nach
`strip()`) ist die erste Prüfung in `sync_users` -- ohne sie wird gar kein `GraphClient`
gebaut. `execute_run` verbucht den Skip-Fall als harmlosen `detail_log`-Eintrag
(`{"step":"sync","skipped":"graph_not_configured"}`), OHNE `status="partial"`/`error` zu
setzen -- die Deduplizierung findet dort statt, nicht in der UI (Task 2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.db.tenant_context import open_active_session
from app.services.graph import sync as graph_sync
from app.services.graph.sync import is_graph_configured, sync_users
from app.services.scheduler import SchedulerService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def _unconfigured_settings(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "graph.tenant_id": "",
        "graph.client_id": "",
        "graph.client_secret": "",
        "graph.cloud": "global",
        "policy.auto_detect": True,
        "policy.validity_days_override": None,
        "sync.shared_patterns": [],
        "sync.shared_detect_unlicensed": True,
        "sync.group_id": "",
    }
    base.update(overrides)
    return base


def _boom_if_constructed(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("GraphClient wurde trotz fehlender Konfiguration gebaut")


# ---- is_graph_configured: reine Bool-Logik -------------------------------------------- #


def test_is_graph_configured_true_when_all_three_present() -> None:
    assert is_graph_configured(
        {"graph.tenant_id": "t", "graph.client_id": "c", "graph.client_secret": "s"}
    )


@pytest.mark.parametrize("missing", ["graph.tenant_id", "graph.client_id", "graph.client_secret"])
def test_is_graph_configured_false_when_one_missing(missing: str) -> None:
    settings = {"graph.tenant_id": "t", "graph.client_id": "c", "graph.client_secret": "s"}
    settings[missing] = ""
    assert not is_graph_configured(settings)


def test_is_graph_configured_false_when_whitespace_only() -> None:
    assert not is_graph_configured(
        {"graph.tenant_id": "   ", "graph.client_id": "c", "graph.client_secret": "s"}
    )


def test_is_graph_configured_false_when_all_missing() -> None:
    assert not is_graph_configured({})


# ---- sync_users: unkonfiguriert -> Skip, KEIN GraphClient, KEIN Token-Versuch --------- #


async def test_sync_users_skips_without_building_graph_client(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph_sync, "GraphClient", _boom_if_constructed)

    result = await sync_users(session, _unconfigured_settings())

    assert result == {"checked": 0, "skipped": "graph_not_configured"}


# ---- sync_users: konfiguriert -> Guard ist No-Op, bestehender Sync-Pfad unverändert --- #


class _FakeGraph:
    def __init__(self, config: Any) -> None:
        self.config = config

    async def get_password_validity_map(self) -> tuple[int | None, dict[str, int]]:
        return 90, {}

    async def iter_users(self) -> AsyncIterator[dict[str, Any]]:
        yield {
            "id": "guard-configured-1",
            "userPrincipalName": "guard-configured@example.com",
            "mail": "guard-configured@example.com",
            "accountEnabled": True,
            "assignedLicenses": [{"skuId": "x"}],
        }


async def test_sync_users_configured_guard_is_noop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    upserted: list[dict[str, Any]] = []

    async def _fake_upsert(_session: AsyncSession, data: dict[str, Any]) -> None:
        upserted.append(data)

    monkeypatch.setattr(graph_sync, "GraphClient", _FakeGraph)
    monkeypatch.setattr(graph_sync.entra_repo, "upsert", _fake_upsert)
    settings = _unconfigured_settings(
        **{
            "graph.tenant_id": "tid",
            "graph.client_id": "cid",
            "graph.client_secret": "secret",
        }
    )

    result = await sync_users(session, settings)

    assert result == {"checked": 1}
    assert "skipped" not in result
    assert len(upserted) == 1
    assert upserted[0]["upn"] == "guard-configured@example.com"


# ---- execute_run: unkonfiguriert -> success, kein doppelter Fehler ------------------- #


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


class _FakeSender:
    """Ersetzt den echten Mail-Sender -- `mail.backend` ist standardmässig `"graph"`,
    `build_sender` baute (unabhängig vom hier getesteten Sync-Guard, vorbestehendes
    Verhalten) bislang ebenfalls unbedingt einen `GraphClient`. Das ist NICHT Gegenstand
    dieses Tasks (nur der Sync-Schritt); ein Fake hält den Test auf den Sync-Guard
    fokussiert, ohne über einen zweiten, unabhängigen Pfad ohnehin ins MSAL zu laufen."""

    client = None


def _patch_everything_but_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Netzwerk-/Mail-lastige Nebenschritte abschalten (gleiches Muster wie
    `test_scheduler_tenant_scope.py`/`test_runtime_isolation.py`) -- das Ziel dieses Tests
    ist der Sync-Guard, nicht SSO-Abgleich, Ausschlüsse, Mail-Versand oder der Admin-Alert."""

    async def _fake_sso_sync(
        session: Any, settings: dict[str, Any], *, tenant_id: int
    ) -> dict[str, int]:
        return {"synced": 0, "removed": 0}

    async def _no_excluded(session: Any, settings: dict[str, Any]) -> set[str]:
        return set()

    async def _no_users(session: Any) -> list[Any]:
        return []

    async def _no_alert(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sso_sync)
    monkeypatch.setattr("app.services.runner._resolve_excluded_ids", _no_excluded)
    monkeypatch.setattr("app.services.runner.entra_repo.iter_active_for_notification", _no_users)
    monkeypatch.setattr("app.services.runner.build_sender", lambda settings: _FakeSender())
    monkeypatch.setattr("app.services.alerts.maybe_send_run_alert", _no_alert)


async def test_execute_run_skips_sync_cleanly_when_graph_unconfigured(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    dtid = await _real_default_tenant_id(migrated_engine)
    monkeypatch.setattr(graph_sync, "GraphClient", _boom_if_constructed)
    _patch_everything_but_sync(monkeypatch)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    try:
        assert run.tenant_id == dtid
        assert run.status == "success"
        assert run.error is None
        assert run.detail_log == [{"step": "sync", "skipped": "graph_not_configured"}], (
            f"Skip muss GENAU einmal, benign, ohne 'error'-Key auftauchen: {run.detail_log}"
        )
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
            await conn.commit()


async def test_resolve_excluded_ids_skips_graph_when_unconfigured(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_resolve_excluded_ids` baute den GraphClient bislang bei JEDER Gruppen-Ausschlussregel
    -- auch ohne Graph-Konfiguration, sodass die MSAL-Authority-Validierung (leere
    `graph.tenant_id`) den rohen Fehler in den Run leakte (die zweite Quelle neben `sync_users`).
    Jetzt gilt derselbe `is_graph_configured`-Guard: ohne Graph wird KEIN Client gebaut und es
    bleiben nur die User-Wert-Ausschlüsse. Non-vakuös -- ohne Guard löst `_boom_if_constructed`
    aus."""
    from app.services import runner

    async def _user_values(_session: Any) -> list[str]:
        return ["excluded-1", "excluded-2"]

    async def _group_ids(_session: Any) -> list[str]:
        return ["group-a"]  # nicht-leer: ohne Guard würde der GraphClient gebaut

    monkeypatch.setattr(runner.exclusion_repo, "user_values", _user_values)
    monkeypatch.setattr(runner.exclusion_repo, "group_ids", _group_ids)
    monkeypatch.setattr(runner, "GraphClient", _boom_if_constructed)

    excluded = await runner._resolve_excluded_ids(session, _unconfigured_settings())

    assert excluded == {"excluded-1", "excluded-2"}


async def test_resolve_excluded_ids_uses_graph_when_configured(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gegenprobe: mit konfiguriertem Graph wird der Client gebaut und die Gruppenmitglieder
    fliessen in die Ausschlussmenge -- der Guard ist bei konfiguriertem Graph ein No-op."""
    from app.services import runner

    async def _user_values(_session: Any) -> list[str]:
        return ["excluded-1"]

    async def _group_ids(_session: Any) -> list[str]:
        return ["group-a"]

    class _FakeExclGraph:
        def __init__(self, _config: Any) -> None:
            pass

        async def get_group_member_ids(self, _gid: str) -> set[str]:
            return {"member-x"}

    monkeypatch.setattr(runner.exclusion_repo, "user_values", _user_values)
    monkeypatch.setattr(runner.exclusion_repo, "group_ids", _group_ids)
    monkeypatch.setattr(runner, "GraphClient", _FakeExclGraph)

    excluded = await runner._resolve_excluded_ids(
        session,
        {
            "graph.tenant_id": "t",
            "graph.client_id": "c",
            "graph.client_secret": "s",
        },
    )

    assert excluded == {"excluded-1", "member-x"}
