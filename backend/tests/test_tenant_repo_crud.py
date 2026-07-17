"""TDD für die Tenant-CRUD-Funktionen in `tenant_repo` (Phase 4c Task 1) -- reine
Datenschicht: `create`/`update`/`delete`/`get_by_slug`/`list_all`/`count_sso_users`.

Läuft wie `test_tenant_authorization.py` auf der gewöhnlichen, savepoint-isolierten
`session`-Fixture: `create`/`update` prüfen Duplikate per Anwendungslogik (SELECT vor
INSERT/UPDATE), bevor je ein `session.commit()` folgt -- es kommt nie zu einem
IntegrityError innerhalb der SAVEPOINT-Transaktion (anders als in `test_entra_uniqueness.py`,
wo der DB-Unique-Constraint selbst getestet wird). `session.commit()` committet hier nur
die SAVEPOINT-Ebene; die äußere Transaktion der Fixture wird am Testende zurückgerollt --
volle Rückstandsfreiheit ist damit ohne eigenes Aufräumen gegeben (siehe conftest.py).
"""

from __future__ import annotations

import pytest
from app.core.errors import ConflictError, NotFoundError
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from sqlalchemy.ext.asyncio import AsyncSession


async def _mk_tenant(
    session: AsyncSession, *, slug: str, entra_tenant_id: str | None = None
) -> Tenant:
    t = Tenant(name=slug, slug=slug, entra_tenant_id=entra_tenant_id)
    session.add(t)
    await session.flush()
    return t


async def _mk_sso_user(session: AsyncSession, *, username: str, tenant_id: int) -> AppUser:
    u = AppUser(
        username=username, password_hash="x", role="admin", is_sso=True, tenant_id=tenant_id
    )
    session.add(u)
    await session.flush()
    return u


# ---- create ----------------------------------------------------------------------------- #


async def test_create_returns_persisted_tenant(session: AsyncSession) -> None:
    t = await tenant_repo.create(
        session, name="Contoso AG", slug="contoso-crud", entra_tenant_id="tid-contoso"
    )
    assert t.id is not None
    assert t.name == "Contoso AG"
    assert t.slug == "contoso-crud"
    assert t.entra_tenant_id == "tid-contoso"
    assert t.is_active is True

    found = await tenant_repo.get_by_slug(session, "contoso-crud")
    assert found is not None and found.id == t.id


async def test_create_without_entra_tid_is_allowed(session: AsyncSession) -> None:
    t = await tenant_repo.create(session, name="Fabrikam", slug="fabrikam-crud")
    assert t.entra_tenant_id is None


async def test_create_duplicate_slug_raises_conflict(session: AsyncSession) -> None:
    await tenant_repo.create(session, name="Dup A", slug="dup-slug")
    with pytest.raises(ConflictError) as exc_info:
        await tenant_repo.create(session, name="Dup B", slug="dup-slug")
    assert exc_info.value.code == "tenant_slug_taken"


async def test_create_duplicate_entra_tid_raises_conflict(session: AsyncSession) -> None:
    await tenant_repo.create(session, name="Tid A", slug="tid-a", entra_tenant_id="shared-tid")
    with pytest.raises(ConflictError) as exc_info:
        await tenant_repo.create(session, name="Tid B", slug="tid-b", entra_tenant_id="shared-tid")
    assert exc_info.value.code == "tenant_entra_tid_taken"


# ---- get_by_slug ------------------------------------------------------------------------- #


async def test_get_by_slug_returns_none_when_missing(session: AsyncSession) -> None:
    assert await tenant_repo.get_by_slug(session, "does-not-exist") is None


# ---- update ---------------------------------------------------------------------------- #


async def test_update_name_only(session: AsyncSession) -> None:
    t = await _mk_tenant(session, slug="upd-name")
    updated = await tenant_repo.update(session, t.id, name="New Name")  # type: ignore[arg-type]
    assert updated.name == "New Name"
    assert updated.slug == "upd-name"  # unverändert
    assert updated.is_active is True  # unverändert


async def test_update_entra_tid_only(session: AsyncSession) -> None:
    t = await _mk_tenant(session, slug="upd-tid")
    updated = await tenant_repo.update(
        session,
        t.id,
        entra_tenant_id="new-tid",  # type: ignore[arg-type]
    )
    assert updated.entra_tenant_id == "new-tid"
    assert updated.name == "upd-tid"  # unverändert


