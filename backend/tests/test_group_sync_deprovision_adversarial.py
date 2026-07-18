"""Task 3 (Tenant-Refinements) -- SICHERHEITSKRITISCH: das Deprovision-Cleanup im proaktiven
Entra-Gruppen-Sync (`services/group_sync.sync_group`). Wenn ein Provider-SSO-Konto durch den
Sync vollständig deprovisioniert wird (aus allen Teams raus, keine Grant-Zeile mehr), wird die
`app_user`-Zeile GELÖSCHT -- keine "Leiche". Das Löschen feuert NUR unter dem vollständigen
Fail-Safe-Gate (`_is_fully_deprovisioned`); eine einzige nicht erfüllte Bedingung MUSS das Konto
behalten.

Angriffs-orientiert (wie `test_group_sync_adversarial.py`): die Tests treiben `sync_group` mit
einem gefälschten `get_group_members` an und behaupten DIREKT auf den `app_user`-,
`admin_tenant`/`auditor_tenant`- und `user_session`-Tabellen an echtem Postgres (RLS wirkt mit).

Jeder KEPT-Fall schwächt genau EINE Gate-Bedingung ab und würde ROT, wenn der Gate diese
Bedingung fallen liesse. Der DELETED-Fall würde ROT, wenn der Löschpfad fehlte.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser, UserSession
from app.repositories import assignment_group_member_repo as member_repo
from app.repositories import assignment_group_repo, tenant_repo
from app.services import group_sync
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# ---- Seeding helpers (mirror test_group_sync_adversarial.py) ------------------------------- #


def _slug() -> str:
    return f"t3-depr-{uuid.uuid4().hex[:10]}"


def _entra() -> str:
    return f"grp-{uuid.uuid4().hex}"


def _member(upn: str) -> dict[str, Any]:
    return {
        "id": f"eid-{uuid.uuid4().hex}",
        "userPrincipalName": upn,
        "displayName": upn,
        "mail": upn,
    }


async def _mk_tenant(session: AsyncSession) -> Tenant:
    return await tenant_repo.create(session, name="Depr Tenant", slug=_slug())


async def _mk_user(
    session: AsyncSession, *, upn: str, role: str, tenant_id: int | None, is_sso: bool = True
) -> AppUser:
    u = AppUser(username=upn, password_hash="x", role=role, is_sso=is_sso, tenant_id=tenant_id)
    session.add(u)
    await session.flush()
    return u


async def _mk_team(
    session: AsyncSession, tenant_ids: list[int], *, role: str = "admin"
) -> tuple[int, str]:
    entra = _entra()
    group = await assignment_group_repo.create(
        session, name="Team", entra_group_id=entra, role=role
    )
    assert group.id is not None
    await assignment_group_repo.set_tenants(session, group.id, tenant_ids)
    return group.id, entra


async def _mk_session(session: AsyncSession, user_id: int) -> None:
    """Eine (nicht widerrufene) Refresh-Session -- muss beim Löschen mit verschwinden."""
    row = UserSession(
        user_id=user_id,
        refresh_jti=uuid.uuid4().hex,
        token_hash=uuid.uuid4().hex,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
    )
    session.add(row)
    await session.flush()


async def _user_exists(session: AsyncSession, user_id: int) -> bool:
    return (await session.get(AppUser, user_id)) is not None


async def _session_count(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(UserSession).where(UserSession.user_id == user_id)
            )
        ).scalar_one()
    )


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


class _FakeGraph:
    def __init__(self, members_by_entra: dict[str, list[dict[str, Any]]]) -> None:
        self._members = members_by_entra

    async def get_group_members(self, entra_group_id: str) -> list[dict[str, Any]]:
        return list(self._members.get(entra_group_id, []))


def _patch_graph(
    monkeypatch: pytest.MonkeyPatch, members_by_entra: dict[str, list[dict[str, Any]]]
) -> None:
    monkeypatch.setattr(group_sync, "GraphClient", lambda _cfg: _FakeGraph(members_by_entra))


# ---- FULL DEPROVISION -> the app_user row (and its sessions) is DELETED -------------------- #


async def test_full_deprovision_deletes_account_and_sessions(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P: is_sso, homed default, only in T, holds only a source='group' grant on A (via T),
    plus a live refresh session. Removing P from T's Graph members -> after sync_group(T):
    snapshot gone, group grant revoked, app_user P DELETED, P's user_session rows gone."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"provider-{uuid.uuid4().hex}@provider.example"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None
    await _mk_session(session, p.id)

    # Sync 1: P present -> group grant on A materialized.
    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {
        (tenant_a.id, "group")
    }
    assert await _session_count(session, p.id) == 1

    # Sync 2: P removed from T -> fully deprovisioned -> DELETED.
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert upn not in await member_repo.upns_for_group(session, t_id)  # snapshot gone
    assert await _admin_rows(session, p.id) == []  # group grant revoked
    assert not await _user_exists(session, p.id)  # app_user DELETED
    assert await _session_count(session, p.id) == 0  # user_session rows gone


# ---- MANUAL grant on the account -> KEPT (manual grant blocks deletion) -------------------- #


async def test_manual_grant_blocks_deletion(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P also holds a source='manual' admin grant on A. Removed from T -> the group grant is
    revoked, but the manual grant persists in the grant table -> P is NOT deleted."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"provider-{uuid.uuid4().hex}@provider.example"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None
    # Manual grant on a DISTINCT tenant B (a group row on A would collide with the group grant
    # on the composite PK -- manual precedence means no group row ever forms there).
    await tenant_repo.add_grant(
        session, user_id=p.id, tenant_id=tenant_b.id, kind="admin", source="manual"
    )

    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)

    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    # The manual B grant survives (source='group' on A revoked) and BLOCKS the delete.
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {
        (tenant_b.id, "manual")
    }
    assert await _user_exists(session, p.id)


async def test_manual_auditor_grant_blocks_deletion(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders on the SECOND grant table: a source='manual' AUDITOR grant must also
    block the delete (the gate checks BOTH admin and auditor grant tables)."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"provider-{uuid.uuid4().hex}@provider.example"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None
    await tenant_repo.add_grant(
        session, user_id=p.id, tenant_id=tenant_b.id, kind="auditor", source="manual"
    )

    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert await _admin_rows(session, p.id) == []
    assert {(r.tenant_id, r.source) for r in await _auditor_rows(session, p.id)} == {
        (tenant_b.id, "manual")
    }
    assert await _user_exists(session, p.id)


# ---- Still in ANOTHER team snapshot -> KEPT ------------------------------------------------ #


async def test_other_team_membership_blocks_deletion(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolates gate condition (4) (`groups_containing_upn` non-empty -> KEPT) from condition
    (5) (holds a grant row -> KEPT), which a naive setup would leave conflated.

    T2 is a ZERO-TENANT team (`_mk_team(session, [])`): P is added to its Graph membership, so
    it lands in T2's local snapshot, but `set_tenants` maps T2 to no customer at all, so
    `reconcile_group_grants` materializes NOTHING for T2 -- T2 membership grants P no grant row
    on any tenant. P's ONLY grant is the T-derived group grant on A, which is revoked the
    moment P leaves T. So right after the sync: P holds ZERO grant rows in either grant table
    (condition 5 would NOT block the delete on its own) yet P still appears in T2's snapshot
    (condition 4 alone blocks it). This isolates (4): if a developer deleted condition (4) from
    `_is_fully_deprovisioned`, conditions 1/2/3/5 would all pass (P is_sso, non-superadmin,
    provider-homed, and grant-less) and P WOULD be deleted -- this test would go RED."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])
    t2_id, t2 = await _mk_team(session, [])  # zero-tenant team -- membership only, no grant.

    upn = f"provider-{uuid.uuid4().hex}@provider.example"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None

    # P is in both T and T2.
    _patch_graph(monkeypatch, {t: [_member(upn)], t2: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    await group_sync.sync_group(session, {}, t2_id)
    # T2 maps no tenant -> P's T2 membership produced no grant row.
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {
        (tenant_a.id, "group")
    }

    # Remove P from T only; T2 still fetches P.
    _patch_graph(monkeypatch, {t: [], t2: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)

    # T2's snapshot still holds P -> groups_containing_upn non-empty -> condition (4) KEEPS P.
    assert await member_repo.groups_containing_upn(session, upn)
    assert upn in await member_repo.upns_for_group(session, t2_id)
    assert await _user_exists(session, p.id)
    # And P holds NO grant row anywhere -- condition (5) alone would NOT have blocked the
    # delete; only condition (4) is doing the work here.
    assert await _admin_rows(session, p.id) == []
    assert await _auditor_rows(session, p.id) == []


# ---- Customer-homed account -> NEVER deleted (not a provider account) ---------------------- #


async def test_customer_homed_account_never_deleted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A customer-A-homed SSO account that (mis)appears in T's snapshot: is_provider_account is
    False -> it can never gain a group grant AND can never be deleted."""
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"customer-{uuid.uuid4().hex}@a.example"
    cust = await _mk_user(session, upn=upn, role="admin", tenant_id=tenant_a.id)
    assert cust.id is not None

    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert await _user_exists(session, cust.id)  # NEVER deleted -- not a provider account.


