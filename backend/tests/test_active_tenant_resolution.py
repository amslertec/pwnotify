"""Angriffs-/Autorisierungstest für Phase 4a Task 3: `get_tenant_session` löst den aktiven
Tenant aus dem `active_tenant`-Claim des Access-Tokens auf -- statt wie bisher (Phase 3)
immer den Default-Tenant zu liefern -- und autorisiert ihn IMMER über `tenant_repo.is_allowed`
(Task 2), bevor eine tenant-gescopte Session geöffnet wird.

Kern-Sicherheitszusicherung (siehe Task-2-Review, Checkliste in der Task-3-Beschreibung):
ein Claim auf einen FREMDEN Tenant oder auf einen zwischenzeitlich DEAKTIVIERTEN EIGENEN
Tenant führt zu 403 (`ForbiddenError`) -- nie zu einer stillschweigend leeren/falschen
Session. `resolve_initial_tenant` (Task 2) gated selbst NICHT auf `is_active` (siehe dessen
Docstring); `_complete_login` (dieser Task) muss das beim Login nachholen, sonst landet ein
inaktiver eigener Tenant im Token-Claim/`active_tenant_id`.

Seed-Pattern wie in `test_isolation_attack.py`/`test_route_tenant_scoping.py`: echte
Superuser-Connection auf `migrated_engine`, echt committet, Cleanup im `finally` --
`get_tenant_session` öffnet über `get_session_factory()` eine EIGENE Verbindung und sähe
uncommittete Daten der savepoint-isolierten `session`-Fixture nicht (siehe deren
Kommentar in conftest.py). Für `_complete_login` (Test 3) reicht die gewöhnliche
`session`-Fixture: dort läuft alles auf derselben Session, keine zweite Verbindung im Spiel.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, get_current_user, get_tenant_session
from app.api.routes.auth import _complete_login
from app.core.errors import ForbiddenError
from app.core.security import decode_token, issue_token_pair
from app.db.session import get_session_factory
from app.models.user import UserSession
from app.repositories import user_repo
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


class _FakeRequest:
    """Duck-typed Request -- `get_current_user`/`get_tenant_session` lesen nur `.cookies`."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


