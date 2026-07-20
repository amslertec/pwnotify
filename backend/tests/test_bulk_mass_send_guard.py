"""H2: `/users/bulk` must not bypass the mass-send brake.

Two independent guards are proven here:

1. **Schema cap** — `BulkRequest` now hard-limits the payload (`ids`: 1..2000,
   `action`: `exclude|include|notify`). Pydantic rejects an oversized or unknown
   request with a validation error *before* any mail logic runs. Against the old
   untyped schema (`ids: list[int]`, `action: str`) these constructions succeeded,
   so the tests are non-vacuously red on the pre-fix code.

2. **Absolute brake** — the bulk-notify branch now runs the same absolute ceiling
   (`schedule.max_notify_count`) that `runner.execute_run` enforces. A request for
   more accounts than the cap is refused with a `PwNotifyError` and NO mail is sent.
   Old code looped over every id and dispatched a send per account, so the send
   counter would be non-zero -- the assertion `sent_calls == 0` proves the guard.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app.api.routes import users as users_route
from app.api.routes.users import BulkRequest, bulk
from app.core.errors import PwNotifyError
from app.models.user import AppUser
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession


# --- (1) schema cap: pure pydantic, no DB needed -------------------------------- #
def test_bulk_request_rejects_more_than_2000_ids() -> None:
    with pytest.raises(PydanticValidationError):
        BulkRequest(ids=list(range(2001)), action="notify")


def test_bulk_request_accepts_exactly_2000_ids() -> None:
    # Boundary: 2000 is the documented convention (see schemas/assignment.py) and must pass.
    req = BulkRequest(ids=list(range(2000)), action="notify")
    assert len(req.ids) == 2000


def test_bulk_request_rejects_empty_ids() -> None:
    with pytest.raises(PydanticValidationError):
        BulkRequest(ids=[], action="notify")


def test_bulk_request_rejects_unknown_action() -> None:
    with pytest.raises(PydanticValidationError):
        BulkRequest(ids=[1], action="delete")


# --- (2) absolute brake in the bulk-notify branch ------------------------------- #
async def test_bulk_notify_over_absolute_cap_blocks_and_sends_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`schedule.max_notify_count` = 3, bulk-notify for 5 accounts -> refused, zero sends.

    `notify_user` is monkeypatched to a counter: on the pre-fix code it would fire once per
    id (5 sends); the guard must keep it at 0. `entra_repo.get` is faked so the pre-fix loop
    would actually reach the (counted) send -- otherwise the red would be vacuous.
    """
    sent_calls: list[int] = []

    async def _fake_notify(*_a: Any, **_k: Any) -> SimpleNamespace:
        sent_calls.append(1)
        return SimpleNamespace(action="sent", recipient="x")

    async def _fake_get(_session: Any, uid: int) -> SimpleNamespace:
        return SimpleNamespace(id=uid, upn=f"u{uid}@example.test")

    monkeypatch.setattr(users_route, "notify_user", _fake_notify)
    monkeypatch.setattr(users_route, "build_sender", lambda _settings: object())
    monkeypatch.setattr(users_route.entra_repo, "get", _fake_get)

    # Duck-typed settings service: only `get_all` is consulted by the notify branch.
    svc = SimpleNamespace(
        get_all=lambda: _coro({"schedule.max_notify_count": 3, "schedule.reminder_days": [7]})
    )

    admin = AppUser(username="h2-admin", password_hash="x", role="admin")
    session.add(admin)
    await session.flush()

    body = BulkRequest(ids=[1, 2, 3, 4, 5], action="notify")

    with pytest.raises(PwNotifyError) as ei:
        await bulk(None, admin, body, session, svc)  # type: ignore[arg-type]

    # The refusal message must carry the offending cap so the operator can act.
    assert "3" in ei.value.message
    assert sent_calls == [], "Mass-send brake was bypassed -- notify_user should never run"


async def test_bulk_notify_under_absolute_cap_still_sends(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: a payload within the cap must still dispatch normally -- the brake
    only blocks the over-the-ceiling case, it does not break legitimate bulk reminders."""
    sent_calls: list[int] = []

    async def _fake_notify(*_a: Any, **_k: Any) -> SimpleNamespace:
        sent_calls.append(1)
        return SimpleNamespace(action="sent", recipient="x")

    async def _fake_get(_session: Any, uid: int) -> SimpleNamespace:
        return SimpleNamespace(id=uid, upn=f"u{uid}@example.test")

    monkeypatch.setattr(users_route, "notify_user", _fake_notify)
    monkeypatch.setattr(users_route, "build_sender", lambda _settings: object())
    monkeypatch.setattr(users_route.entra_repo, "get", _fake_get)

    svc = SimpleNamespace(
        get_all=lambda: _coro({"schedule.max_notify_count": 100, "schedule.reminder_days": [7]})
    )

    admin = AppUser(username="h2-admin-ok", password_hash="x", role="admin")
    session.add(admin)
    await session.flush()

    body = BulkRequest(ids=[1, 2, 3], action="notify")
    result = await bulk(None, admin, body, session, svc)  # type: ignore[arg-type]

    assert sent_calls == [1, 1, 1]
    assert "3" in result.message


async def _coro(value: Any) -> Any:
    return value
