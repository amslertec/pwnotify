"""`iter_active_for_notification` gains a test-mode switch (`include_inactive`).

Feature (sync test mode): when enabled, disabled (`account_enabled=false`) AND unlicensed
(`is_shared=true`) accounts become notification candidates too -- to exercise the real
send/expiry flow. The `excluded` and `expiry_date` filters MUST still apply either way
(an excluded account or one without an expiry date is never notified).

Seeded rows carry an explicit `tenant_id` (the `session` fixture sets no tenant context, so
the model's `current_tenant_or_none` default would yield a NULL and violate NOT NULL). The
repo query itself has no tenant predicate (RLS handles that at runtime); assertions filter by
a unique UPN prefix so pre-existing rows can't perturb the result.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest_asyncio
from app.models.entra import EntraUser
from app.repositories import entra_repo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PREFIX = f"tm-{uuid.uuid4().hex[:8]}"


def _upn(tag: str) -> str:
    return f"{_PREFIX}-{tag}@example.com"


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> dict[str, EntraUser]:
    tid = (await session.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=10)

    def _u(tag: str, **over: object) -> EntraUser:
        base: dict[str, object] = {
            "tenant_id": tid,
            "entra_id": f"{_PREFIX}-{tag}",
            "upn": _upn(tag),
            "account_enabled": True,
            "is_shared": False,
            "excluded": False,
            "expiry_date": future,
        }
        base.update(over)
        return EntraUser(**base)  # type: ignore[arg-type]

    rows = {
        "normal": _u("normal"),
        "disabled": _u("disabled", account_enabled=False),
        "shared": _u("shared", is_shared=True),
        "excluded": _u("excluded", excluded=True),
        "no_expiry": _u("no_expiry", expiry_date=None),
    }
    session.add_all(list(rows.values()))
    await session.flush()
    return rows


async def _upns(session: AsyncSession, **kwargs: bool) -> set[str]:
    users = await entra_repo.iter_active_for_notification(session, **kwargs)  # type: ignore[arg-type]
    return {u.upn for u in users if u.upn.startswith(_PREFIX)}


async def test_default_excludes_disabled_and_shared(
    session: AsyncSession, seeded: dict[str, EntraUser]
) -> None:
    # Default (test mode off): only the plain active, non-shared, non-excluded, has-expiry user.
    assert await _upns(session) == {_upn("normal")}


async def test_include_inactive_adds_disabled_and_shared(
    session: AsyncSession, seeded: dict[str, EntraUser]
) -> None:
    # Test mode on: disabled + unlicensed join in; excluded/no-expiry still filtered out.
    assert await _upns(session, include_inactive=True) == {
        _upn("normal"),
        _upn("disabled"),
        _upn("shared"),
    }
