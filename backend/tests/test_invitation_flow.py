"""TDD für Task 5 (Console+Groups+Invite): Einladungs-Fluss -- `create_local` im
Einladungsmodus (`admin_users.py`) + die öffentlichen Accept-/Info-Endpunkte
(`api/routes/public_tokens.py`).

Treibt Route-Funktionen direkt an (Muster wie `test_create_local_home.py`/
`test_admin_users_scoping.py`) -- die savepoint-isolierte `session`-Fixture (echtes
Postgres) genügt: `services.user_token`s Mail-Versand öffnet zwar intern eine EIGENE
`tenant_scoped_session` (RLS-Isolation für `setting`, s. dortigen Docstring), liest darüber
aber nur Branding-DEFAULTS (kein Test hier setzt eigene `setting`-Zeilen) -- diese Zeile ist
unabhängig von der Verbindung, über die sie gelesen wird. Der äussere Rollback macht die
Suite ohne manuelles Aufräumen rückstandsfrei, zweimal hintereinander ausführbar.

Der Mail-Versand selbst wird über `monkeypatch` auf `services.user_token.build_sender`
gefaked (`_FakeSender` sammelt die verschickten Mails) -- kein echter Netzwerkzugriff, der
rohe Token wird aus der URL im HTML-Body zurückgewonnen (das ist exakt der Kanal, über den
ein echter Empfänger ihn ebenfalls nur bekäme).

Der Rate-Limiter (`@limiter.limit` auf allen `public_tokens`-Endpunkten) wird für die Dauer
dieses Moduls abgeschaltet: `slowapi` verlangt bei aktivem Limiter eine ECHTE
`starlette.requests.Request`-Instanz (Isinstance-Check) -- ein reiner Funktionsaufruf mit
`request=None` (wie es der Rest dieser Suite überall sonst tut) würde sonst mit einer
internen `slowapi`-Exception scheitern. Das Rate-Limiting selbst ist hier nicht der Beweis
(dafür bräuchte es ohnehin einen echten HTTP-Client), sondern nur ein Implementierungsdetail
der Endpunkte -- fürs Deaktivieren zuständig ist `_disable_rate_limiter` unten, die den
Zustand danach wieder herstellt (der `Limiter` ist ein Modul-Singleton, geteilt mit jeder
anderen Suite im selben Lauf)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import limiter
from app.api.routes.admin_assignments import set_assignments
from app.api.routes.admin_users import create_local
from app.api.routes.public_tokens import accept_token, token_info
from app.core.errors import ConflictError, ForbiddenError
from app.core.security import hash_token
from app.models._base import utcnow
from app.models.tenant import AdminTenant, Tenant
from app.models.token import UserToken
from app.models.user import AppUser
from app.repositories import tenant_repo, user_token_repo
from app.schemas.assignment import AssignmentUpdate
from app.schemas.auth import AdminUserCreate, TokenAccept
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
    return f"ti5-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession) -> Tenant:
    return await tenant_repo.create(session, name="Ti5 Tenant", slug=_slug())


async def _mk_user(
    session: AsyncSession, *, role: str, is_sso: bool = False, tenant_id: int | None = None
) -> AppUser:
    u = AppUser(
        username=f"ti5-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
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


# ---- Einladung anlegen ----------------------------------------------------------------- #


async def test_superadmin_invite_creates_pending_inactive_account_and_sends_mail(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    email = f"alice-{uuid.uuid4().hex[:8]}@ti5.test"
    body = AdminUserCreate(email=email, role="admin")

    out = await create_local(None, superadmin, body, session, None)  # type: ignore[arg-type]

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None
    assert persisted.username.startswith("pending:")
    assert persisted.is_active is False
    assert persisted.email == email

    token_row = await _token_row(session, out.id, "invite")  # type: ignore[arg-type]
    assert token_row.consumed_at is None
    assert token_row.expires_at > utcnow()

    assert len(fake_sender.sent) == 1
    msg = fake_sender.sent[0]
    assert msg["to"] == [email]
    assert "Einladung" in msg["subject"]
    raw = _extract_token(msg["html"])
    assert hash_token(raw) == token_row.token_hash
    assert "/einladung?token=" in msg["html"]


async def test_invite_requires_email(session: AsyncSession, fake_sender: _FakeSender) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    body = AdminUserCreate(role="admin")  # weder username noch password noch email

    with pytest.raises(ForbiddenError) as exc_info:
        await create_local(None, superadmin, body, session, None)  # type: ignore[arg-type]
    assert exc_info.value.code == "email_required"
    assert fake_sender.sent == []


# ---- GET /public/token/info -------------------------------------------------------------- #


async def test_token_info_valid_then_garbage_all_null(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    email = f"info-{uuid.uuid4().hex[:8]}@ti5.test"
    out = await create_local(  # type: ignore[arg-type]
        None, superadmin, AdminUserCreate(email=email, role="admin"), session, None
    )
    raw = _extract_token(fake_sender.sent[0]["html"])

    info = await token_info(None, session, raw, "invite")  # type: ignore[arg-type]
    assert info.valid is True
    assert info.email == email
    assert info.purpose == "invite"

    garbage = await token_info(None, session, "not-a-real-token", "invite")  # type: ignore[arg-type]
    assert garbage.valid is False
    assert garbage.email is None
    assert garbage.purpose is None
    assert out.id is not None  # nur zur Beruhigung von mypy/Linters, s.o. verwendet


# ---- POST /public/token/accept ------------------------------------------------------------ #


async def _mk_invite(
    session: AsyncSession, fake_sender: _FakeSender, *, caller: AppUser, active_tenant: int | None
) -> tuple[int, str]:
    email = f"accept-{uuid.uuid4().hex[:8]}@ti5.test"
    out = await create_local(  # type: ignore[arg-type]
        None, caller, AdminUserCreate(email=email, role="admin"), session, active_tenant
    )
    raw = _extract_token(fake_sender.sent[-1]["html"])
    assert out.id is not None
    return out.id, raw


async def test_accept_weak_password_rejected_account_stays_inactive(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    user_id, raw = await _mk_invite(session, fake_sender, caller=superadmin, active_tenant=None)

    with pytest.raises(ForbiddenError) as exc_info:
        await accept_token(  # type: ignore[arg-type]
            None,
            TokenAccept(
                token=raw,
                first_name="Alice",
                last_name="Test",
                username=f"alice-{uuid.uuid4().hex[:8]}",
                password="weakpassword",  # keine Ziffer, kein Sonderzeichen
            ),
            session,
        )
    assert exc_info.value.code == "password_policy"

    persisted = await session.get(AppUser, user_id)
    assert persisted is not None and persisted.is_active is False
    # Token bleibt gültig -- ein Policy-Fehlschlag darf keinen Einlöseversuch verbrauchen.
    assert (await _token_row(session, user_id, "invite")).consumed_at is None


async def test_accept_username_taken_does_not_consume_token(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    user_id, raw = await _mk_invite(session, fake_sender, caller=superadmin, active_tenant=None)
    taken = await _mk_user(session, role="admin")

    with pytest.raises(ConflictError) as exc_info:
        await accept_token(  # type: ignore[arg-type]
            None,
            TokenAccept(
                token=raw,
                first_name="Alice",
                last_name="Test",
                username=taken.username,
                password="Str0ng!Passw0rd",
            ),
            session,
        )
    assert exc_info.value.code == "username_taken"
    assert (await _token_row(session, user_id, "invite")).consumed_at is None


async def test_accept_valid_activates_account_then_second_accept_fails(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    user_id, raw = await _mk_invite(session, fake_sender, caller=superadmin, active_tenant=None)
    new_username = f"alice-real-{uuid.uuid4().hex[:8]}"

    msg = await accept_token(  # type: ignore[arg-type]
        None,
        TokenAccept(
            token=raw,
            first_name="Alice",
            last_name="Test",
            username=new_username,
            password="Str0ng!Passw0rd",
        ),
        session,
    )
    assert msg.message

    persisted = await session.get(AppUser, user_id)
    assert persisted is not None
    assert persisted.is_active is True
    assert persisted.username == new_username
    assert persisted.display_name == "Alice Test"

    row = await _token_row(session, user_id, "invite")
    assert row.consumed_at is not None

    # Single-Use: derselbe Token ein zweites Mal -> generischer Fehlschlag.
    with pytest.raises(ForbiddenError) as exc_info:
        await accept_token(  # type: ignore[arg-type]
            None,
            TokenAccept(
                token=raw,
                first_name="Alice",
                last_name="Test",
                username=f"alice-again-{uuid.uuid4().hex[:8]}",
                password="Str0ng!Passw0rd",
            ),
            session,
        )
    assert exc_info.value.code == "token_invalid"


async def test_consume_atomic_second_call_on_already_consumed_token_is_noop(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """TOCTOU-Regression für die atomare guarded UPDATE in `user_token_repo.consume`
    (`WHERE id=:id AND consumed_at IS NULL`): zwei 'Racer', die BEIDE dieselbe noch-gültige
    Token-Zeile gelesen haben, bevor einer von beiden committet hat, dürfen NICHT beide
    erfolgreich verbrauchen. Simuliert hier durch zwei direkte `consume`-Aufrufe auf
    demselben, im Speicher unverändert gebliebenen `UserToken`-Objekt -- exakt das Bild, das
    ein zweiter Request über `get_live_by_hash` VOR dem Commit des ersten sähe."""
    superadmin = await _mk_user(session, role="superadmin")
    user_id, _raw = await _mk_invite(session, fake_sender, caller=superadmin, active_tenant=None)
    row = await _token_row(session, user_id, "invite")

    first = await user_token_repo.consume(session, row)
    assert first is True
    consumed_at_first = (await _token_row(session, user_id, "invite")).consumed_at
    assert consumed_at_first is not None

    second = await user_token_repo.consume(session, row)
    assert second is False

    # `consumed_at` bleibt exakt der erste Zeitstempel -- kein zweites Set/Commit.
    consumed_at_after = (await _token_row(session, user_id, "invite")).consumed_at
    assert consumed_at_after == consumed_at_first


async def test_expired_invite_token_is_rejected(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    import datetime as dt

    superadmin = await _mk_user(session, role="superadmin")
    user_id, raw = await _mk_invite(session, fake_sender, caller=superadmin, active_tenant=None)

    row = await _token_row(session, user_id, "invite")
    row.expires_at = utcnow() - dt.timedelta(hours=1)
    await session.flush()

    info = await token_info(None, session, raw, "invite")  # type: ignore[arg-type]
    assert info.valid is False

    with pytest.raises(ForbiddenError) as exc_info:
        await accept_token(  # type: ignore[arg-type]
            None,
            TokenAccept(
                token=raw,
                first_name="Alice",
                last_name="Test",
                username=f"alice-expired-{uuid.uuid4().hex[:8]}",
                password="Str0ng!Passw0rd",
            ),
            session,
        )
    assert exc_info.value.code == "token_invalid"


# ---- Scoping bleibt unverändert (Task-2-Cross-Grant-Lock) -------------------------------- #


async def test_scoped_admin_invite_is_a_homed_a_granted_not_cross_grantable_to_b(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    superadmin = await _mk_user(session, role="superadmin")
    local_admin_a = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=tenant_a.id))
    await session.flush()

    user_id, _raw = await _mk_invite(
        session, fake_sender, caller=local_admin_a, active_tenant=tenant_a.id
    )

    persisted = await session.get(AppUser, user_id)
    assert persisted is not None and persisted.tenant_id == tenant_a.id
    grant = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == user_id, AdminTenant.tenant_id == tenant_a.id
            )
        )
    ).scalar_one_or_none()
    assert grant is not None, "eingeladenes Konto hat keine admin_tenant(A)-Zuweisung erhalten"

    with pytest.raises(ForbiddenError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            user_id,
            AssignmentUpdate(tenant_ids=[tenant_b.id]),
            session,
        )
    assert exc_info.value.code == "customer_account_not_grantable"


async def test_direct_create_path_unchanged_with_password(session: AsyncSession) -> None:
    """Regressionsschutz: `password` weiterhin präsent -> bestehender Direktpfad, keine
    Einladung, kein Mail-Versand-Aufruf nötig (kein `fake_sender` hier -- ein echter Versand
    würde sonst mangels Monkeypatch tatsächlich fehlschlagen und den Test verraten)."""
    superadmin = await _mk_user(session, role="superadmin")
    username = f"direct-{uuid.uuid4().hex[:8]}"
    out = await create_local(  # type: ignore[arg-type]
        None,
        superadmin,
        AdminUserCreate(username=username, password="a-strong-password-1", role="admin"),
        session,
        None,
    )
    persisted = await session.get(AppUser, out.id)
    assert persisted is not None
    assert persisted.username == username
    assert persisted.is_active is True
    assert persisted.email is None
    existing = (
        await session.execute(select(UserToken).where(UserToken.app_user_id == out.id))
    ).scalar_one_or_none()
    assert existing is None
