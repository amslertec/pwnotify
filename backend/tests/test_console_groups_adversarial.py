"""Task 9 der Console+Groups+Invite-Phase (SICHERHEITSKRITISCH): die adversarische
Sicherheits-Verifikationsmatrix, ERWEITERT um die drei neuen Flächen dieses Inkrements --
Gruppen-Grants (Task 4), Bulk-Zuweisung (Task 2) und Einladung/Reset-Tokens (Task 5).

Dies ist die ABNAHME-GATE-Datei für die gesamte Phase: ein rotes Assert hier ist ein ECHTER
Befund in Task 2/4/5, kein Testfehler -- die Anweisung ist, im Rotfall NICHT die Assertion
abzuschwächen, sondern die Arbeit dorthin zurückzugeben.

**Aufbau, bewusst gemischt (wie die beiden Vorbilder):**
- Die meisten Fälle (Gruppen-Grant-Invarianten, Einladung/Reset-Tokens, Bulk-Lock) laufen
  auf der savepoint-isolierten `session`-Fixture (`conftest.py`) -- echtes Postgres, aber
  KEIN eigener RLS-Rollenwechsel nötig, weil `app_user`/`admin_tenant`/`auditor_tenant`/
  `user_token`/`assignment_group*` instanzweite (nicht RLS-gescopte) Tabellen sind (siehe
  deren Modul-Docstrings). Genau das Muster von `test_group_grant_reconcile_adversarial.py`
  / `test_assignment_bulk_cross_grant_lock.py` / `test_invitation_flow.py` /
  `test_password_reset_flow.py` -- der äussere Rollback macht die Suite rückstandsfrei,
  zweimal hintereinander ausführbar, ohne manuelles Aufräumen.
- GENAU EIN Fall -- der RLS-Backstop-Test -- braucht eine ECHTE, committete Verbindung
  (`migrated_engine`, wie `test_context_gating_adversarial.py`): `entra_user` IST eine
  RLS-tenant-gescopte Tabelle, und `get_tenant_session` öffnet dafür eine EIGENE Verbindung
  (`tenant_scoped_session`/`get_session_factory`), die uncommittete Zeilen der
  savepoint-Fixture nicht sähe. Dieser eine Fixture räumt daher explizit in `finally` auf.

Die neun Fälle der Aufgabenstellung, in fünf Abschnitten:
A. Gruppen-Grant-Invarianten (Kunden-homed NIE gruppen-granted, auch bei beiden Teams;
   Provider bekommt exakt seine Team-Kunden, Rolle bestimmt die Art; Team-Austritt entzieht
   Gruppen-Grant, manueller Grant bleibt; Rollen-Flip räumt die andere Zieltabelle auf).
B. RLS-Backstop Ende-zu-Ende (Gruppen-Grant -> aktiver Tenant A liefert nur A; gefälschter
   Claim auf B -> 403; Team-Austritt entzieht A wieder).
C. Einladungs-Token-Sicherheit (Single-Use, Ablauf, KEINE Enumeration über
   Garbage/Expired/Consumed/Wrong-Purpose/Never-Existed, Scoped-Invite-Cross-Grant-Lock,
   serverseitige Passwort-Policy).
D. Reset-Token-Sicherheit (Teilmengen-Scope, Single-Use, ~1h-Ablauf, Zweck-Scoping in BEIDEN
   Richtungen, keine Enumeration, serverseitige Passwort-Policy).
E. Bulk-Cross-Grant-Lock, erneut auf Matrix-Ebene beweisen (gemischte Charge, Superadmin im
   Kunden-Kontext -> 403).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import secrets
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, get_current_user, get_tenant_session, limiter
from app.api.routes import users as users_routes
from app.api.routes.admin_assignments import bulk_assign, set_assignments
from app.api.routes.admin_users import create_local, send_reset
from app.api.routes.public_tokens import accept_token, reset_token, token_info
from app.core.errors import ForbiddenError
from app.core.security import hash_token, issue_token_pair
from app.db.session import get_session_factory
from app.models._base import utcnow
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.token import UserToken
from app.models.user import AppUser
from app.repositories import assignment_group_repo, tenant_repo
from app.schemas.assignment import AssignmentUpdate, BulkAssignmentUpdate
from app.schemas.auth import AdminUserCreate, TokenAccept, TokenReset
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


# ========================================================================================= #
# Shared helpers -- same conventions as the existing adversarial/route-driving test files.
# ========================================================================================= #
class _FakeRequest:
    """Duck-typed Request -- guards/routes read only `.cookies`/`.headers`/`.client`
    (exact convention from `test_matrix_b_route_gating.py` /
    `test_context_gating_adversarial.py`)."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _req(user_id: int, active_tenant: int | None) -> _FakeRequest:
    pair = issue_token_pair(str(user_id), active_tenant=active_tenant)
    return _FakeRequest({ACCESS_COOKIE: pair.access_token})


