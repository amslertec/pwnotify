"""TDD für Task 4 der Multi-Tenant-Phase: Sync/Members-API (`POST …/sync`,
`GET …/members`) + Auto-Sync-Hooks in `create_group`/`set_group_tenants` +
`GroupOut.member_count`/`last_synced_at`.

Treibt die Route-Funktionen direkt an (wie `test_admin_groups.py`) -- die gewöhnliche
savepoint-isolierte `session`-Fixture genügt. Der gefälschte Graph-Client folgt exakt dem
Muster aus `test_group_sync_adversarial.py`: `group_sync.GraphClient` wird gepatcht, egal
ob `sync_group` von der Route oder direkt aufgerufen wird."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, require_superadmin, require_superadmin_default_context
from app.api.routes.admin_groups import (
    create_group,
    list_group_members,
    set_group_tenants,
    sync_group_route,
)
from app.core.errors import ForbiddenError, NotFoundError
from app.core.security import issue_token_pair
from app.models.user import AppUser
from app.repositories import assignment_group_repo, tenant_repo
from app.schemas.assignment_group import GroupCreate, GroupTenants
from app.services import group_sync
from app.services.graph.client import GraphConfig
from app.services.group_sync import GroupSyncError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


class _FakeRequest:
    """Duck-typed Request -- Guard/Route lesen nur `.cookies`/`.headers`/`.client`
    (exaktes Muster aus `test_admin_groups.py`/`test_matrix_b_route_gating.py`)."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _slug() -> str:
    return f"grp-sync-{uuid.uuid4().hex[:10]}"


def _entra_id() -> str:
    return f"grp-sync-entra-{uuid.uuid4().hex}"


def _member(upn: str, *, entra_id: str | None = None, name: str | None = None) -> dict[str, Any]:
    return {
        "id": entra_id or f"eid-{uuid.uuid4().hex}",
        "userPrincipalName": upn,
        "displayName": name or upn,
        "mail": upn,
    }


async def _mk_superadmin(session: AsyncSession) -> AppUser:
    user = AppUser(
        username=f"grp-sync-superadmin-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="superadmin",
    )
    session.add(user)
    await session.flush()
    return user


async def _mk_admin(session: AsyncSession) -> AppUser:
    user = AppUser(
        username=f"grp-sync-admin-{uuid.uuid4().hex[:8]}", password_hash="x", role="admin"
    )
    session.add(user)
    await session.flush()
    return user


def _request_with_claim(user_id: int, tenant_id: int | None) -> _FakeRequest:
    pair = issue_token_pair(str(user_id), active_tenant=tenant_id)
    return _FakeRequest({ACCESS_COOKIE: pair.access_token})


async def _default_context_request(session: AsyncSession, superadmin: AppUser) -> _FakeRequest:
    assert superadmin.id is not None
    default = await tenant_repo.default_tenant(session)
    request = _request_with_claim(superadmin.id, default.id)
    await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
    return request


class _FakeGraph:
    """Ersetzt `GraphClient` im Sync -- liefert die vorgegebenen Mitglieder je
    `entra_group_id`, oder wirft (Graph-Fehler-Pfad)."""

    def __init__(
        self,
        members_by_entra: dict[str, list[dict[str, Any]]],
        *,
        raises: Exception | None = None,
    ) -> None:
        self._members = members_by_entra
        self._raises = raises

    async def get_group_members(self, entra_group_id: str) -> list[dict[str, Any]]:
        if self._raises is not None:
            raise self._raises
        return list(self._members.get(entra_group_id, []))


def _patch_graph(
    monkeypatch: pytest.MonkeyPatch,
    members_by_entra: dict[str, list[dict[str, Any]]],
    *,
    raises: Exception | None = None,
) -> None:
    monkeypatch.setattr(
        group_sync, "GraphClient", lambda _cfg: _FakeGraph(members_by_entra, raises=raises)
    )


# ---- POST …/sync: counters returned, snapshot populated, GroupOut reflects it -------------- #


