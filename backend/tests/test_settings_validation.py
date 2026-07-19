"""Foundation: SettingSpec value validation wired into SettingsService.set_many.

A registered key may carry a ``validate`` callable. On write, set_many runs it before
persisting: an invalid value is rejected with HTTP 400 (ValidationError), a valid value is
stored, and a registered key WITHOUT a validator keeps its previous free-form behaviour.
Validation happens before any row is written, so a rejected key leaves the batch unpersisted.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.core.errors import ValidationError
from app.db.tenant_context import tenant_scoped_session
from app.services.settings_schema import SETTINGS, SettingSpec
from app.services.settings_service import SettingsService
from app.services.settings_validators import number_range
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def temp_tenant(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Throwaway tenant on its own committed connection; setting rows + tenant removed in
    finally (FK-safe order: setting before tenant)."""
    async with migrated_engine.connect() as conn:
        tid = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) "
                    "VALUES ('SettingsValidation', 'settings-validation', true, now()) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await conn.commit()
        try:
            yield tid
        finally:
            await conn.execute(text("DELETE FROM setting WHERE tenant_id = :id"), {"id": tid})
            await conn.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tid})
            await conn.commit()


# --- pure validator ------------------------------------------------------------- #
def test_number_range_accepts_value_in_range() -> None:
    validate = number_range(min_value=0, max_value=10)
    assert validate(5) == 5


def test_number_range_rejects_out_of_range_with_400() -> None:
    validate = number_range(min_value=0, max_value=10)
    with pytest.raises(ValidationError) as ei:
        validate(99)
    assert ei.value.status_code == 400


def test_number_range_rejects_non_numeric() -> None:
    validate = number_range(min_value=0)
    with pytest.raises(ValidationError):
        validate("not-a-number")


def test_number_range_integer_only_rejects_fraction() -> None:
    validate = number_range(min_value=0, integer_only=True)
    with pytest.raises(ValidationError):
        validate(3.5)


def test_number_range_exclusive_min_rejects_boundary() -> None:
    validate = number_range(min_value=0, exclusive_min=True)
    with pytest.raises(ValidationError):
        validate(0)


# --- set_many wiring ------------------------------------------------------------ #
async def test_set_many_rejects_invalid_value(
    temp_tenant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        SETTINGS, "test.probe", SettingSpec(0, validate=number_range(min_value=0, max_value=10))
    )
    async with tenant_scoped_session(temp_tenant) as s:
        svc = SettingsService(s)
        with pytest.raises(ValidationError) as ei:
            await svc.set_many({"test.probe": 99})
        assert ei.value.status_code == 400
        assert await svc.get("test.probe") == 0  # nothing persisted for the rejected batch


async def test_set_many_stores_valid_value(
    temp_tenant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        SETTINGS, "test.probe", SettingSpec(0, validate=number_range(min_value=0, max_value=10))
    )
    async with tenant_scoped_session(temp_tenant) as s:
        svc = SettingsService(s)
        await svc.set_many({"test.probe": 7})
        assert await svc.get("test.probe") == 7


async def test_set_many_registered_key_without_validator_still_writable(
    temp_tenant: int,
) -> None:
    async with tenant_scoped_session(temp_tenant) as s:
        svc = SettingsService(s)
        await svc.set_many({"branding.app_name": "Acme"})
        assert await svc.get("branding.app_name") == "Acme"
