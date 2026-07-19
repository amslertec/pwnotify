"""TDD für die Access-Rescope (Sicherheitsfix): die Access-Seite (`admin_users.list_users`)
scopt JETZT für JEDEN Aufrufer -- Superadmin eingeschlossen -- auf den AKTIVEN Mandanten.

**THE bug, den dieser Test beweist:** `list_users` lieferte für einen Superadmin vormals
`user_repo.list_all(session)` instanzweit -- unabhängig vom aktiven Tenant sah ein
Superadmin beim Wechsel zwischen Kunden immer dieselbe volle Kontoliste (Leselecke: die
Access-Seite eines Kunden zeigte fremde Konten). Die Tests hier seeden drei Tenants
(Default, A, B; je ein SSO-Admin + SSO-Auditor bei A/B, ein lokaler Admin heim am
Default-Tenant) und beweisen NON-VAKUOS, dass:
- ein an A gebundener lokaler Admin NIE ein Konto von B sieht (B wird tatsächlich befüllt),
- ein Superadmin im DEFAULT-Kontext NUR Default-Heim-Konten (+ die `superadmins`-Liste)
  sieht, NIE A's oder B's,
- derselbe Superadmin, in den Kontext A geschaltet, NUR A's Heim-Konten sieht (KEIN
  `superadmins`-Schlüssel, NIE Default's oder B's Konten),
- der Wechsel A -> B das Ergebnis tatsächlich ändert,
- ein gefälschter `active_tenant`-Claim auf einen nicht gehaltenen Tenant leer bleibt
  (default-deny), nie ein stiller Fallback auf "alles".

Treibt die Route-Funktionen direkt an (wie `test_admin_tenants.py`) -- die Routen öffnen
selbst keine zusätzliche Session (kein `tenant_scoped_session`/eigene Verbindung), die
gewöhnliche savepoint-isolierte `session`-Fixture (echtes Postgres, siehe `conftest.py`)
genügt: der äussere Rollback macht die Suite ohne manuelles Aufräumen rückstandsfrei,
zweimal hintereinander ausführbar. `list_users` erwartet seit der Rescope zusätzlich den
rohen `active_tenant`-Claim als drittes Argument (`ActiveTenantClaim`, unautorisiert -- die
Autorisierung passiert INNERHALB der Route über `tenant_repo.is_allowed`, s. dort) -- hier
direkt als Plain-`int | None` übergeben, exakt wie `create_local`s `active_tenant`-Parameter
es in diesem Testmodul bereits vormacht.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import default_tenant_id
from app.api.routes.admin_users import create_local, delete_user, list_users, set_role
from app.core.errors import ForbiddenError
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.auth import AdminUserCreate, RoleUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeSender:
    """Fakt den Mail-Versand für den Einladungspfad (Muster aus `test_invitation_flow.py`) --
    kein echter Netzwerkzugriff, der Test hier interessiert sich nur für `AdminUserOut.email`
    auf dem Rückgabewert, nicht für den Mailinhalt."""

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


def _slug() -> str:
    return f"t3-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession, *, slug: str | None = None) -> Tenant:
    return await tenant_repo.create(session, name=slug or "T3 Tenant", slug=slug or _slug())


async def _mk_user(
    session: AsyncSession,
    *,
    role: str,
    is_sso: bool = False,
    tenant_id: int | None = None,
) -> AppUser:
    u = AppUser(
        username=f"t3-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


class _Seed:
    default_id: int
    a_id: int
    b_id: int
    superadmin: AppUser
    default_local_admin: AppUser
    local_admin_a: AppUser
    sso_admin_a: AppUser
    sso_auditor_a: AppUser
    sso_admin_b: AppUser
    sso_auditor_b: AppUser


async def _seed(session: AsyncSession) -> _Seed:
    """Default-Tenant (real, aus der Migration) + zwei Kunden-Tenants A und B, je mit einem
    SSO-Admin + SSO-Auditor (B wird also WIRKLICH befüllt -- non-vakuöser Beweis, dass B nie
    an A oder den Default-Kontext leckt). Ein lokaler Admin heim am DEFAULT-Tenant (Provider-
    Personal -- non-vakuöser Beweis für die superadmin-default-Sicht), ein lokaler Admin NUR
    auf A zugewiesen (`admin_tenant`), ein Superadmin."""
    default_id = await default_tenant_id(session)
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None

    superadmin = await _mk_user(session, role="superadmin")
    default_local_admin = await _mk_user(session, role="admin", tenant_id=default_id)

    local_admin_a = await _mk_user(session, role="admin", tenant_id=a.id)
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=a.id))

    sso_admin_a = await _mk_user(session, role="admin", is_sso=True, tenant_id=a.id)
    sso_auditor_a = await _mk_user(session, role="auditor", is_sso=True, tenant_id=a.id)
    sso_admin_b = await _mk_user(session, role="admin", is_sso=True, tenant_id=b.id)
    sso_auditor_b = await _mk_user(session, role="auditor", is_sso=True, tenant_id=b.id)
    await session.flush()

    seed = _Seed()
    seed.default_id = default_id
    seed.a_id, seed.b_id = a.id, b.id
    seed.superadmin = superadmin
    seed.default_local_admin = default_local_admin
    seed.local_admin_a = local_admin_a
    seed.sso_admin_a = sso_admin_a
    seed.sso_auditor_a = sso_auditor_a
    seed.sso_admin_b = sso_admin_b
    seed.sso_auditor_b = sso_auditor_b
    return seed


# ---- list_users: gescopt auf den AKTIVEN Mandanten, für JEDEN Aufrufer -------------------- #


async def test_local_admin_a_sees_only_a_accounts_no_superadmins_key(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)

    out = await list_users(seed.local_admin_a, session, seed.a_id)  # type: ignore[arg-type]

    assert "superadmins" not in out
    sso_ids = {u.id for u in out["sso"]}
    local_ids = {u.id for u in out["local"]}

    # A's Konten müssen da sein ...
    assert seed.sso_admin_a.id in sso_ids
    assert seed.sso_auditor_a.id in sso_ids
    assert seed.local_admin_a.id in local_ids

    # ... B's Konten dürfen NIE erscheinen (non-vakuöser Beweis: B ist tatsächlich befüllt).
    assert seed.sso_admin_b.id not in sso_ids
    assert seed.sso_auditor_b.id not in sso_ids

    # Auch das Default-Heim-Konto (Provider-Personal) darf in A's Sicht nie erscheinen.
    assert seed.default_local_admin.id not in local_ids

    # Kein Superadmin taucht jemals in der lokalen Liste eines Nicht-Superadmins auf.
    assert seed.superadmin.id not in local_ids


async def test_sso_admin_a_sees_own_tenant_accounts_like_a_local_admin(
    session: AsyncSession,
) -> None:
    """Ein SSO-Admin hält per Kern-Invariante sein Heim-Tenant (`admin_tenants` = Grants
    vereinigt mit dem SSO-Heim bei Admin-Rolle) und verwaltet die Access-Seite seines Kunden
    wie ein lokaler Admin. Regressionsschutz gegen das frühere `is_sso`-Blanket-Gate, das
    SSO-Admins fälschlich immer leere Listen lieferte."""
    seed = await _seed(session)

    out = await list_users(seed.sso_admin_a, session, seed.a_id)  # type: ignore[arg-type]

    assert "superadmins" not in out
    sso_ids = {u.id for u in out["sso"]}
    local_ids = {u.id for u in out["local"]}

    # A's Konten (inkl. des SSO-Admins selbst) sind da ...
    assert seed.sso_admin_a.id in sso_ids
    assert seed.sso_auditor_a.id in sso_ids
    assert seed.local_admin_a.id in local_ids

    # ... B's Konten NIE (non-vakuöser Beweis: B ist tatsächlich befüllt).
    assert seed.sso_admin_b.id not in sso_ids
    assert seed.sso_auditor_b.id not in sso_ids


async def test_sso_admin_a_forged_claim_for_unheld_tenant_is_denied(
    session: AsyncSession,
) -> None:
    """Ein SSO-Admin von A, der einen `active_tenant`-Claim für B (nicht gehalten) fälscht,
    bekommt leere Listen -- `is_allowed` gated ihn wie jeden Nicht-Superadmin (sein
    `admin_tenants` ist nur `{A}`)."""
    seed = await _seed(session)

    out = await list_users(seed.sso_admin_a, session, seed.b_id)  # type: ignore[arg-type]

    assert out == {"local": [], "sso": []}


async def test_superadmin_default_context_sees_only_default_homed_plus_superadmins(
    session: AsyncSession,
) -> None:
    """Superadmin OHNE aktiven Kontextwechsel (Default-Kontext, Provider-Ebene): sieht NUR
    die Heim-Konten des DEFAULT-Tenants -- niemals A's oder B's -- plus zusätzlich die
    instanzweite `superadmins`-Liste (nur in diesem Kontext vorhanden)."""
    seed = await _seed(session)

    out = await list_users(seed.superadmin, session, seed.default_id)  # type: ignore[arg-type]

    assert "superadmins" in out
    superadmin_ids = {u.id for u in out["superadmins"]}
    assert seed.superadmin.id in superadmin_ids
    # Die Superadmin-Liste enthält NIE Nicht-Superadmins.
    assert seed.local_admin_a.id not in superadmin_ids

    local_ids = {u.id for u in out["local"]}
    sso_ids = {u.id for u in out["sso"]}

    # Default-Heim-Konto ist da (non-vakuös) ...
    assert seed.default_local_admin.id in local_ids
    # Superadmins tauchen nicht nochmal in "local" auf.
    assert seed.superadmin.id not in local_ids

    # ... aber A's und B's Heim-Konten NIE -- weder lokal noch SSO.
    assert seed.local_admin_a.id not in local_ids
    assert seed.sso_admin_a.id not in sso_ids
    assert seed.sso_auditor_a.id not in sso_ids
    assert seed.sso_admin_b.id not in sso_ids
    assert seed.sso_auditor_b.id not in sso_ids


async def test_superadmin_switched_into_customer_a_sees_only_a_no_superadmins_key(
    session: AsyncSession,
) -> None:
    """Derselbe Superadmin, in den Kontext A geschaltet: sieht NUR A's Heim-Konten -- KEIN
    `superadmins`-Schlüssel (der nur im Default-Kontext erscheint), NIE Default's
    Provider-Personal, NIE B's Konten."""
    seed = await _seed(session)

    out = await list_users(seed.superadmin, session, seed.a_id)  # type: ignore[arg-type]

    assert "superadmins" not in out
    local_ids = {u.id for u in out["local"]}
    sso_ids = {u.id for u in out["sso"]}

    assert seed.local_admin_a.id in local_ids
    assert seed.sso_admin_a.id in sso_ids
    assert seed.sso_auditor_a.id in sso_ids

    assert seed.default_local_admin.id not in local_ids
    assert seed.superadmin.id not in local_ids
    assert seed.sso_admin_b.id not in sso_ids
    assert seed.sso_auditor_b.id not in sso_ids