def _slug() -> str:
    return f"t9-{uuid.uuid4().hex[:10]}"


def _entra() -> str:
    return f"t9-grp-{uuid.uuid4().hex}"


class _FakeSender:
    """Faked mail transport -- same shape as `test_invitation_flow.py`/
    `test_password_reset_flow.py`'s `_FakeSender`."""

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


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Iterator[None]:
    """`slowapi` requires a real `starlette.Request` when enabled -- these tests call route
    functions directly with a duck-typed request/`None`, exactly like `test_invitation_flow.py`
    / `test_password_reset_flow.py`. Rate limiting itself is an HTTP-layer concern outside the
    scope of a direct route-function call; disabled here so the underlying token-security
    logic can be driven directly."""
    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


@pytest_asyncio.fixture
async def fake_sender(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[_FakeSender]:
    import app.services.user_token as user_token_service

    sender = _FakeSender()
    monkeypatch.setattr(user_token_service, "build_sender", lambda _settings: sender)
    yield sender


def _extract_token(html: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_-]+)", html)
    assert match is not None, f"no token found in mail body: {html!r}"
    return match.group(1)


async def _mk_tenant(session: AsyncSession, *, active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="T9 Tenant", slug=_slug())
    if not active:
        assert t.id is not None
        t = await tenant_repo.update(session, t.id, is_active=False)
    return t


async def _mk_user(
    session: AsyncSession,
    *,
    role: str,
    is_sso: bool = True,
    tenant_id: int | None = None,
    email: str | None = None,
) -> AppUser:
    u = AppUser(
        username=f"t9-{role}-{'sso' if is_sso else 'local'}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
        email=email,
    )
    session.add(u)
    await session.flush()
    return u


async def _mk_team(session: AsyncSession, tenant_ids: list[int], *, role: str = "admin") -> str:
    entra = _entra()
    group = await assignment_group_repo.create(
        session, name="T9 Team", entra_group_id=entra, role=role
    )
    assert group.id is not None
    await assignment_group_repo.set_tenants(session, group.id, tenant_ids)
    return entra


async def _admin_rows(session: AsyncSession, user_id: int) -> list[AdminTenant]:
    return list(
        (await session.execute(select(AdminTenant).where(AdminTenant.user_id == user_id))).scalars()
    )


async def _auditor_rows(session: AsyncSession, user_id: int) -> list[AuditorTenant]:
    return list(
        (
            await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == user_id))
        ).scalars()
    )


async def _all_admin_rows(session: AsyncSession, user_id: int) -> list[AdminTenant]:
    return await _admin_rows(session, user_id)


async def _token_row(session: AsyncSession, user_id: int, purpose: str) -> UserToken:
    """Only safe when EXACTLY one `purpose` row exists for the account (the common case in
    this file) -- callers that mint a second token for the same account/purpose (e.g. a
    reissued reset link) must look the row up by its own raw token instead (`_token_row_by_raw`),
    since `issue_reset`'s reissue leaves the PRIOR (now consumed) row in place alongside it."""
    return (
        await session.execute(
            select(UserToken).where(UserToken.app_user_id == user_id, UserToken.purpose == purpose)
        )
    ).scalar_one()


async def _token_row_by_raw(session: AsyncSession, raw: str) -> UserToken:
    return (
        await session.execute(select(UserToken).where(UserToken.token_hash == hash_token(raw)))
    ).scalar_one()


# ========================================================================================= #
# SECTION A -- Group-grant invariants (savepoint `session` fixture; instance-wide tables).
# ========================================================================================= #


async def test_customer_and_null_home_accounts_never_group_granted_even_with_both_teams(
    session: AsyncSession,
) -> None:
    """The isolation invariant at the group layer, non-vacuous: EVERY account whose home is
    a customer (or NULL) ends with ZERO grant rows, even when its `groups` claim lists BOTH
    teams (T1 -> A, T2 -> B) -- including its OWN home tenant's team. Only `is_provider_account`
    (home == default tenant) may ever gain a `source='group'` row."""
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    t1 = await _mk_team(session, [tenant_a.id])
    t2 = await _mk_team(session, [tenant_b.id])

    customer_a_sso_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    customer_b_sso_auditor = await _mk_user(session, role="auditor", tenant_id=tenant_b.id)
    null_home_admin = await _mk_user(session, role="admin", tenant_id=None)
    assert customer_a_sso_admin.id is not None
    assert customer_b_sso_auditor.id is not None
    assert null_home_admin.id is not None

    for account in (customer_a_sso_admin, customer_b_sso_auditor, null_home_admin):
        assert account.id is not None
        await assignment_group_repo.reconcile_group_grants(session, account, [t1, t2])
        assert await _admin_rows(session, account.id) == []
        assert await _auditor_rows(session, account.id) == []


