"""TDD für Task 5 (Console+Groups+Invite): Passwort-Reset-Fluss -- der Admin-Trigger
(`POST /admin/users/{id}/reset`, `admin_users.send_reset`) + der öffentliche Reset-Endpunkt
(`POST /public/token/reset`, `api/routes/public_tokens.reset_token`).

Gleiches Muster wie `test_invitation_flow.py` (siehe dortigen Modul-Docstring für die
Begründung: savepoint-isolierte `session`-Fixture genügt, Mail-Versand gefaked via
`monkeypatch` auf `services.user_token.build_sender`, Rate-Limiter für die Dauer des Moduls
abgeschaltet, da `slowapi` sonst eine echte `Request`-Instanz verlangt)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import REFRESH_COOKIE, limiter
from app.api.routes.admin_users import delete_user, send_reset
from app.api.routes.auth import refresh as auth_refresh
from app.api.routes.public_tokens import reset_token
from app.core.errors import AuthError, ForbiddenError
from app.core.security import hash_token, issue_token_pair, verify_password
from app.models._base import utcnow
from app.models.tenant import AdminTenant, Tenant
from app.models.token import UserToken
from app.models.user import AppUser
from app.repositories import tenant_repo, user_repo
from app.schemas.auth import TokenReset
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


def _extract_token(text: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_-]+)", text)
    assert match is not None, f"kein Token in der Mail gefunden: {text!r}"
    return match.group(1)


def _slug() -> str:
    return f"pr5-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession) -> Tenant:
    return await tenant_repo.create(session, name="Pr5 Tenant", slug=_slug())


async def _mk_user(
    session: AsyncSession,
    *,
    role: str,
    is_sso: bool = False,
    tenant_id: int | None = None,
    email: str | None = None,
) -> AppUser:
    u = AppUser(
        username=f"pr5-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
        email=email,
    )
    session.add(u)
    await session.flush()
    return u


async def _token_row(session: AsyncSession, user_id: int, purpose: str) -> UserToken:
    return (
        await session.execute(
            select(UserToken).where(UserToken.app_user_id == user_id, UserToken.purpose == purpose)
        )
    ).scalar_one()


# ---- Admin-Trigger: POST /admin/users/{id}/reset ------------------------------------------ #


async def test_admin_reset_local_with_email_mints_token_and_sends_mail(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    email = f"target-{uuid.uuid4().hex[:8]}@pr5.test"
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email=email)
    assert target.id is not None

    msg = await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    assert msg.message

    row = await _token_row(session, target.id, "reset")
    assert row.consumed_at is None
    assert row.expires_at > utcnow()

    assert len(fake_sender.sent) == 1
    sent = fake_sender.sent[0]
    assert sent["to"] == [email]
    raw = _extract_token(sent["html"])
    assert hash_token(raw) == row.token_hash
    assert "/passwort-neu?token=" in sent["html"]


async def test_reset_rejected_for_sso_target(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(
        session, role="admin", is_sso=True, tenant_id=tenant.id, email="sso@pr5.test"
    )
    assert target.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "sso_no_reset"
    assert fake_sender.sent == []


async def test_reset_rejected_when_no_email(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email=None)
    assert target.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "email_required"
    assert fake_sender.sent == []


async def test_customer_admin_resetting_b_only_account_is_out_of_scope(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None

    local_admin_a = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=tenant_a.id))

    target_b = await _mk_user(session, role="admin", tenant_id=tenant_b.id, email="bonly@pr5.test")
    assert target_b.id is not None
    session.add(AdminTenant(user_id=target_b.id, tenant_id=tenant_b.id))
    await session.flush()

    with pytest.raises(ForbiddenError) as exc_info:
        await send_reset(None, local_admin_a, target_b.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "user_not_in_scope"
    assert fake_sender.sent == []


async def test_customer_admin_can_still_reset_own_tenant_user(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    local_admin_a = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=tenant_a.id))

    target_a = await _mk_user(session, role="admin", tenant_id=tenant_a.id, email="aonly@pr5.test")
    assert target_a.id is not None
    session.add(AdminTenant(user_id=target_a.id, tenant_id=tenant_a.id))
    await session.flush()

    msg = await send_reset(None, local_admin_a, target_a.id, session)  # type: ignore[arg-type]
    assert msg.message
    assert len(fake_sender.sent) == 1


async def test_reissuing_reset_invalidates_the_prior_live_token(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """§7c: ein neu ausgestelltes Reset-Token ersetzt idempotent ältere, noch gültige --
    ein zuvor verschicktes, ungenutztes Reset-Token darf nach einem erneuten Trigger nicht
    mehr funktionieren."""
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email="reissue@pr5.test")
    assert target.id is not None

    await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    first_raw = _extract_token(fake_sender.sent[-1]["html"])

    await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    second_raw = _extract_token(fake_sender.sent[-1]["html"])
    assert first_raw != second_raw

    with pytest.raises(ForbiddenError) as exc_info:
        await reset_token(  # type: ignore[arg-type]
            None, TokenReset(token=first_raw, password="NewStr0ng!Pass"), session
        )
    assert exc_info.value.code == "token_invalid"

    msg = await reset_token(  # type: ignore[arg-type]
        None, TokenReset(token=second_raw, password="NewStr0ng!Pass"), session
    )
    assert msg.message


# ---- Öffentlicher Endpunkt: POST /public/token/reset --------------------------------------- #


async def _mk_reset(
    session: AsyncSession, fake_sender: _FakeSender, *, admin: AppUser, target: AppUser
) -> str:
    assert target.id is not None
    await send_reset(None, admin, target.id, session)  # type: ignore[arg-type]
    return _extract_token(fake_sender.sent[-1]["html"])


async def test_public_reset_sets_password_then_reuse_and_expiry_fail(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email="reset-me@pr5.test")
    assert target.id is not None
    raw = await _mk_reset(session, fake_sender, admin=superadmin, target=target)

    msg = await reset_token(  # type: ignore[arg-type]
        None, TokenReset(token=raw, password="NewStr0ng!Pass"), session
    )
    assert msg.message

    refreshed = await session.get(AppUser, target.id)
    assert refreshed is not None
    assert verify_password("NewStr0ng!Pass", refreshed.password_hash)

    row = await _token_row(session, target.id, "reset")
    assert row.consumed_at is not None

    # Wiederverwendung desselben Tokens -> generischer Fehlschlag (Single-Use).
    with pytest.raises(ForbiddenError) as exc_info:
        await reset_token(  # type: ignore[arg-type]
            None, TokenReset(token=raw, password="AnotherStr0ng!Pass"), session
        )
    assert exc_info.value.code == "token_invalid"


async def test_expired_reset_token_is_rejected(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email="expired@pr5.test")
    assert target.id is not None
    raw = await _mk_reset(session, fake_sender, admin=superadmin, target=target)

    row = await _token_row(session, target.id, "reset")
    row.expires_at = utcnow() - dt.timedelta(hours=1)
    await session.flush()

    with pytest.raises(ForbiddenError) as exc_info:
        await reset_token(  # type: ignore[arg-type]
            None, TokenReset(token=raw, password="NewStr0ng!Pass"), session
        )
    assert exc_info.value.code == "token_invalid"


async def test_cross_purpose_invite_token_rejected_by_reset_endpoint(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """Ein `purpose='invite'`-Token darf am `/reset`-Endpunkt nicht funktionieren --
    `get_live_by_hash` filtert exakt auf den passenden `purpose`."""
    from app.api.routes.admin_users import create_local
    from app.schemas.auth import AdminUserCreate

    superadmin = await _mk_user(session, role="superadmin")
    email = f"invite-cross-{uuid.uuid4().hex[:8]}@pr5.test"
    out = await create_local(  # type: ignore[arg-type]
        None, superadmin, AdminUserCreate(email=email, role="admin"), session, None
    )
    assert out.id is not None
    invite_raw = _extract_token(fake_sender.sent[-1]["html"])

    with pytest.raises(ForbiddenError) as exc_info:
        await reset_token(  # type: ignore[arg-type]
            None, TokenReset(token=invite_raw, password="NewStr0ng!Pass"), session
        )
    assert exc_info.value.code == "token_invalid"


async def test_weak_password_rejected_by_reset_endpoint(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email="weak@pr5.test")
    assert target.id is not None
    raw = await _mk_reset(session, fake_sender, admin=superadmin, target=target)

    with pytest.raises(ForbiddenError) as exc_info:
        await reset_token(None, TokenReset(token=raw, password="weakpassword"), session)  # type: ignore[arg-type]
    assert exc_info.value.code == "password_policy"
    # Policy-Fehlschlag verbraucht das Token nicht -- ein erneuter Versuch mit einem
    # starken Passwort muss weiterhin funktionieren.
    row = await _token_row(session, target.id, "reset")
    assert row.consumed_at is None


# ---- H3: ein Reset widerruft alle bestehenden Sitzungen ------------------------------------ #


class _FakeRequest:
    """Duck-typed Request -- `refresh` liest nur `.cookies` (plus `.client`/Headers für die
    In-Place-Rotation, die hier aber nie erreicht wird)."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}
        self.deleted_cookies: set[str] = set()

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value

    def delete_cookie(self, name: str, **_: object) -> None:
        self.deleted_cookies.add(name)