async def test_superadmin_switching_active_tenant_changes_the_returned_set(
    session: AsyncSession,
) -> None:
    """Wechsel A -> B ändert das Ergebnis TATSÄCHLICH -- kein Caching/Vermischen: A's Konten
    sind abwesend, sobald aktiv B ist, und umgekehrt."""
    seed = await _seed(session)

    out_a = await list_users(seed.superadmin, session, seed.a_id)  # type: ignore[arg-type]
    out_b = await list_users(seed.superadmin, session, seed.b_id)  # type: ignore[arg-type]

    a_sso_ids = {u.id for u in out_a["sso"]}
    b_sso_ids = {u.id for u in out_b["sso"]}

    assert seed.sso_admin_a.id in a_sso_ids
    assert seed.sso_admin_a.id not in b_sso_ids
    assert seed.sso_admin_b.id in b_sso_ids
    assert seed.sso_admin_b.id not in a_sso_ids
    assert a_sso_ids != b_sso_ids


async def test_unassigned_local_admin_sees_nothing(session: AsyncSession) -> None:
    """Default-Deny: ein lokaler Admin OHNE JEDE `admin_tenant`-Zuweisung sieht -- anders
    als vor dem Fix -- nicht die volle Liste, sondern gar nichts (kein Claim -> Fallback auf
    den Default-Tenant, den er ebenfalls nicht hält -> `is_allowed` verweigert)."""
    unassigned = await _mk_user(session, role="admin")
    out = await list_users(unassigned, session, None)  # type: ignore[arg-type]
    assert out == {"local": [], "sso": []}


