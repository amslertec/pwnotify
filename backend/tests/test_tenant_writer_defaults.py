"""Reale Writer gegen den server_default-Bridge aus Fix 1/2 (Phase-1-Review).

Diese Tests treiben echte DB-Writes über die Produktionspfade (run_repo, SettingsService),
ohne tenant_id explizit zu setzen. Sie decken genau die Lücke ab, die der Review fand:
bestehende Writer setzen tenant_id nicht, also muss der server_default greifen.
"""

from __future__ import annotations

from app.repositories import exclusion_repo, run_repo
from app.services.settings_service import SettingsService
from sqlalchemy import text


async def test_run_repo_create_falls_back_to_default_tenant(session):
    default_tenant_id = (
        await session.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))
    ).scalar_one()

    run = await run_repo.create(session, trigger="manual", dry_run=False)

    assert run.tenant_id == default_tenant_id


async def test_exclusion_repo_add_falls_back_to_default_tenant(session):
    default_tenant_id = (
        await session.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))
    ).scalar_one()

    exclusion = await exclusion_repo.add(
        session, kind="user", value="alice@example.com", label="test"
    )

    assert exclusion.tenant_id == default_tenant_id


async def test_settings_upsert_updates_existing_row_on_composite_pk(session):
    service = SettingsService(session)

    await service.set("app.public_url", "https://first.example")
    await service.set("app.public_url", "https://second.example")

    value = await service.get("app.public_url")
    assert value == "https://second.example"

    rows = (
        await session.execute(text("SELECT count(*) FROM setting WHERE key = 'app.public_url'"))
    ).scalar_one()
    assert rows == 1
