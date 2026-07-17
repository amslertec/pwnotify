"""TDD für Task 10 (Superadmin-Einladungs-Parität): `create_superadmin`
(`api/routes/admin_users.py`) bekommt denselben Einladungsmodus wie `create_local` (Task 5,
s. `test_invitation_flow.py`) -- ein `pending:`-Platzhalterkonto mit `role='superadmin'`,
das über den ROLLENAGNOSTISCHEN Accept-Pfad (`public_tokens.accept_token`) aktiviert wird,
OHNE dass dieser Pfad je angefasst werden musste (er schreibt `target.role` nie).

Muster wie `test_invitation_flow.py`: treibt die Route-Funktion direkt an (savepoint-
isolierte `session`-Fixture, echtes Postgres), faked den Mail-Versand über
`services.user_token.build_sender` (`fake_sender`-Fixture unten, identisch zu
`test_invitation_flow.py`)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import default_tenant_id, limiter
from app.api.routes.admin_users import create_superadmin
from app.api.routes.public_tokens import accept_token
from app.core.errors import ForbiddenError
from app.core.security import hash_token
from app.models._base import utcnow
from app.models.token import UserToken
from app.models.user import AppUser
from app.schemas.auth import SuperadminCreate, TokenAccept
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeSender:
    backend = "fake"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str | None = None,
        inline_images: list[Any] | None = None,
    ) -> None:
        self.sent.append({"to": to, "subject": subject, "html": html_body, "text": text_body})


@pytest_asyncio.fixture
async def fake_sender(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[_FakeSender]:
    import app.services.user_token as user_token_service

    sender = _FakeSender()
    monkeypatch.setattr(user_token_service, "build_sender", lambda _settings: sender)
    yield sender


def _extract_token(text_: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_-]+)", text_)
    assert match is not None, f"kein Token in der Mail gefunden: {text_!r}"
    return match.group(1)


async def _mk_superadmin(session: AsyncSession) -> AppUser:
    u = AppUser(
        username=f"ti10-superadmin-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="superadmin",
        is_sso=False,
    )
    session.add(u)
    await session.flush()
    return u


async def _token_row(session: AsyncSession, user_id: int, purpose: str) -> UserToken:
    row = (
        await session.execute(
            select(UserToken).where(UserToken.app_user_id == user_id, UserToken.purpose == purpose)
        )
    ).scalar_one()
    return row


# ---- Einladungsmodus -------------------------------------------------------------------- #


async def test_superadmin_invite_creates_pending_inactive_home_default_and_sends_mail(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    caller = await _mk_superadmin(session)
    email = f"invited-{uuid.uuid4().hex[:8]}@ti10.test"
    body = SuperadminCreate(email=email)

    out = await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None
    assert persisted.username.startswith("pending:")
    assert persisted.is_active is False
    assert persisted.role == "superadmin"
    assert persisted.email == email
    assert persisted.tenant_id == await default_tenant_id(session)

    token_row = await _token_row(session, out.id, "invite")  # type: ignore[arg-type]
    assert token_row.consumed_at is None
    assert token_row.expires_at > utcnow()

    assert len(fake_sender.sent) == 1
    msg = fake_sender.sent[0]
    assert msg["to"] == [email]
    raw = _extract_token(msg["html"])
    assert hash_token(raw) == token_row.token_hash


async def test_superadmin_invite_accept_activates_as_superadmin_with_chosen_username(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    caller = await _mk_superadmin(session)
    email = f"accept-{uuid.uuid4().hex[:8]}@ti10.test"
    out = await create_superadmin(  # type: ignore[arg-type]
        None, caller, SuperadminCreate(email=email), session
    )
    raw = _extract_token(fake_sender.sent[-1]["html"])
    new_username = f"real-superadmin-{uuid.uuid4().hex[:8]}"

    msg = await accept_token(  # type: ignore[arg-type]
        None,
        TokenAccept(
            token=raw,
            first_name="Alice",
            last_name="Admin",
            username=new_username,
            password="Str0ng!Passw0rd",
        ),
        session,
    )
    assert msg.message

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None
    assert persisted.is_active is True
    assert persisted.username == new_username
    assert persisted.role == "superadmin"  # Accept-Pfad ist rollenagnostisch -- unverändert

    row = await _token_row(session, out.id, "invite")  # type: ignore[arg-type]
    assert row.consumed_at is not None


async def test_superadmin_invite_requires_email(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    caller = await _mk_superadmin(session)
    body = SuperadminCreate()  # weder username/password noch email

    with pytest.raises(ForbiddenError) as exc_info:
        await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "email_required"
    assert fake_sender.sent == []


# ---- Direktmodus (unverändert) ------------------------------------------------------------ #


async def test_superadmin_direct_mode_requires_username(session: AsyncSession) -> None:
    caller = await _mk_superadmin(session)
    body = SuperadminCreate(password="a-strong-password-1")  # kein username

    with pytest.raises(ForbiddenError) as exc_info:
        await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "username_required"


async def test_superadmin_direct_mode_unchanged(session: AsyncSession) -> None:
    """Regressionsschutz: Direktpfad bleibt wie bisher -- aktiv, kein Token, keine Mail
    nötig (kein `fake_sender`-Fixture -- ein echter Versand würde ohne Monkeypatch
    fehlschlagen und den Test verraten, falls hier fälschlich ein Invite-Zweig liefe)."""
    caller = await _mk_superadmin(session)
    username = f"direct-superadmin-{uuid.uuid4().hex[:8]}"
    body = SuperadminCreate(username=username, password="a-strong-password-1")

    out = await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None
    assert persisted.username == username
    assert persisted.is_active is True
    assert persisted.role == "superadmin"
    assert persisted.email is None

    existing = (
        await session.execute(select(UserToken).where(UserToken.app_user_id == out.id))
    ).scalar_one_or_none()
    assert existing is None


async def test_superadmin_create_still_rejects_sso() -> None:
    body = SuperadminCreate(username="xyz", password="a-strong-password-1", is_sso=True)
    assert body.is_sso is True  # Route lehnt dies hart ab (unveränderter Guard, s. Route)


# Der Nicht-Superadmin-/Kunden-Kontext-Guard selbst (`SuperadminDefaultContextUser` ->
# `require_superadmin_default_context`) bleibt unverändert (dieser Task fasst ihn nicht an)
# und ist bereits durch `test_matrix_b_route_gating.py` abgedeckt (u. a. `create_superadmin`
# im Default- vs. Kunden-Kontext).