async def test_local_admin_forged_claim_for_unheld_tenant_is_denied(
    session: AsyncSession,
) -> None:
    """Sicherheitskritisch: A's lokaler Admin mit einem gefälschten/veralteten
    `active_tenant`-Claim auf B (den er nicht hält) bekommt -- trotz des rohen Claims --
    default-deny statt B's (oder irgendwelche) Konten (non-vakuöser Beweis: B ist
    tatsächlich befüllt, die leere Antwort ist also KEIN Zufall)."""
    seed = await _seed(session)

    out = await list_users(seed.local_admin_a, session, seed.b_id)  # type: ignore[arg-type]

    assert out == {"local": [], "sso": []}


async def test_auditor_caller_gets_empty_scoped_lists(session: AsyncSession) -> None:
    """Default-Deny auch für den Auditor, obwohl die `/access`-Seite im Frontend
    admin-only ist -- dieses Gate gilt am Endpunkt selbst, unabhängig davon."""
    seed = await _seed(session)
    auditor = await _mk_user(session, role="auditor")
    assert auditor.id is not None
    session.add(AuditorTenant(user_id=auditor.id, tenant_id=seed.a_id))
    await session.flush()

    out = await list_users(auditor, session, seed.a_id)  # type: ignore[arg-type]
    assert out == {"local": [], "sso": []}


