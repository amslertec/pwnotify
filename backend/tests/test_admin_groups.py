"""TDD für Task 3 der Console+Groups+Invite-Phase: Assignment-Group-CRUD-API
("Teams", `admin_groups.py`) + `assignment_group_repo`.

Treibt die Route-Funktionen direkt an (wie `test_admin_tenants.py`/
`test_admin_assignments.py`/`test_matrix_b_route_gating.py`) -- die gewöhnliche
savepoint-isolierte `session`-Fixture genügt, kein manuelles Aufräumen nötig (äusserer
Rollback macht die Suite rückstandsfrei, zweimal hintereinander ausführbar).

`entra_group_id` ist in diesem Inkrement FREI-TEXT (Design §7) -- die Tests prüfen daher
nur Eindeutigkeit, kein Format."""

from __future__ import annotations

import uuid

import pydantic
import pytest
from app.api.deps import ACCESS_COOKIE, require_superadmin, require_superadmin_default_context
from app.api.routes.admin_groups import (
    create_group,
    delete_group,
    list_groups,
    set_group_tenants,
    update_group,
)
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.security import issue_token_pair
from app.models.assignment_group import AssignmentGroupTenant
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.repositories import assignment_group_repo, tenant_repo
from app.schemas.assignment_group import GroupCreate, GroupTenants, GroupUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeRequest:
    """Duck-typed Request -- Guard/Route lesen nur `.cookies`/`.headers`/`.client`
    (exaktes Muster aus `test_matrix_b_route_gating.py`)."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _slug() -> str:
    return f"grp-{uuid.uuid4().hex[:10]}"


def _entra_id() -> str:
    return f"grp-entra-{uuid.uuid4().hex[:10]}"


async def _mk_superadmin(session: AsyncSession) -> AppUser:
    user = AppUser(
        username=f"grp-superadmin-{uuid.uuid4().hex[:8]}", password_hash="x", role="superadmin"
    )
    session.add(user)
    await session.flush()
    return user


async def _mk_admin(session: AsyncSession) -> AppUser:
    user = AppUser(username=f"grp-admin-{uuid.uuid4().hex[:8]}", password_hash="x", role="admin")
    session.add(user)
    await session.flush()
    return user


async def _mk_tenant(session: AsyncSession, *, active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="Group Tenant", slug=_slug())
    if not active:
        assert t.id is not None
        t = await tenant_repo.update(session, t.id, is_active=False)
    return t


def _request_with_claim(user_id: int, tenant_id: int | None) -> _FakeRequest:
    pair = issue_token_pair(str(user_id), active_tenant=tenant_id)
    return _FakeRequest({ACCESS_COOKIE: pair.access_token})


async def _default_context_request(session: AsyncSession, superadmin: AppUser) -> _FakeRequest:
    """Baut eine Request mit `active_tenant`-Claim == Default-Tenant und läuft die
    Gate-Kette manuell durch -- `require_superadmin` -> `require_superadmin_default_context`,
    exakt wie FastAPIs DI es täte."""
    assert superadmin.id is not None
    default = await tenant_repo.default_tenant(session)
    request = _request_with_claim(superadmin.id, default.id)
    await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
    return request


async def _tenant_row(
    session: AsyncSession, group_id: int, tenant_id: int
) -> AssignmentGroupTenant | None:
    return (
        await session.execute(
            select(AssignmentGroupTenant).where(
                AssignmentGroupTenant.assignment_group_id == group_id,
                AssignmentGroupTenant.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


# ---- Full lifecycle: create -> assign A+B -> list -> rename -> set to just A -> delete ---- #


async def test_group_lifecycle_create_assign_rename_set_delete_cascades(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None

    created = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Team Alpha", entra_group_id=_entra_id(), role="admin"),
        session,
    )
    assert created.name == "Team Alpha"
    assert created.role == "admin"
    assert created.tenant_ids == []
    group_id = created.id

    assigned = await set_group_tenants(
        request,  # type: ignore[arg-type]
        superadmin,
        group_id,
        GroupTenants(tenant_ids=[tenant_a.id, tenant_b.id]),
        session,
    )
    assert set(assigned.tenant_ids) == {tenant_a.id, tenant_b.id}

    listed = await list_groups(superadmin, session)  # type: ignore[arg-type]
    row = next(g for g in listed if g.id == group_id)
    assert set(row.tenant_ids) == {tenant_a.id, tenant_b.id}

    renamed = await update_group(
        request,  # type: ignore[arg-type]
        superadmin,
        group_id,
        GroupUpdate(name="Team Alpha Renamed", role="admin"),
        session,
    )
    assert renamed.name == "Team Alpha Renamed"
    assert renamed.role == "admin"
    assert set(renamed.tenant_ids) == {tenant_a.id, tenant_b.id}

    narrowed = await set_group_tenants(
        request,  # type: ignore[arg-type]
        superadmin,
        group_id,
        GroupTenants(tenant_ids=[tenant_a.id]),
        session,
    )
    assert narrowed.tenant_ids == [tenant_a.id]
    assert await _tenant_row(session, group_id, tenant_a.id) is not None
    assert await _tenant_row(session, group_id, tenant_b.id) is None

    await delete_group(request, superadmin, group_id, session)  # type: ignore[arg-type]
    assert await assignment_group_repo.get(session, group_id) is None
    # Kaskade: die verbliebene Mitgliedschaftszeile (A) muss mit der Gruppe verschwunden sein.
    assert await _tenant_row(session, group_id, tenant_a.id) is None


# ---- Task 2: `role` surfaced through create/update/`GroupOut`, invalid role is a 422 -------- #


async def test_create_group_with_auditor_role_is_reflected_in_group_out(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)

    created = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Auditor Team", entra_group_id=_entra_id(), role="auditor"),
        session,
    )
    assert created.role == "auditor"


async def test_update_group_role_flips_from_auditor_to_admin(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)

    created = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Flip Team", entra_group_id=_entra_id(), role="auditor"),
        session,
    )
    assert created.role == "auditor"

    updated = await update_group(
        request,  # type: ignore[arg-type]
        superadmin,
        created.id,
        GroupUpdate(name="Flip Team", role="admin"),
        session,
    )
    assert updated.role == "admin"


def test_group_create_rejects_invalid_role() -> None:
    """`role` is `Literal["admin", "auditor"]` -- an unknown value never reaches the route or
    the DB, Pydantic rejects it at the edge (FastAPI turns this into a 422 for real requests)."""
    with pytest.raises(pydantic.ValidationError):
        GroupCreate(name="Bad Role Team", entra_group_id=_entra_id(), role="superadmin")  # type: ignore[arg-type]


def test_group_update_rejects_invalid_role() -> None:
    with pytest.raises(pydantic.ValidationError):
        GroupUpdate(name="Bad Role Team", role="nope")  # type: ignore[arg-type]


# ---- Guard rails: duplicate entra_group_id / inactive tenant ------------------------------- #


async def test_duplicate_entra_group_id_is_rejected(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    entra_id = _entra_id()

    await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Team A", entra_group_id=entra_id, role="admin"),
        session,
    )

    with pytest.raises(ConflictError) as exc_info:
        await create_group(
            request,  # type: ignore[arg-type]
            superadmin,
            GroupCreate(name="Team B", entra_group_id=entra_id, role="admin"),
            session,
        )
    assert exc_info.value.code == "group_exists"


async def test_set_tenants_rejects_inactive_tenant(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    created = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Team Inactive", entra_group_id=_entra_id(), role="admin"),
        session,
    )
    inactive = await _mk_tenant(session, active=False)
    assert inactive.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await set_group_tenants(
            request,  # type: ignore[arg-type]
            superadmin,
            created.id,
            GroupTenants(tenant_ids=[inactive.id]),
            session,
        )
    assert exc_info.value.code == "tenant_not_active"
    assert await _tenant_row(session, created.id, inactive.id) is None


async def test_create_group_toctou_integrity_error_maps_to_group_exists_409(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU-Regression: zwei parallele Creates können BEIDE den Vorab-Check
    (`get_by_entra_group_id`) passieren und erst am DB-Unique-Index kollidieren. Hier
    simuliert per Monkeypatch (Vorab-Check liefert IMMER `None`, also 'frei') -- der
    zweite `create` trifft dann echt auf den Unique-Index und MUSS trotzdem denselben
    `group_exists`-Konflikt (409) liefern, keinen rohen 500."""
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    entra_id = _entra_id()

    await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Team First", entra_group_id=entra_id, role="admin"),
        session,
    )

    async def _always_free(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(assignment_group_repo, "get_by_entra_group_id", _always_free)

    with pytest.raises(ConflictError) as exc_info:
        await create_group(
            request,  # type: ignore[arg-type]
            superadmin,
            GroupCreate(name="Team Second", entra_group_id=entra_id, role="admin"),
            session,
        )
    assert exc_info.value.code == "group_exists"


async def test_update_and_delete_unknown_group_raise_not_found(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)

    with pytest.raises(NotFoundError) as exc_info:
        await update_group(
            request,  # type: ignore[arg-type]
            superadmin,
            999_999_999,
            GroupUpdate(name="Whatever", role="admin"),
            session,
        )
    assert exc_info.value.code == "group_not_found"

    with pytest.raises(NotFoundError) as exc_info:
        await delete_group(request, superadmin, 999_999_999, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "group_not_found"


# ---- Guard rails: non-superadmin / superadmin in a customer context ------------------------ #


async def test_non_superadmin_cannot_call_group_routes(session: AsyncSession) -> None:
    local_admin = await _mk_admin(session)

    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin(local_admin)
    assert exc_info.value.code == "superadmin_required"


async def test_superadmin_in_customer_context_is_rejected(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await _mk_tenant(session)
    assert customer.id is not None
    request = _request_with_claim(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "default_context_required"


# ---- tenant_ids_for_entra_groups: union across groups, unknown id -> empty ------------------ #


async def test_tenant_ids_for_entra_groups_unions_across_groups(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    request = await _default_context_request(session, superadmin)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None

    group_1 = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Team 1", entra_group_id=_entra_id(), role="admin"),
        session,
    )
    group_2 = await create_group(
        request,  # type: ignore[arg-type]
        superadmin,
        GroupCreate(name="Team 2", entra_group_id=_entra_id(), role="admin"),
        session,
    )
    await set_group_tenants(
        request,  # type: ignore[arg-type]
        superadmin,
        group_1.id,
        GroupTenants(tenant_ids=[tenant_a.id]),
        session,
    )
    await set_group_tenants(
        request,  # type: ignore[arg-type]
        superadmin,
        group_2.id,
        GroupTenants(tenant_ids=[tenant_b.id]),
        session,
    )

    result = await assignment_group_repo.tenant_ids_for_entra_groups(
        session, {group_1.entra_group_id, group_2.entra_group_id}
    )
    assert result == {tenant_a.id, tenant_b.id}

    empty = await assignment_group_repo.tenant_ids_for_entra_groups(session, {"unknown-entra-id"})
    assert empty == set()