async def test_update_is_active_only(session: AsyncSession) -> None:
    t = await _mk_tenant(session, slug="upd-active")
    updated = await tenant_repo.update(session, t.id, is_active=False)  # type: ignore[arg-type]
    assert updated.is_active is False


async def test_update_same_entra_tid_on_same_tenant_is_noop(session: AsyncSession) -> None:
    """Den eigenen unveränderten Wert erneut zu setzen, darf keinen Konflikt mit sich selbst
    auslösen."""
    t = await _mk_tenant(session, slug="upd-self-tid", entra_tenant_id="self-tid")
    updated = await tenant_repo.update(
        session,
        t.id,
        entra_tenant_id="self-tid",  # type: ignore[arg-type]
    )
    assert updated.entra_tenant_id == "self-tid"


async def test_update_duplicate_entra_tid_raises_conflict(session: AsyncSession) -> None:
    await _mk_tenant(session, slug="upd-a", entra_tenant_id="taken-tid")
    b = await _mk_tenant(session, slug="upd-b", entra_tenant_id="other-tid")
    with pytest.raises(ConflictError) as exc_info:
        await tenant_repo.update(session, b.id, entra_tenant_id="taken-tid")  # type: ignore
    assert exc_info.value.code == "tenant_entra_tid_taken"


async def test_update_unknown_tenant_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await tenant_repo.update(session, 999_999, name="whatever")


# ---- delete ------------------------------------------------------------------------------ #


async def test_delete_removes_tenant(session: AsyncSession) -> None:
    t = await _mk_tenant(session, slug="del-me")
    await tenant_repo.delete(session, t.id)  # type: ignore[arg-type]
    assert await tenant_repo.get(session, t.id) is None  # type: ignore[arg-type]


async def test_delete_unknown_tenant_is_noop(session: AsyncSession) -> None:
    await tenant_repo.delete(session, 999_999)  # darf nicht werfen


# ---- list_all ------------------------------------------------------------------------------ #


async def test_list_all_includes_inactive(session: AsyncSession) -> None:
    active = await _mk_tenant(session, slug="list-active")
    inactive = await _mk_tenant(session, slug="list-inactive")
    inactive.is_active = False
    await session.flush()

    ids = {t.id for t in await tenant_repo.list_all(session)}
    assert active.id in ids
    assert inactive.id in ids


async def test_list_all_ordered_by_name(session: AsyncSession) -> None:
    t_z = await _mk_tenant(session, slug="zzz-name")
    t_z.name = "Zeta"
    t_a = await _mk_tenant(session, slug="aaa-name")
    t_a.name = "Alpha"
    await session.flush()

    names = [t.name for t in await tenant_repo.list_all(session)]
    assert names.index("Alpha") < names.index("Zeta")


# ---- count_sso_users ------------------------------------------------------------------------ #


async def test_count_sso_users_counts_only_matching_tenant(session: AsyncSession) -> None:
    a = await _mk_tenant(session, slug="sso-count-a")
    b = await _mk_tenant(session, slug="sso-count-b")
    assert a.id is not None and b.id is not None

    await _mk_sso_user(session, username="sso1@a", tenant_id=a.id)
    await _mk_sso_user(session, username="sso2@a", tenant_id=a.id)
    await _mk_sso_user(session, username="sso1@b", tenant_id=b.id)

    assert await tenant_repo.count_sso_users(session, a.id) == 2
    assert await tenant_repo.count_sso_users(session, b.id) == 1


async def test_count_sso_users_excludes_local_accounts(session: AsyncSession) -> None:
    a = await _mk_tenant(session, slug="sso-count-local")
    assert a.id is not None
    local = AppUser(
        username="local@a", password_hash="x", role="admin", is_sso=False, tenant_id=a.id
    )
    session.add(local)
    await session.flush()

    assert await tenant_repo.count_sso_users(session, a.id) == 0


async def test_count_sso_users_returns_zero_for_unknown_tenant(session: AsyncSession) -> None:
    assert await tenant_repo.count_sso_users(session, 999_999) == 0