# ---- create_local: Scoping + Auto-Grant ---------------------------------------------------- #


async def test_local_admin_creates_auditor_grants_auditor_tenant_on_active_tenant(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-new-auditor-{uuid.uuid4().hex[:8]}",
        password="Str0ng!Passw0rd1",
        role="auditor",
    )

    out = await create_local(
        None,  # type: ignore[arg-type]
        seed.local_admin_a,
        body,
        session,
        seed.a_id,
    )

    assert out.role == "auditor"
    row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == out.id, AuditorTenant.tenant_id == seed.a_id
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "Neuer Auditor hat keine auditor_tenant(A)-Zuweisung erhalten"

    # NIE eine admin_tenant-Zeile (Grant-Typ muss zur Rolle passen) und NIE B.
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None
    b_row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == out.id, AuditorTenant.tenant_id == seed.b_id
            )
        )
    ).scalar_one_or_none()
    assert b_row is None

    # Erscheint danach in A's gescopter Liste.
    listed = await list_users(seed.local_admin_a, session, seed.a_id)  # type: ignore[arg-type]
    assert out.id in {u.id for u in listed["local"]}


async def test_local_admin_creates_admin_grants_admin_tenant_on_active_tenant(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-new-admin-{uuid.uuid4().hex[:8]}",
        password="Str0ng!Passw0rd1",
        role="admin",
    )

    out = await create_local(
        None,  # type: ignore[arg-type]
        seed.local_admin_a,
        body,
        session,
        seed.a_id,
    )

    assert out.role == "admin"
    row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == seed.a_id
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "Neuer Admin hat keine admin_tenant(A)-Zuweisung erhalten"

    auditor_row = (
        await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert auditor_row is None


async def test_local_admin_without_active_tenant_is_rejected(session: AsyncSession) -> None:
    """Kein `active_tenant`-Claim -> klare Ablehnung statt eines unsichtbaren,
    nicht zugewiesenen Kontos."""
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-orphan-{uuid.uuid4().hex[:8]}", password="Str0ng!Passw0rd1", role="admin"
    )
    with pytest.raises(ForbiddenError) as exc_info:
        await create_local(None, seed.local_admin_a, body, session, None)  # type: ignore[arg-type]
    assert exc_info.value.code == "tenant_required"