async def test_public_reset_revokes_existing_sessions(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """H3: ein Passwort-Reset ist eine Recovery-Massnahme. Eine bereits bestehende (evtl. vom
    Angreifer gehaltene) Sitzung MUSS mit dem Reset sterben -- sonst rotiert ihr Refresh-Token
    ungestört weiter (bis zu `refresh_token_ttl_days`), obwohl das Passwort neu gesetzt wurde."""
    superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email="hijack@pr5.test")
    assert target.id is not None

    # Bestehende Sitzung des Kontos (wie nach einem Login): gültige user_session-Zeile.
    pair = issue_token_pair(str(target.id), generation=0)
    await user_repo.create_session(
        session,
        user_id=target.id,
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=None,
        ip=None,
    )
    assert len(await user_repo.list_sessions(session, target.id)) == 1

    raw = await _mk_reset(session, fake_sender, admin=superadmin, target=target)
    msg = await reset_token(  # type: ignore[arg-type]
        None, TokenReset(token=raw, password="NewStr0ng!Pass"), session
    )
    assert msg.message

    # (a) keine aktive Sitzung mehr -- die alte Zeile ist widerrufen.
    assert await user_repo.list_sessions(session, target.id) == []

    # (b) der ALTE Refresh-Token liefert an /auth/refresh 401 (session_invalid).
    req = _FakeRequest({REFRESH_COOKIE: pair.refresh_token})
    resp = _FakeResponse()
    with pytest.raises(AuthError) as exc_info:
        await auth_refresh(req, resp, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "session_invalid"


# ---- Carry-forward-Fix aus Task 1: `delete_user` eines Tokens-Erstellers ------------------ #


async def test_delete_user_who_created_tokens_succeeds_without_integrity_error(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """`user_token.created_by` hat KEIN `ON DELETE` -- ein Admin, der noch offene Tokens
    für ANDERE Konten ausgestellt hat (hier: ein per Reset-Trigger verschickter Link),
    muss trotzdem löschbar sein. Ohne den `user_token_repo.delete_created_by`-Aufruf in
    `delete_user` würde dies mit einem `IntegrityError` scheitern."""
    superadmin = await _mk_user(session, role="superadmin")
    another_superadmin = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    target = await _mk_user(session, role="admin", tenant_id=tenant.id, email="issued-for@pr5.test")
    assert target.id is not None and superadmin.id is not None

    # `superadmin` ist der ERSTELLER (`created_by`) eines noch gültigen Reset-Tokens für
    # ein ANDERES Konto (`target`) -- genau der Fall, den der Carry-forward-Fix abdeckt.
    await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    row = await _token_row(session, target.id, "reset")
    assert row.created_by == superadmin.id
    assert row.consumed_at is None

    msg = await delete_user(None, another_superadmin, superadmin.id, session)  # type: ignore[arg-type]
    assert msg.message

    assert await session.get(AppUser, superadmin.id) is None
    # Das Token selbst (für `target`, ein anderes Konto) ist mitgelöscht -- konsistent mit
    # der `created_by`-Kaskade, die es sonst als Waise zurückliesse.
    remaining = (
        await session.execute(select(UserToken).where(UserToken.id == row.id))
    ).scalar_one_or_none()
    assert remaining is None
