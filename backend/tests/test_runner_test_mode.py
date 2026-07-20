"""Feature (sync test mode): `execute_run` threads `sync.test_mode` into the notification
filter as `include_inactive`.

When `sync.test_mode` is on, the run's candidate set (which is ALSO the due-estimate set --
the mass-send guard must reason over the exact same list) is fetched with
`include_inactive=True`, so disabled + unlicensed accounts get real reminder mails. When off,
the flag is `False` and behavior is unchanged.

The test drives a real run via `SchedulerService.trigger_now` on the default tenant (mirrors
`test_runner_sync_guard.py`) but replaces the settings source and the notification query with
recorders, so it asserts exactly the flag that flows from setting to repo call -- and that
every returned candidate is handed to `notify_user`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app.db.tenant_context import open_active_session
from app.services import runner
from app.services.scheduler import SchedulerService
from app.services.settings_schema import default_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


class _FakeSender:
    client = None


def _patch_run_environment(
    monkeypatch: pytest.MonkeyPatch, *, test_mode: bool
) -> tuple[dict[str, Any], list[str]]:
    """Neutralize every network/mail side-step and inject settings with `sync.test_mode`.

    Returns the recorder dict (captures the `include_inactive` kwarg passed to the repo) and
    the list of UPNs handed to `notify_user`.
    """
    settings = default_settings()
    settings["sync.test_mode"] = test_mode  # graph.* stay empty -> sync skips cleanly

    class _FakeSettings:
        def __init__(self, _session: Any) -> None:
            pass

        async def get_all(self) -> dict[str, Any]:
            return settings

    captured: dict[str, Any] = {}
    notified: list[str] = []

    async def _fake_iter(_session: Any, *, include_inactive: bool = False) -> list[Any]:
        captured["include_inactive"] = include_inactive
        # days_left in reminder_days -> counts toward the due estimate as well.
        return [SimpleNamespace(upn="a@x", days_left=1), SimpleNamespace(upn="b@x", days_left=1)]

    async def _fake_notify(_session: Any, user: Any, **_kw: Any) -> Any:
        notified.append(user.upn)
        return SimpleNamespace(
            action="dry_run", stage=1, recipient=user.upn, channel="mail", error=None
        )

    async def _fake_sso_sync(*_a: Any, **_k: Any) -> dict[str, int]:
        return {"synced": 0, "removed": 0}

    async def _no_excluded(*_a: Any, **_k: Any) -> set[str]:
        return set()

    async def _no_alert(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(runner, "SettingsService", _FakeSettings)
    monkeypatch.setattr(runner.entra_repo, "iter_active_for_notification", _fake_iter)
    monkeypatch.setattr(runner, "notify_user", _fake_notify)
    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sso_sync)
    monkeypatch.setattr(runner, "_resolve_excluded_ids", _no_excluded)
    monkeypatch.setattr(runner, "build_sender", lambda _settings: _FakeSender())
    monkeypatch.setattr("app.services.alerts.maybe_send_run_alert", _no_alert)
    return captured, notified


async def _run_and_cleanup(migrated_engine: AsyncEngine) -> Any:
    service = SchedulerService(open_active_session, base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)
    async with migrated_engine.connect() as conn:
        await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
        await conn.commit()
    return run


async def test_test_mode_on_includes_inactive_in_send_set(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _real_default_tenant_id(migrated_engine)
    captured, notified = _patch_run_environment(monkeypatch, test_mode=True)

    await _run_and_cleanup(migrated_engine)

    assert captured["include_inactive"] is True
    assert notified == ["a@x", "b@x"]


async def test_test_mode_off_keeps_default_filter(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _real_default_tenant_id(migrated_engine)
    captured, notified = _patch_run_environment(monkeypatch, test_mode=False)

    await _run_and_cleanup(migrated_engine)

    assert captured["include_inactive"] is False
    assert notified == ["a@x", "b@x"]