class _FakeLoginRequest:
    """Duck-typed Request für `_complete_login` (über `audit.record` hinweg)."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client: object | None = None
        self.cookies: dict[str, str] = {}


class _FakeResponse:
    """Duck-typed Response -- sammelt nur die gesetzten Cookies."""

    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value


@contextlib.asynccontextmanager
async def _tenant_session_for(uid: int, *, claim: int | None) -> AsyncGenerator[AsyncSession]:
    """Treibt `get_tenant_session` exakt wie FastAPI: echtes Access-Token für `uid` (optional
    mit `active_tenant`-Claim), eine Owner-Session für `get_current_user` + Autorisierung,
    dann die (versuchte) tenant-gescopte Session."""
    pair = issue_token_pair(str(uid), active_tenant=claim)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_tenant_session(request, user, owner)
        try:
            yield await anext(gen)
        finally:
            await gen.aclose()


class _Seed:
    a: int
    b_foreign: int
    c_inactive: int
    auditor_id: int
    sso_inactive_id: int


@pytest_asyncio.fixture
async def seed(migrated_engine: AsyncEngine) -> AsyncGenerator[_Seed]:
    """Zwei aktive Tenants (A erlaubt, B fremd) + ein inaktiver, an ein SSO-Konto gebundener
    Tenant C + ein lokaler Auditor (nur A zugewiesen) + ein SSO-Konto (an C gebunden,
    inzwischen deaktiviert) -- alles echt committet über eine Superuser-Connection."""
    async with migrated_engine.connect() as conn:
        a, b, c = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('AtrA','atr-a',true,now()), "
                        "('AtrBForeign','atr-b-foreign',true,now()), "
                        "('AtrCInactive','atr-c-inactive',false,now()) "
                        "RETURNING id"
                    )
                )
            )
            .scalars()
            .all()
        )
        auditor_id = (
            await conn.execute(
                text(
                    "INSERT INTO app_user "
                    "(username, password_hash, role, is_active, is_sso, "
                    "failed_login_count, language, created_at, updated_at) VALUES "
                    "('atr-auditor@local', 'x', 'auditor', true, false, 0, 'de', now(), now()) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO auditor_tenant (user_id, tenant_id) VALUES (:uid, :tid)"),
            {"uid": auditor_id, "tid": a},
        )
        sso_inactive_id = (
            await conn.execute(
                text(
                    "INSERT INTO app_user "
                    "(username, password_hash, role, is_active, is_sso, tenant_id, "
                    "failed_login_count, language, created_at, updated_at) VALUES "
                    "('atr-sso@c', 'x', 'admin', true, true, :tid, 0, 'de', now(), now()) "
                    "RETURNING id"
                ),
                {"tid": c},
            )
        ).scalar_one()
        await conn.commit()
        s = _Seed()
        s.a, s.b_foreign, s.c_inactive = int(a), int(b), int(c)
        s.auditor_id, s.sso_inactive_id = int(auditor_id), int(sso_inactive_id)
        try:
            yield s
        finally:
            await conn.execute(
                text("DELETE FROM user_session WHERE user_id IN (:u1, :u2)"),
                {"u1": s.auditor_id, "u2": s.sso_inactive_id},
            )
            await conn.execute(
                text("DELETE FROM app_user WHERE id IN (:u1, :u2)"),
                {"u1": s.auditor_id, "u2": s.sso_inactive_id},
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b, :c)"),
                {"a": s.a, "b": s.b_foreign, "c": s.c_inactive},
            )
            await conn.commit()


# ---- 1. Erlaubter Claim: Session scoped auf den Claim-Tenant ---------------------------- #


async def test_allowed_claim_scopes_session_to_claimed_tenant(seed: _Seed) -> None:
    """Der Auditor ist Tenant A zugewiesen und trägt genau diesen Claim -- die Session muss
    auf A gescoped sein (GUC == A)."""
    async with _tenant_session_for(seed.auditor_id, claim=seed.a) as session:
        guc = (
            await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()
        assert guc == str(seed.a), f"GUC zeigt nicht auf den erlaubten Claim-Tenant: {guc}"


# ---- 2. Verweigerter Claim: 403, nie ein stiller Fallback ------------------------------- #


async def test_foreign_tenant_claim_is_forbidden(seed: _Seed) -> None:
    """Der Auditor trägt einen Claim auf Tenant B, dem er NICHT zugewiesen ist -- muss 403
    auslösen, bevor überhaupt eine tenant-gescopte Session geöffnet wird. Das ist die
    Kern-Angriffszusicherung: ein gefälschter/veralteter Claim darf nie durchrutschen."""
    with pytest.raises(ForbiddenError):
        async with _tenant_session_for(seed.auditor_id, claim=seed.b_foreign):
            pass


async def test_deactivated_own_tenant_claim_is_forbidden(seed: _Seed) -> None:
    """Das SSO-Konto trägt einen Claim auf SEINEN EIGENEN Tenant C -- der aber zwischen-
    zeitlich deaktiviert wurde. `is_allowed` muss das ablehnen (403), kein Verfügbarkeits-
    Leck auf einen inaktiven Kunden nur weil es der eigene ist."""
    with pytest.raises(ForbiddenError):
        async with _tenant_session_for(seed.sso_inactive_id, claim=seed.c_inactive):
            pass


# ---- 3. `_complete_login`: inaktiver eigener Tenant wird beim Login ausgegatet ---------- #


async def test_complete_login_gates_out_inactive_bound_tenant(
    seed: _Seed, session: AsyncSession
) -> None:
    """Login (bzw. der gemeinsame Abschluss-Pfad `_complete_login`) für ein SSO-Konto, das
    an einen INZWISCHEN DEAKTIVIERTEN Tenant gebunden ist: `resolve_initial_tenant` liefert
    diesen Tenant ungefiltert (siehe dessen Docstring), `_complete_login` muss ihn aber über
    `is_allowed` aussieben -- weder im Token-Claim noch in `user_session.active_tenant_id`
    darf die inaktive Id landen."""
    user = await user_repo.get(session, seed.sso_inactive_id)
    assert user is not None

    request = _FakeLoginRequest()
    response = _FakeResponse()
    await _complete_login(request, response, session, user)  # type: ignore[arg-type]

    access_token = response.cookie_values[ACCESS_COOKIE]
    payload = decode_token(access_token, expected_type="access")
    assert payload.get("active_tenant") is None, (
        f"Inaktiver Tenant landete im Claim: {payload.get('active_tenant')}"
    )

    us = (
        await session.execute(
            select(UserSession).where(UserSession.user_id == seed.sso_inactive_id)
        )
    ).scalar_one()
    assert us.active_tenant_id is None, (
        f"Inaktiver Tenant landete in active_tenant_id: {us.active_tenant_id}"
    )