async def test_sync_route_returns_counters_and_updates_group_out(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    superadmin = await _mk_superadmin(session)
    await _default_context_request(session, superadmin)
    tenant = await tenant_repo.create(session, name="Sync Tenant", slug=_slug())
    assert tenant.id is not None

    entra = _entra_id()
    group = await assignment_group_repo.create(session, name="Sync Team", entra_group_id=entra)
    assert group.id is not None
    await assignment_group_repo.set_tenants(session, group.id, [tenant.id])

    upn = f"member-{uuid.uuid4().hex}@provider.example"
    _patch_graph(monkeypatch, {entra: [_member(upn)]})

    # Before sync: GroupOut reflects an empty, never-synced snapshot.
    from app.api.routes.admin_groups import _to_out

    before = await _to_out(session, group)
    assert before.member_count == 0
    assert before.last_synced_at is None

    result = await sync_group_route(superadmin, group.id, session)  # type: ignore[arg-type]
    assert result.member_count == 1
    assert result.added == 1
    assert result.removed == 0

    listed = await list_group_members(superadmin, group.id, session, page=1, size=25)  # type: ignore[arg-type]
    assert listed.total == 1
    assert listed.items[0].upn == upn

    refreshed = await _to_out(session, group)
    assert refreshed.member_count == 1
    assert refreshed.last_synced_at is not None


# ---- POST …/sync: Graph failure -> clean 4xx sync_failed, never 500 ------------------------ #


async def test_sync_route_graph_error_is_sync_failed_not_500(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.errors import GraphError

    superadmin = await _mk_superadmin(session)
    await _default_context_request(session, superadmin)
    entra = _entra_id()
    group = await assignment_group_repo.create(session, name="Broken Team", entra_group_id=entra)
    assert group.id is not None

    _patch_graph(monkeypatch, {}, raises=GraphError("boom", code="graph_error"))
    with pytest.raises(GroupSyncError) as exc_info:
        await sync_group_route(superadmin, group.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "sync_failed"
    assert exc_info.value.status_code == 502


# ---- POST …/sync: unknown group -> 404 ------------------------------------------------------ #


async def test_sync_route_missing_group_raises_not_found(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    await _default_context_request(session, superadmin)

    with pytest.raises(NotFoundError) as exc_info:
        await sync_group_route(superadmin, 999_999_999, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "group_not_found"
    assert exc_info.value.status_code == 404


# ---- GET …/members: pagination across >25 rows, correct total ------------------------------ #


async def test_members_route_paginates(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    superadmin = await _mk_superadmin(session)
    await _default_context_request(session, superadmin)
    entra = _entra_id()
    group = await assignment_group_repo.create(session, name="Big Team", entra_group_id=entra)
    assert group.id is not None

    members = [_member(f"user{i:03d}-{uuid.uuid4().hex[:6]}@provider.example") for i in range(30)]
    _patch_graph(monkeypatch, {entra: members})
    await sync_group_route(superadmin, group.id, session)  # type: ignore[arg-type]

    page1 = await list_group_members(superadmin, group.id, session, page=1, size=25)  # type: ignore[arg-type]
    page2 = await list_group_members(superadmin, group.id, session, page=2, size=25)  # type: ignore[arg-type]

    assert page1.total == 30
    assert page2.total == 30
    assert len(page1.items) == 25
    assert len(page2.items) == 5
    assert {i.entra_id for i in page1.items} & {i.entra_id for i in page2.items} == set()


async def test_members_route_missing_group_raises_not_found(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    await _default_context_request(session, superadmin)

    with pytest.raises(NotFoundError) as exc_info:
        await list_group_members(superadmin, 999_999_999, session, page=1, size=25)  # type: ignore[arg-type]
    assert exc_info.value.code == "group_not_found"


# ---- Auto-sync on create_group: fires, populates snapshot ---------------------------------- #


async def test_auto_sync_fires_on_create(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    entra = _entra_id()
    upn = f"autosync-{uuid.uuid4().hex}@provider.example"
    _patch_graph(monkeypatch, {entra: [_member(upn)]})

    created = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Auto Team", entra_group_id=entra),
        session,
    )
    assert created.member_count == 1
    assert created.last_synced_at is not None


# ---- Auto-sync on create_group: Graph error -> group still created, no 500 ------------------ #


async def test_auto_sync_graph_error_on_create_leaves_group_created(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.errors import GraphError

    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    entra = _entra_id()
    _patch_graph(monkeypatch, {}, raises=GraphError("boom", code="graph_error"))

    created = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Auto Fail Team", entra_group_id=entra),
        session,
    )
    assert created.id is not None
    assert created.member_count == 0
    assert created.last_synced_at is None
    assert await assignment_group_repo.get(session, created.id) is not None


# ---- Auto-sync on set_group_tenants: fires, re-materializes -------------------------------- #


async def test_auto_sync_fires_on_set_tenants(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    tenant = await tenant_repo.create(session, name="Set Tenants Tenant", slug=_slug())
    assert tenant.id is not None
    entra = _entra_id()
    upn = f"autosync-tenants-{uuid.uuid4().hex}@provider.example"
    _patch_graph(monkeypatch, {entra: [_member(upn)]})

    group = await assignment_group_repo.create(session, name="Retag Team", entra_group_id=entra)
    assert group.id is not None

    updated = await set_group_tenants(
        request,  # type: ignore[arg-type]
        superadmin,
        group.id,
        GroupTenants(tenant_ids=[tenant.id]),
        session,
    )
    assert updated.member_count == 1
    assert updated.last_synced_at is not None


# ---- Auto-sync on set_group_tenants: Graph error -> tenants still set, no 500 --------------- #


async def test_auto_sync_graph_error_on_set_tenants_leaves_tenants_set(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.errors import GraphError

    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    tenant = await tenant_repo.create(session, name="Set Tenants Fail Tenant", slug=_slug())
    assert tenant.id is not None
    entra = _entra_id()
    group = await assignment_group_repo.create(session, name="Fail Team", entra_group_id=entra)
    assert group.id is not None

    _patch_graph(monkeypatch, {}, raises=GraphError("boom", code="graph_error"))
    updated = await set_group_tenants(
        request,  # type: ignore[arg-type]
        superadmin,
        group.id,
        GroupTenants(tenant_ids=[tenant.id]),
        session,
    )
    assert updated.tenant_ids == [tenant.id]
    assert updated.member_count == 0
    assert updated.last_synced_at is None


# ---- Multi-tenant correctness: provider Graph config must come from the DEFAULT tenant ------ #
#
# Bug: both `sync_group_route` and `_auto_sync` built the Graph config from
# `SettingsService(session).get_all()` on the RAW owner session. `setting`'s PK is
# `(tenant_id, key)` and the owner DB role bypasses RLS, so an unscoped `select(Setting)`
# returns EVERY tenant's `graph.*` rows; `get_all()` folds them into a single dict keyed by
# `key` alone, so whichever tenant's row the (unordered) scan visits last silently wins --
# an undefined mix the instant a second customer configures Graph. Provider group
# enumeration must always resolve the DEFAULT/provider tenant's config, deterministically,
# never the customer's.
#
# Seeded via real, committed SQL against `migrated_engine` (not the savepoint-isolated
# `session` fixture): the fix reads settings through `tenant_scoped_session`, which opens
# its OWN connection via the app engine and therefore -- exactly as documented in
# `test_isolation_attack.py` -- sees no uncommitted data from a savepoint on a different
# connection.


@pytest_asyncio.fixture
async def default_and_customer_graph_settings(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int]]:
    """DEFAULT tenant configured with `graph.tenant_id='provider-tenant'`, a second,
    freshly created CUSTOMER tenant configured with a DIFFERENT `graph.tenant_id=
    'customer-tenant'` -- the sync must resolve the former, never the latter."""
    async with migrated_engine.connect() as conn:
        default_id = int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )
        customer_id = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('GroupSyncScopeCustomer','group-sync-scope-customer',true,now()) "
                        "RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:default_id, 'graph.tenant_id', to_jsonb('provider-tenant'::text), "
                "false, now()), "
                "(:default_id, 'graph.client_id', to_jsonb('provider-client'::text), "
                "false, now()), "
                "(:default_id, 'graph.client_secret', to_jsonb('provider-secret'::text), "
                "false, now()), "
                "(:default_id, 'graph.cloud', to_jsonb('global'::text), false, now()), "
                "(:customer_id, 'graph.tenant_id', to_jsonb('customer-tenant'::text), "
                "false, now()), "
                "(:customer_id, 'graph.client_id', to_jsonb('customer-client'::text), "
                "false, now()), "
                "(:customer_id, 'graph.client_secret', to_jsonb('customer-secret'::text), "
                "false, now()), "
                "(:customer_id, 'graph.cloud', to_jsonb('global'::text), false, now())"
            ),
            {"default_id": default_id, "customer_id": customer_id},
        )
        await conn.commit()
        try:
            yield default_id, customer_id
        finally:
            await conn.execute(
                text(
                    "DELETE FROM setting WHERE tenant_id = :default_id AND key IN "
                    "('graph.tenant_id', 'graph.client_id', 'graph.client_secret', "
                    "'graph.cloud')"
                ),
                {"default_id": default_id},
            )
            # Kaskadiert automatisch auf die `setting`-Zeilen des Customer-Tenants.
            await conn.execute(text("DELETE FROM tenant WHERE id = :cid"), {"cid": customer_id})
            await conn.commit()


def _patch_graph_capturing(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[GraphConfig],
    members_by_entra: dict[str, list[dict[str, Any]]],
) -> None:
    def _make(cfg: GraphConfig) -> _FakeGraph:
        captured.append(cfg)
        return _FakeGraph(members_by_entra)

    monkeypatch.setattr(group_sync, "GraphClient", _make)


async def test_sync_route_reads_provider_graph_config_from_default_tenant_scope(
    session: AsyncSession,
    default_and_customer_graph_settings: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bewusst KEIN `set_tenants(...,[customer_id])`: der Beweis gilt unabhängig davon, welchem
    # Kunden die Gruppe zugeordnet ist -- Provider-Gruppen-Enumeration muss IMMER das
    # Default-/Provider-Config nutzen. Ein FK-Grant auf den Customer-Tenant würde dessen
    # Zeile zudem bis zum Rollback der savepoint-`session` sperren und mit dem Teardown der
    # (echt committenden) Settings-Fixture kollidieren.
    _default_id, _customer_id = default_and_customer_graph_settings
    captured: list[GraphConfig] = []
    entra = _entra_id()
    _patch_graph_capturing(monkeypatch, captured, {entra: []})

    superadmin = await _mk_superadmin(session)
    await _default_context_request(session, superadmin)

    group = await assignment_group_repo.create(session, name="Scope Team", entra_group_id=entra)
    assert group.id is not None

    await sync_group_route(superadmin, group.id, session)  # type: ignore[arg-type]

    assert captured, "GraphClient wurde nie konstruiert -- der Sync ist nicht gelaufen."
    used = captured[-1]
    assert used.tenant_id == "provider-tenant", (
        f"Sync hat NICHT das Default-/Provider-Tenant-Graph-Config gelesen, sondern "
        f"{used.tenant_id!r} -- Owner-Session-Gemisch aus mehreren Tenants?"
    )
    assert used.client_id == "provider-client"
    assert used.tenant_id != "customer-tenant"


async def test_auto_sync_reads_provider_graph_config_from_default_tenant_scope(
    session: AsyncSession,
    default_and_customer_graph_settings: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gleicher Beweis über den `_auto_sync`-Pfad (`create_group`), der Task-4c-Bug sass an
    BEIDEN Stellen -- Route und Auto-Sync-Helfer -- getrennt."""
    _default_id, _customer_id = default_and_customer_graph_settings
    captured: list[GraphConfig] = []
    entra = _entra_id()
    _patch_graph_capturing(monkeypatch, captured, {entra: []})

    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)

    await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Auto Scope Team", entra_group_id=entra),
        session,
    )

    assert captured, "GraphClient wurde nie konstruiert -- der Auto-Sync ist nicht gelaufen."
    used = captured[-1]
    assert used.tenant_id == "provider-tenant", (
        f"Auto-Sync hat NICHT das Default-/Provider-Tenant-Graph-Config gelesen, sondern "
        f"{used.tenant_id!r} -- Owner-Session-Gemisch aus mehreren Tenants?"
    )
    assert used.tenant_id != "customer-tenant"


# ---- Guard rails: non-superadmin / superadmin in a customer context ------------------------- #


async def test_non_superadmin_cannot_call_sync_or_members_routes(session: AsyncSession) -> None:
    local_admin = await _mk_admin(session)

    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin(local_admin)
    assert exc_info.value.code == "superadmin_required"


async def test_superadmin_in_customer_context_is_rejected_for_sync_and_members(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="Sync Guard Tenant", slug=_slug())
    assert customer.id is not None
    request = _request_with_claim(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "default_context_required"
