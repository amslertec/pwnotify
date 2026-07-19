"""TDD für Task 8 (Access-Modell/Superadmin-Design §9): das First-Time-Setup legt das erste
Konto als SUPERADMIN an (nicht mehr den alten Drei-Wege-`admin`) und kann optional den vom
Phase-1-Migration bereits angelegten Default-Tenant (Slug `default`) auf den Firmennamen
umbenennen -- Setup legt ihn NICHT neu an. Multi-Tenant-Mode bleibt danach AUS.

Nutzt die gewöhnliche `session`-Fixture (savepoint-isoliert, siehe conftest.py) -- die Test-DB
enthält beim Start jedes Tests garantiert keinen Admin (jeder vorherige Test rollt seine
Benutzerzeilen zurück), das ist also bereits der geforderte "fresh DB / no admin"-Zustand,
ohne die DB extra leeren zu müssen. `create_admin` wird direkt aufgerufen (wie in
`test_switch_tenant.py`/`test_active_tenant_resolution.py`), mit einem Duck-typed
Request/Response-Paar."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.deps import limiter
from app.api.routes.setup import AdminCreate, create_admin
from app.core.errors import ConflictError
from app.repositories import tenant_repo
from app.services import instance_settings
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeRequest:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client: object | None = None
        self.cookies: dict[str, str] = {}


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value


async def test_setup_creates_superadmin_and_renames_default_tenant_mode_stays_off(
    session: AsyncSession,
) -> None:
    body = AdminCreate(
        username="ti-first-setup",
        password="Str0ng!Passw0rd1",
        default_tenant_name="Acme AG",
    )
    response = _FakeResponse()
    request = _FakeRequest()

    out = await create_admin(body, response, request, session)  # type: ignore[arg-type]

    assert out.role == "superadmin"
    assert out.is_sso is False

    default = await tenant_repo.default_tenant(session)
    assert default.slug == "default"
    assert default.name == "Acme AG"

    # Multi-Tenant-Mode bleibt AUS -- Setup schaltet ihn nie ein (Design §9.4).
    assert (await instance_settings.read_mode(session)) is False

    # Auto-Login: Refresh-Cookie wurde gesetzt.
    assert response.cookie_values


async def test_setup_without_tenant_name_keeps_default_name(session: AsyncSession) -> None:
    default_before = await tenant_repo.default_tenant(session)
    original_name = default_before.name

    body = AdminCreate(username="ti-first-setup-2", password="Str0ng!Passw0rd1")
    response = _FakeResponse()
    request = _FakeRequest()

    out = await create_admin(body, response, request, session)  # type: ignore[arg-type]
    assert out.role == "superadmin"

    default_after = await tenant_repo.default_tenant(session)
    assert default_after.name == original_name


async def test_second_setup_call_still_conflicts(session: AsyncSession) -> None:
    body = AdminCreate(username="ti-first-setup-3", password="Str0ng!Passw0rd1")
    response = _FakeResponse()
    request = _FakeRequest()
    await create_admin(body, response, request, session)  # type: ignore[arg-type]

    body2 = AdminCreate(username="ti-second-setup", password="An0ther!Passw0rd2")
    with pytest.raises(ConflictError) as exc_info:
        await create_admin(body2, _FakeResponse(), _FakeRequest(), session)  # type: ignore[arg-type]
    assert exc_info.value.code == "admin_exists"