async def test_provider_sso_admin_gets_exactly_its_teams_customers_role_drives_kind(
    session: AsyncSession,
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    t1 = await _mk_team(session, [tenant_a.id])
    t2 = await _mk_team(session, [tenant_b.id])

    provider_sso_admin = await _mk_user(session, role="admin", is_sso=True, tenant_id=default.id)
    assert provider_sso_admin.id is not None

    await assignment_group_repo.reconcile_group_grants(session, provider_sso_admin, [t1])
    rows = await _admin_rows(session, provider_sso_admin.id)
    assert {(r.tenant_id, r.source) for r in rows} == {(tenant_a.id, "group")}
    assert await _auditor_rows(session, provider_sso_admin.id) == []

    await assignment_group_repo.reconcile_group_grants(session, provider_sso_admin, [t1, t2])
    rows = await _admin_rows(session, provider_sso_admin.id)
    assert {(r.tenant_id, r.source) for r in rows} == {
        (tenant_a.id, "group"),
        (tenant_b.id, "group"),
    }

    provider_sso_auditor = await _mk_user(
        session, role="auditor", is_sso=True, tenant_id=default.id
    )
    assert provider_sso_auditor.id is not None
    await assignment_group_repo.reconcile_group_grants(session, provider_sso_auditor, [t1])
    aud_rows = await _auditor_rows(session, provider_sso_auditor.id)
    assert {(r.tenant_id, r.source) for r in aud_rows} == {(tenant_a.id, "group")}
    # Role drives kind -- NEVER an admin_tenant row for an auditor, even though it holds the
    # SAME team membership as the admin above.
    assert await _admin_rows(session, provider_sso_auditor.id) == []


async def test_team_leave_revokes_group_grant_manual_grant_persists(session: AsyncSession) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    t2 = await _mk_team(session, [tenant_b.id])

    provider = await _mk_user(session, role="admin", is_sso=True, tenant_id=default.id)
    assert provider.id is not None

    # A manual superadmin-assigned grant on A (an explicit action, independent of any team).
    await tenant_repo.add_grant(
        session, user_id=provider.id, tenant_id=tenant_a.id, kind="admin", source="manual"
    )
    # Joining T2 materializes a group grant on B.
    await assignment_group_repo.reconcile_group_grants(session, provider, [t2])
    rows = {(r.tenant_id, r.source) for r in await _admin_rows(session, provider.id)}
    assert rows == {(tenant_a.id, "manual"), (tenant_b.id, "group")}

    # Leaving ALL teams (groups=[]): B's group grant is revoked, A's manual grant PERSISTS
    # and stays source='manual' (never touched/converted by the reconcile).
    await assignment_group_repo.reconcile_group_grants(session, provider, [])
    rows = await _admin_rows(session, provider.id)
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant_a.id
    assert rows[0].source == "manual"


async def test_role_flip_cleans_stale_other_kind_group_grant_at_matrix_level(
    session: AsyncSession,
) -> None:
    """Non-vacuous against the pre-fix behaviour (Task 4 review): without the other-kind
    cleanup, the demoted account would silently retain a stale admin_tenant(A, group) row
    after Entra flips its role to auditor."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t1 = await _mk_team(session, [tenant_a.id])

    account = await _mk_user(session, role="admin", is_sso=True, tenant_id=default.id)
    assert account.id is not None

    await assignment_group_repo.reconcile_group_grants(session, account, [t1])
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, account.id)} == {
        (tenant_a.id, "group")
    }

    account.role = "auditor"
    await session.flush()

    await assignment_group_repo.reconcile_group_grants(session, account, [t1])
    assert await _admin_rows(session, account.id) == []
    assert {(r.tenant_id, r.source) for r in await _auditor_rows(session, account.id)} == {
        (tenant_a.id, "group")
    }


# ========================================================================================= #
# SECTION B -- RLS backstop end-to-end (real committed connection; `entra_user` IS RLS-scoped).
# ========================================================================================= #
@dataclass
class _RlsSeed:
    default_id: int
    a_id: int
    b_id: int
    provider_id: int
    entra_group_id: str
    a_entra_id: int
    b_entra_id: int


@pytest_asyncio.fixture
async def rls_seed(migrated_engine: AsyncEngine) -> AsyncGenerator[_RlsSeed]:
    tag = uuid.uuid4().hex[:8]
    async with migrated_engine.connect() as conn:
        default_id = int(
            (await conn.execute(text("SELECT id FROM tenant WHERE is_default"))).scalar_one()
        )
        a_id, b_id = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        f"('T9 RLS A {tag}', 't9-rls-a-{tag}', true, now()), "
                        f"('T9 RLS B {tag}', 't9-rls-b-{tag}', true, now()) RETURNING id"
                    )
                )
            )
            .scalars()
            .all()
        )
        a_id, b_id = int(a_id), int(b_id)

        provider_id = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO app_user (username, password_hash, role, is_active, "
                        "is_sso, tenant_id, failed_login_count, language, created_at, "
                        "updated_at) VALUES (:u, 'x', 'admin', true, true, :tid, 0, 'de', "
                        "now(), now()) RETURNING id"
                    ),
                    {"u": f"t9-rls-provider-sso-admin-{tag}", "tid": default_id},
                )
            ).scalar_one()
        )

        entra_group_id = f"t9-rls-team-{tag}"
        group_id = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group (name, entra_group_id, created_at) "
                        "VALUES (:n, :e, now()) RETURNING id"
                    ),
                    {"n": "T9 RLS Team", "e": entra_group_id},
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO assignment_group_tenant (assignment_group_id, tenant_id) "
                "VALUES (:g, :a)"
            ),
            {"g": group_id, "a": a_id},
        )

        a_entra_id, b_entra_id = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO entra_user (tenant_id, entra_id, upn, display_name, "
                        "other_mails, account_enabled, password_never_expires, excluded, "
                        "is_shared, raw, last_synced_at) VALUES "
                        f"(:a, 't9-rls-a-entra-{tag}', 't9-rls-a-{tag}@example.com', "
                        "'T9 RLS A User', '[]'::jsonb, true, false, false, false, "
                        "'{}'::jsonb, now()), "
                        f"(:b, 't9-rls-b-entra-{tag}', 't9-rls-b-{tag}@example.com', "
                        "'T9 RLS B User', '[]'::jsonb, true, false, false, false, "
                        "'{}'::jsonb, now()) RETURNING id"
                    ),
                    {"a": a_id, "b": b_id},
                )
            )
            .scalars()
            .all()
        )
        a_entra_id, b_entra_id = int(a_entra_id), int(b_entra_id)

        await conn.commit()

        seed = _RlsSeed(
            default_id=default_id,
            a_id=a_id,
            b_id=b_id,
            provider_id=provider_id,
            entra_group_id=entra_group_id,
            a_entra_id=a_entra_id,
            b_entra_id=b_entra_id,
        )
        try:
            yield seed
        finally:
            await conn.execute(
                text("DELETE FROM entra_user WHERE id IN (:a, :b)"),
                {"a": a_entra_id, "b": b_entra_id},
            )
            await conn.execute(
                text("DELETE FROM admin_tenant WHERE user_id = :u"), {"u": provider_id}
            )
            await conn.execute(
                text("DELETE FROM auditor_tenant WHERE user_id = :u"), {"u": provider_id}
            )
            await conn.execute(
                text("DELETE FROM assignment_group_tenant WHERE assignment_group_id = :g"),
                {"g": group_id},
            )
            await conn.execute(text("DELETE FROM assignment_group WHERE id = :g"), {"g": group_id})
            await conn.execute(
                text("DELETE FROM user_session WHERE user_id = :u"), {"u": provider_id}
            )
            await conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": provider_id})
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a_id, "b": b_id}
            )
            await conn.commit()


@contextlib.asynccontextmanager
async def _tenant_ctx(user_id: int, active_tenant: int | None) -> AsyncGenerator[AsyncSession]:
    """`TenantSessionDep` chain -- raises `ForbiddenError` (before yielding) for a
    forged/foreign/no-longer-allowed claim, exactly the attack surface this test closes."""
    request = _req(user_id, active_tenant)
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_tenant_session(request, user, owner)
        try:
            scoped = await anext(gen)
            yield scoped
        finally:
            await gen.aclose()


async def _entra_ids_visible(session: AsyncSession) -> set[int]:
    out = await users_routes.list_users(None, session, page=1, page_size=200)  # type: ignore[arg-type]
    return {item.id for item in out.items}


async def test_rls_backstop_group_grant_then_forged_claim_rejected_then_team_leave_denies(
    rls_seed: _RlsSeed,
) -> None:
    """End-to-end proof that a group grant is not just a row in `admin_tenant` but an
    ACTUAL, RLS-enforced operative capability -- and that it is revoked the moment the
    underlying team membership disappears, with no lingering access."""
    stub = AppUser(
        id=rls_seed.provider_id,
        username="x",
        password_hash="x",
        role="admin",
        is_sso=True,
        tenant_id=rls_seed.default_id,
    )

    async with get_session_factory()() as owner:
        await assignment_group_repo.reconcile_group_grants(owner, stub, [rls_seed.entra_group_id])
        rows = (
            (
                await owner.execute(
                    select(AdminTenant).where(AdminTenant.user_id == rls_seed.provider_id)
                )
            )
            .scalars()
            .all()
        )
    assert {(r.tenant_id, r.source) for r in rows} == {(rls_seed.a_id, "group")}

    # Switching active tenant to A: the group grant is a REAL operative capability -- A's
    # data (and ONLY A's) is visible.
    async with _tenant_ctx(rls_seed.provider_id, rls_seed.a_id) as tsession:
        ids = await _entra_ids_visible(tsession)
    assert ids == {rls_seed.a_entra_id}
    assert rls_seed.b_entra_id not in ids

    # A forged/switched claim naming the FOREIGN tenant B (never granted) -> explicit 403,
    # never a silent empty result.
    with pytest.raises(ForbiddenError) as exc:
        async with _tenant_ctx(rls_seed.provider_id, rls_seed.b_id):
            pass
    assert exc.value.code == "tenant_forbidden"

    # The account leaves the team (groups=[]): the group grant on A is reconciled away.
    async with get_session_factory()() as owner:
        await assignment_group_repo.reconcile_group_grants(owner, stub, [])
        rows = (
            (
                await owner.execute(
                    select(AdminTenant).where(AdminTenant.user_id == rls_seed.provider_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []

    # A is now denied too -- no lingering access after the team-leave reconcile.
    with pytest.raises(ForbiddenError) as exc:
        async with _tenant_ctx(rls_seed.provider_id, rls_seed.a_id):
            pass
    assert exc.value.code == "tenant_forbidden"


# ========================================================================================= #
# SECTION C -- Invitation-token security (savepoint `session` fixture).
# ========================================================================================= #


async def test_invite_no_enumeration_info_and_accept_indistinguishable_across_invalid_classes(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """`GET /public/token/info` and `POST /public/token/accept` must return the SAME
    generic invalid outcome for FIVE distinct invalid classes: garbage, never-existed,
    expired, consumed, and wrong-purpose (a live RESET token presented as an invite).
    Asserted by comparing the actual response objects/exceptions across all five -- not
    just that each individually fails."""
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)

    async def _mk_invite_raw(email: str) -> tuple[int, str]:
        out = await create_local(  # type: ignore[arg-type]
            None, superadmin, AdminUserCreate(email=email, role="admin"), session, None
        )
        assert out.id is not None
        return out.id, _extract_token(fake_sender.sent[-1]["html"])

    garbage = "not-a-real-token-at-all"
    never_existed = secrets.token_urlsafe(32)

    expired_uid, expired_raw = await _mk_invite_raw(f"expired-{uuid.uuid4().hex[:8]}@t9.test")
    row = await _token_row(session, expired_uid, "invite")
    row.expires_at = utcnow() - dt.timedelta(hours=1)
    await session.flush()

    _consumed_uid, consumed_raw = await _mk_invite_raw(f"consumed-{uuid.uuid4().hex[:8]}@t9.test")
    await accept_token(  # type: ignore[arg-type]
        None,
        TokenAccept(
            token=consumed_raw,
            first_name="A",
            last_name="B",
            username=f"t9-consumed-{uuid.uuid4().hex[:8]}",
            password="Str0ng!Passw0rd",
        ),
        session,
    )

    # Wrong-purpose: a LIVE reset token (correctly minted, correctly unexpired/unconsumed)
    # presented at the invite surface -- must be indistinguishable from the other four.
    reset_target = await _mk_user(
        session,
        role="admin",
        is_sso=False,
        tenant_id=(await _mk_tenant(session)).id,
        email=f"wrong-purpose-{uuid.uuid4().hex[:8]}@t9.test",
    )
    assert reset_target.id is not None
    assert superadmin.id is not None
    await send_reset(None, superadmin, reset_target.id, session)  # type: ignore[arg-type]
    wrong_purpose_raw = _extract_token(fake_sender.sent[-1]["html"])

    invalid_tokens = {
        "garbage": garbage,
        "never_existed": never_existed,
        "expired": expired_raw,
        "consumed": consumed_raw,
        "wrong_purpose": wrong_purpose_raw,
    }

    # `GET /public/token/info` -- every class returns the IDENTICAL generic shape.
    info_results = {}
    for label, raw in invalid_tokens.items():
        info = await token_info(None, session, raw, "invite")  # type: ignore[arg-type]
        info_results[label] = (info.valid, info.email, info.purpose)
    assert set(info_results.values()) == {(False, None, None)}, (
        f"token_info leaked a distinguishable signal across invalid classes: {info_results}"
    )

    # `POST /public/token/accept` -- every class raises the SAME code + message.
    accept_results = {}
    for label, raw in invalid_tokens.items():
        with pytest.raises(ForbiddenError) as exc:
            await accept_token(  # type: ignore[arg-type]
                None,
                TokenAccept(
                    token=raw,
                    first_name="A",
                    last_name="B",
                    username=f"t9-probe-{label}-{uuid.uuid4().hex[:8]}",
                    password="Str0ng!Passw0rd",
                ),
                session,
            )
        accept_results[label] = (exc.value.code, exc.value.message)
    expected_accept = {("token_invalid", "Einladung ungültig oder abgelaufen.")}
    assert set(accept_results.values()) == expected_accept, (
        f"accept_token leaked a distinguishable signal across invalid classes: {accept_results}"
    )


async def test_scoped_local_admin_invite_is_a_homed_a_granted_cannot_cross_grant_to_b(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)
    local_admin_a = await _mk_user(session, role="admin", is_sso=False, tenant_id=tenant_a.id)
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=tenant_a.id))
    await session.flush()

    email = f"scoped-invite-{uuid.uuid4().hex[:8]}@t9.test"
    out = await create_local(  # type: ignore[arg-type]
        None, local_admin_a, AdminUserCreate(email=email, role="admin"), session, tenant_a.id
    )
    assert out.id is not None

    pending = await session.get(AppUser, out.id)
    assert pending is not None and pending.tenant_id == tenant_a.id
    grant = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == tenant_a.id
            )
        )
    ).scalar_one_or_none()
    assert grant is not None

    with pytest.raises(ForbiddenError) as exc:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            out.id,
            AssignmentUpdate(tenant_ids=[tenant_b.id]),
            session,
        )
    assert exc.value.code == "customer_account_not_grantable"
    remaining = await tenant_repo.list_grant_tenant_ids(session, out.id, "admin")
    assert tenant_b.id not in remaining


async def test_invite_accept_server_side_password_policy_rejects_weak_password(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """Server-side policy is authoritative REGARDLESS of what a (hypothetically bypassed)
    client would allow -- a weak password is rejected even though `TokenAccept.password`
    only enforces a MIN LENGTH at the schema layer, not the full policy."""
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)
    email = f"weak-invite-{uuid.uuid4().hex[:8]}@t9.test"
    out = await create_local(  # type: ignore[arg-type]
        None, superadmin, AdminUserCreate(email=email, role="admin"), session, None
    )
    assert out.id is not None
    raw = _extract_token(fake_sender.sent[-1]["html"])

    with pytest.raises(ForbiddenError) as exc:
        await accept_token(  # type: ignore[arg-type]
            None,
            TokenAccept(
                token=raw,
                first_name="A",
                last_name="B",
                username=f"t9-weak-{uuid.uuid4().hex[:8]}",
                password="allweaklower1",  # 10+ chars, digit, lower -- no upper, no special
            ),
            session,
        )
    assert exc.value.code == "password_policy"
    # Policy failure does not consume the token -- a subsequent strong-password attempt works.
    assert (await _token_row(session, out.id, "invite")).consumed_at is None


# ========================================================================================= #
# SECTION D -- Reset-token security (savepoint `session` fixture).
# ========================================================================================= #


async def test_reset_admin_trigger_obeys_subset_scope_all_four_guard_cases(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)
    local_admin_a = await _mk_user(session, role="admin", is_sso=False, tenant_id=tenant_a.id)
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=tenant_a.id))

    target_a = await _mk_user(
        session,
        role="admin",
        is_sso=False,
        tenant_id=tenant_a.id,
        email=f"a-homed-{uuid.uuid4().hex[:8]}@t9.test",
    )
    session.add(AdminTenant(user_id=target_a.id, tenant_id=tenant_a.id))  # type: ignore[arg-type]
    target_b_only = await _mk_user(
        session,
        role="admin",
        is_sso=False,
        tenant_id=tenant_b.id,
        email=f"b-only-{uuid.uuid4().hex[:8]}@t9.test",
    )
    session.add(AdminTenant(user_id=target_b_only.id, tenant_id=tenant_b.id))  # type: ignore[arg-type]
    target_a_sso = await _mk_user(
        session,
        role="admin",
        is_sso=True,
        tenant_id=tenant_a.id,
        email=f"a-sso-{uuid.uuid4().hex[:8]}@t9.test",
    )
    target_a_no_email = await _mk_user(
        session, role="admin", is_sso=False, tenant_id=tenant_a.id, email=None
    )
    session.add(AdminTenant(user_id=target_a_no_email.id, tenant_id=tenant_a.id))  # type: ignore[arg-type]
    await session.flush()

    # (1) customer-admin-of-A can reset an A-homed local account with email -- success.
    msg = await send_reset(None, local_admin_a, target_a.id, session)  # type: ignore[arg-type]
    assert msg.message

    # (2) NOT a B-only account -> 403 user_not_in_scope.
    with pytest.raises(ForbiddenError) as exc:
        await send_reset(None, local_admin_a, target_b_only.id, session)  # type: ignore[arg-type]
    assert exc.value.code == "user_not_in_scope"

    # (3) NOT an SSO account, even though it is A-homed -> 403 sso_no_reset.
    with pytest.raises(ForbiddenError) as exc:
        await send_reset(None, local_admin_a, target_a_sso.id, session)  # type: ignore[arg-type]
    assert exc.value.code == "sso_no_reset"

    # (4) NOT an email-less account, even though it is A-homed -> 403 email_required.
    with pytest.raises(ForbiddenError) as exc:
        await send_reset(None, local_admin_a, target_a_no_email.id, session)  # type: ignore[arg-type]
    assert exc.value.code == "email_required"

    # Superadmin: subset scope does not apply -- can reset the B-only local account.
    msg = await send_reset(None, superadmin, target_b_only.id, session)  # type: ignore[arg-type]
    assert msg.message
    # But the SSO/email guards still apply even to the superadmin (business rules, not scope).
    with pytest.raises(ForbiddenError) as exc:
        await send_reset(None, superadmin, target_a_sso.id, session)  # type: ignore[arg-type]
    assert exc.value.code == "sso_no_reset"


async def test_reset_no_enumeration_info_and_reset_indistinguishable_across_invalid_classes(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)

    async def _mk_reset_raw(email: str) -> tuple[int, str]:
        tenant = await _mk_tenant(session)
        target = await _mk_user(
            session, role="admin", is_sso=False, tenant_id=tenant.id, email=email
        )
        assert target.id is not None
        await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
        return target.id, _extract_token(fake_sender.sent[-1]["html"])

    garbage = "not-a-real-token-at-all"
    never_existed = secrets.token_urlsafe(32)

    expired_uid, expired_raw = await _mk_reset_raw(f"expired-{uuid.uuid4().hex[:8]}@t9.test")
    row = await _token_row(session, expired_uid, "reset")
    row.expires_at = utcnow() - dt.timedelta(hours=1)
    await session.flush()

    _consumed_uid, consumed_raw = await _mk_reset_raw(f"consumed-{uuid.uuid4().hex[:8]}@t9.test")
    await reset_token(  # type: ignore[arg-type]
        None, TokenReset(token=consumed_raw, password="Str0ng!Passw0rd"), session
    )

    # Wrong-purpose: a LIVE invite token presented at the reset surface.
    wrong_purpose_email = f"wrong-purpose-reset-{uuid.uuid4().hex[:8]}@t9.test"
    invite_out = await create_local(  # type: ignore[arg-type]
        None, superadmin, AdminUserCreate(email=wrong_purpose_email, role="admin"), session, None
    )
    assert invite_out.id is not None
    wrong_purpose_raw = _extract_token(fake_sender.sent[-1]["html"])

    invalid_tokens = {
        "garbage": garbage,
        "never_existed": never_existed,
        "expired": expired_raw,
        "consumed": consumed_raw,
        "wrong_purpose": wrong_purpose_raw,
    }

    info_results = {}
    for label, raw in invalid_tokens.items():
        info = await token_info(None, session, raw, "reset")  # type: ignore[arg-type]
        info_results[label] = (info.valid, info.email, info.purpose)
    assert set(info_results.values()) == {(False, None, None)}, (
        f"token_info leaked a distinguishable signal across invalid classes: {info_results}"
    )

    reset_results = {}
    for label, raw in invalid_tokens.items():
        with pytest.raises(ForbiddenError) as exc:
            await reset_token(  # type: ignore[arg-type]
                None, TokenReset(token=raw, password="Str0ng!Passw0rd"), session
            )
        reset_results[label] = (exc.value.code, exc.value.message)
    assert set(reset_results.values()) == {("token_invalid", "Link ungültig oder abgelaufen.")}, (
        f"reset_token leaked a distinguishable signal across invalid classes: {reset_results}"
    )


async def test_reset_single_use_expiry_and_purpose_scoping_in_both_directions(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)
    tenant = await _mk_tenant(session)
    target = await _mk_user(
        session,
        role="admin",
        is_sso=False,
        tenant_id=tenant.id,
        email=f"reset-live-{uuid.uuid4().hex[:8]}@t9.test",
    )
    assert target.id is not None

    await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    reset_raw = _extract_token(fake_sender.sent[-1]["html"])

    invite_email = f"invite-live-{uuid.uuid4().hex[:8]}@t9.test"
    invite_out = await create_local(  # type: ignore[arg-type]
        None, superadmin, AdminUserCreate(email=invite_email, role="admin"), session, None
    )
    assert invite_out.id is not None
    invite_raw = _extract_token(fake_sender.sent[-1]["html"])

    # Direction 1: an INVITE token presented to /reset -> token_invalid.
    with pytest.raises(ForbiddenError) as exc:
        await reset_token(  # type: ignore[arg-type]
            None, TokenReset(token=invite_raw, password="Str0ng!Passw0rd"), session
        )
    assert exc.value.code == "token_invalid"

    # Direction 2: a RESET token presented to /accept -> token_invalid.
    with pytest.raises(ForbiddenError) as exc:
        await accept_token(  # type: ignore[arg-type]
            None,
            TokenAccept(
                token=reset_raw,
                first_name="A",
                last_name="B",
                username=f"t9-cross-{uuid.uuid4().hex[:8]}",
                password="Str0ng!Passw0rd",
            ),
            session,
        )
    assert exc.value.code == "token_invalid"

    # Single-use: the reset token still works legitimately once...
    msg = await reset_token(  # type: ignore[arg-type]
        None, TokenReset(token=reset_raw, password="Str0ng!Passw0rd"), session
    )
    assert msg.message
    # ...and a second use fails.
    with pytest.raises(ForbiddenError) as exc:
        await reset_token(  # type: ignore[arg-type]
            None, TokenReset(token=reset_raw, password="AnotherStr0ng!Pass"), session
        )
    assert exc.value.code == "token_invalid"

    # ~1h expiry: a freshly minted reset token, backdated past `expires_at`, is rejected.
    await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    second_raw = _extract_token(fake_sender.sent[-1]["html"])
    row = await _token_row_by_raw(session, second_raw)
    assert row.expires_at - row.created_at <= dt.timedelta(hours=1, minutes=1)
    row.expires_at = utcnow() - dt.timedelta(minutes=1)
    await session.flush()
    with pytest.raises(ForbiddenError) as exc:
        await reset_token(  # type: ignore[arg-type]
            None, TokenReset(token=second_raw, password="Str0ng!Passw0rd"), session
        )
    assert exc.value.code == "token_invalid"


async def test_reset_public_endpoint_enforces_password_policy_server_side(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)
    tenant = await _mk_tenant(session)
    target = await _mk_user(
        session,
        role="admin",
        is_sso=False,
        tenant_id=tenant.id,
        email=f"weak-reset-{uuid.uuid4().hex[:8]}@t9.test",
    )
    assert target.id is not None
    await send_reset(None, superadmin, target.id, session)  # type: ignore[arg-type]
    raw = _extract_token(fake_sender.sent[-1]["html"])

    with pytest.raises(ForbiddenError) as exc:
        await reset_token(None, TokenReset(token=raw, password="allweaklower1"), session)  # type: ignore[arg-type]
    assert exc.value.code == "password_policy"
    assert (await _token_row(session, target.id, "reset")).consumed_at is None


# ========================================================================================= #
# SECTION E -- Bulk cross-grant lock, re-proven at the matrix level.
# ========================================================================================= #


async def test_bulk_mixed_batch_customer_homed_skipped_no_foreign_grant_written(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin", is_sso=False)
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None

    p1 = await _mk_user(session, role="admin", is_sso=False, tenant_id=default.id)
    p2 = await _mk_user(session, role="admin", is_sso=False, tenant_id=default.id)
    customer_a_admin = await _mk_user(session, role="admin", is_sso=False, tenant_id=tenant_a.id)
    assert p1.id is not None and p2.id is not None and customer_a_admin.id is not None

    out = await bulk_assign(
        None,  # type: ignore[arg-type]
        superadmin,
        BulkAssignmentUpdate(
            user_ids=[p1.id, p2.id, customer_a_admin.id],
            tenant_ids=[tenant_a.id, tenant_b.id],
            action="set",
        ),
        session,
    )

    assert sorted(out.updated) == sorted([p1.id, p2.id])
    assert len(out.skipped) == 1
    assert out.skipped[0].user_id == customer_a_admin.id
    assert out.skipped[0].reason == "customer_account_not_grantable"
    assert await _all_admin_rows(session, customer_a_admin.id) == []

    for provider in (p1, p2):
        assert provider.id is not None
        rows = {(r.tenant_id, r.source) for r in await _all_admin_rows(session, provider.id)}
        assert rows == {(tenant_a.id, "manual"), (tenant_b.id, "manual")}


async def test_bulk_blocked_when_superadmin_switched_into_customer_context(
    session: AsyncSession,
) -> None:
    from app.api.deps import require_superadmin_default_context

    superadmin = await _mk_user(session, role="superadmin", is_sso=False)
    assert superadmin.id is not None
    customer = await _mk_tenant(session)
    provider_admin = await _mk_user(session, role="admin", is_sso=False)
    tenant_a = await _mk_tenant(session)
    assert customer.id is not None and provider_admin.id is not None and tenant_a.id is not None

    request = _req(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await bulk_assign(
            request,  # type: ignore[arg-type]
            guarded,
            BulkAssignmentUpdate(
                user_ids=[provider_admin.id], tenant_ids=[tenant_a.id], action="add"
            ),
            session,
        )
    assert exc.value.code == "default_context_required"
    assert await _all_admin_rows(session, provider_admin.id) == []