# ---- Local account / superadmin colliding with an ex-member UPN -> NEVER deleted ---------- #


async def test_local_account_colliding_upn_never_deleted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LOCAL account (is_sso False) whose username exactly collides with an ex-member UPN is
    never deleted -- condition (1) short-circuits before any provider/grant check."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"local-{uuid.uuid4().hex}@x.example"
    # is_sso False; even homed on default and matching the UPN, it must survive.
    local = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id, is_sso=False)
    assert local.id is not None

    # Seed the snapshot directly with the UPN, then sync with an empty group so the UPN is an
    # ex-member (old - new) candidate for the cleanup.
    await member_repo.reconcile_snapshot(session, t_id, [_member(upn)])
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert await _user_exists(session, local.id)  # local account NEVER deleted.


async def test_superadmin_colliding_upn_never_deleted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superadmin (not is_sso, role='superadmin') whose username collides with an ex-member
    UPN is never deleted -- both condition (1) and (2) protect it."""
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"root-{uuid.uuid4().hex}@x.example"
    su = await _mk_user(session, upn=upn, role="superadmin", tenant_id=None, is_sso=False)
    assert su.id is not None

    await member_repo.reconcile_snapshot(session, t_id, [_member(upn)])
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert await _user_exists(session, su.id)  # superadmin NEVER deleted.


# ---- A member who REMAINS in the group is not even a cleanup candidate --------------------- #


async def test_remaining_member_not_a_candidate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member still present in T (in new_upns) is never in old_upns - new_upns, so it is not
    even considered for deletion -- even a fully-provider account stays put."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"provider-{uuid.uuid4().hex}@provider.example"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None

    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    # Second sync, still a member -> stays.
    await group_sync.sync_group(session, {}, t_id)

    assert await _user_exists(session, p.id)
    assert upn in await member_repo.upns_for_group(session, t_id)