async def test_local_admin_cannot_scope_creation_to_unheld_tenant(session: AsyncSession) -> None:
    """Ein gefälschter/veralteter `active_tenant`-Claim auf B (den A's lokaler Admin nicht
    hält) wird -- trotz des rohen Claims -- über `tenant_repo.is_allowed` abgewiesen."""
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-rogue-{uuid.uuid4().hex[:8]}", password="Str0ng!Passw0rd1", role="admin"
    )
    with pytest.raises(ForbiddenError) as exc_info:
        await create_local(None, seed.local_admin_a, body, session, seed.b_id)  # type: ignore[arg-type]
    assert exc_info.value.code == "tenant_required"


async def test_superadmin_creates_user_unrestricted_without_auto_grant(
    session: AsyncSession,
) -> None:
    """Superadmin-Aufrufer: uneingeschränkt, KEINE automatische Zuweisung (Task 4 weist
    Tenants gezielt zu) -- funktioniert sogar ganz ohne `active_tenant`."""
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-super-created-{uuid.uuid4().hex[:8]}",
        password="Str0ng!Passw0rd1",
        role="admin",
    )
    out = await create_local(None, seed.superadmin, body, session, None)  # type: ignore[arg-type]

    assert out.role == "admin"
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None
    auditor_row = (
        await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert auditor_row is None


# ---- set_role / delete_user: Cross-Tenant-Fix (Whole-Branch-Review) ------------------------ #
#
# THE bug, den dieser Block beweist: Task 3 hat `list_users`/`create_local` gescopt, aber
# `set_role`/`delete_user` blieben nur über `AdminUser` gegatet (jeder Admin/Superadmin JEDER
# Tenant) und lösten `target` ohne RLS auf `app_user` (instanzweit) auf -- ein lokaler Admin
# von Tenant A konnte so die Rolle eines NUR-zu-B-gehörenden Kontos ändern oder es löschen
# (IDs sind sequentiell enumerierbar). Non-vakuöser Beweis: B (bzw. ein Konto in BEIDEN
# Tenants) wird tatsächlich befüllt und existiert nach dem abgelehnten Versuch unverändert
# weiter -- nicht nur behauptet.


async def test_local_admin_a_cannot_delete_b_only_user(session: AsyncSession) -> None:
    seed = await _seed(session)

    with pytest.raises(ForbiddenError) as exc_info:
        await delete_user(None, seed.local_admin_a, seed.sso_auditor_b.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "user_not_in_scope"
    assert await session.get(AppUser, seed.sso_auditor_b.id) is not None


async def test_local_admin_a_cannot_set_role_on_b_only_user(session: AsyncSession) -> None:
    seed = await _seed(session)

    with pytest.raises(ForbiddenError) as exc_info:
        await set_role(
            None,  # type: ignore[arg-type]
            seed.local_admin_a,
            seed.sso_auditor_b.id,
            RoleUpdate(role="admin"),
            session,
        )
    assert exc_info.value.code == "user_not_in_scope"
    refreshed = await session.get(AppUser, seed.sso_auditor_b.id)
    assert refreshed is not None
    assert refreshed.role == "auditor"


async def test_local_admin_a_can_still_delete_own_tenant_user(session: AsyncSession) -> None:
    """Regressionsschutz: die neue Scope-Prüfung sperrt nur FREMDE Tenants -- innerhalb des
    eigenen Bereichs bleibt der lokale Admin voll handlungsfähig."""
    seed = await _seed(session)

    out = await delete_user(None, seed.local_admin_a, seed.sso_auditor_a.id, session)  # type: ignore[arg-type]
    assert out.message
    assert await session.get(AppUser, seed.sso_auditor_a.id) is None


async def test_local_admin_a_can_still_set_role_on_own_tenant_user(session: AsyncSession) -> None:
    seed = await _seed(session)

    out = await set_role(
        None,  # type: ignore[arg-type]
        seed.local_admin_a,
        seed.sso_auditor_a.id,
        RoleUpdate(role="admin"),
        session,
    )
    assert out.role == "admin"


async def test_user_in_both_tenants_rejected_for_admin_holding_only_one(
    session: AsyncSession,
) -> None:
    """Teilmengen-Regel (nicht Schnittmenge): ein Konto mit `admin_tenant`-Grants auf A UND B
    darf NICHT von einem Aufrufer angetastet werden, der nur A hält -- eine Löschung/
    Rollenänderung würde sonst auch B ungewollt mittreffen, weil `app_user` instanzweit ist."""
    seed = await _seed(session)
    both = await _mk_user(session, role="admin")
    assert both.id is not None
    session.add(AdminTenant(user_id=both.id, tenant_id=seed.a_id))
    session.add(AdminTenant(user_id=both.id, tenant_id=seed.b_id))
    await session.flush()

    with pytest.raises(ForbiddenError) as exc_info:
        await delete_user(None, seed.local_admin_a, both.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "user_not_in_scope"
    assert await session.get(AppUser, both.id) is not None

    with pytest.raises(ForbiddenError) as exc_info2:
        await set_role(
            None,  # type: ignore[arg-type]
            seed.local_admin_a,
            both.id,
            RoleUpdate(role="auditor"),
            session,
        )
    assert exc_info2.value.code == "user_not_in_scope"
    refreshed = await session.get(AppUser, both.id)
    assert refreshed is not None
    assert refreshed.role == "admin"


# ---- AdminUserOut.email (Task 6): populated in list + create ------------------------------ #


async def test_list_users_populates_email_from_app_user(session: AsyncSession) -> None:
    """`AdminUserOut.email` (Task 6) muss aus `app_user.email` durchgereicht werden -- ein
    Konto mit hinterlegter E-Mail zeigt sie in der gescopten Liste, ein Konto ohne bleibt
    `None` (non-vakuöser Beweis: beide Zustände kommen tatsächlich vor, keine zufällige
    Übereinstimmung)."""
    seed = await _seed(session)
    seed.local_admin_a.email = "admin-a@example.test"
    await session.flush()

    out = await list_users(seed.local_admin_a, session, seed.a_id)  # type: ignore[arg-type]

    local_by_id = {u.id: u for u in out["local"]}
    assert local_by_id[seed.local_admin_a.id].email == "admin-a@example.test"
    # sso_admin_a wurde ohne E-Mail geseedet -- bleibt None, kein Platzhalter-String.
    sso_by_id = {u.id: u for u in out["sso"]}
    assert sso_by_id[seed.sso_admin_a.id].email is None


async def test_create_local_invite_populates_email_on_returned_admin_user_out(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """Der Einladungspfad in `create_local` setzt `user.email` VOR dem
    `AdminUserOut.model_validate(...)`-Rückgabewert (s. `admin_users.py`) -- muss also in der
    Response ankommen, nicht nur in der DB. `fake_sender` fakt nur den Mail-Versand (Muster
    aus `test_invitation_flow.py`), der hier nicht Gegenstand des Beweises ist."""
    seed = await _seed(session)
    body = AdminUserCreate(email=f"t3-invite-{uuid.uuid4().hex[:8]}@example.test", role="auditor")

    out = await create_local(None, seed.local_admin_a, body, session, seed.a_id)  # type: ignore[arg-type]

    assert out.email == body.email
    assert fake_sender.sent, "Einladungsmail wurde nicht verschickt"


async def test_superadmin_can_delete_and_set_role_across_tenants(session: AsyncSession) -> None:
    """Superadmin-Aufrufer: uneingeschränkte Reichweite -- die neue Scope-Prüfung gilt
    NICHT für ihn (bestehende Last-Superadmin-/Superadmin-Ziel-Guards bleiben unberührt, sie
    betreffen hier nicht-superadmin Ziele)."""
    seed = await _seed(session)

    out_role = await set_role(
        None,  # type: ignore[arg-type]
        seed.superadmin,
        seed.sso_auditor_b.id,
        RoleUpdate(role="admin"),
        session,
    )
    assert out_role.role == "admin"

    out_delete = await delete_user(None, seed.superadmin, seed.sso_admin_b.id, session)  # type: ignore[arg-type]
    assert out_delete.message
    assert await session.get(AppUser, seed.sso_admin_b.id) is None
