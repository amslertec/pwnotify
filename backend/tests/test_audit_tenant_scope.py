"""Fix 1 (Task 5 whole-branch review, CRITICAL): `/audit`-Leserouten liefen bisher immer
auf der Owner-Session -- `audit_log` ist eine RLS-tenant-gescopte Tabelle, und die
Owner-Rolle umgeht RLS vollständig. Ein SSO-Admin, gebunden an Tenant B, konnte damit das
GESAMTE Protokoll aller Mandanten lesen (Cross-Tenant-Offenlegung).

`get_audit_session` (app/api/deps.py) unterscheidet jetzt:
- LOKALER Admin (`not is_sso`, `role == "admin"`) -> Owner-Session, sieht ALLE Mandanten
  (Design §2).
- Jedes andere, mandantengebundene Konto (jedes SSO-Konto, gleich welche Rolle, ODER ein
  lokaler Auditor) -> tenant-gescopte Session, sieht NUR sein autorisiertes aktives
  Mandanten-Protokoll.

Der Kernbeweis unten (`test_sso_admin_bound_to_b_cannot_see_tenant_a_audit_rows`) muss
fehlschlagen, wenn der SSO-Admin B weiterhin A's Zeilen sehen könnte.

Seed-Pattern wie in `test_isolation_attack.py`/`test_route_tenant_scoping.py`: echte,
committete Superuser-Connection (RLS-frei) für Setup -- der tenant-gescopte Zweig von
`get_audit_session` öffnet über `tenant_scoped_session` eine EIGENE Verbindung und sähe
unbestätigte Daten der savepoint-isolierten `session`-Fixture nicht.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, get_audit_session, get_current_user
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.repositories import audit_repo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


class _FakeRequest:
    """Duck-typed Request -- `get_current_user`/`get_audit_session` lesen nur `.cookies`."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


@pytest_asyncio.fixture
async def audit_seed(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, int]]:
    """Zwei Tenants (A, B) + je eine Audit-Zeile + ein lokaler Admin (kein Tenant gebunden)
    + ein SSO-Admin, gebunden an Tenant B -- alles echt committet (Superuser-Connection,
    RLS-frei), Cleanup im `finally`."""
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                "('AudA','aud-a',true,now()), ('AudB','aud-b',true,now())"
            )
        )
        a, b = (
            (
                await conn.execute(
                    text("SELECT id FROM tenant WHERE slug IN ('aud-a','aud-b') ORDER BY slug")
                )
            )
            .scalars()
            .all()
        )
        await conn.execute(
            text(
                "INSERT INTO audit_log (tenant_id, at, actor_type, action, outcome, detail) "
                "VALUES "
                "(:a, now(), 'system', 'test.aud_a_event', 'success', '{}'::jsonb), "
                "(:b, now(), 'system', 'test.aud_b_event', 'success', '{}'::jsonb)"
            ),
            {"a": a, "b": b},
        )
        local_admin_id = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO app_user "
                        "(username, password_hash, role, is_active, is_sso, "
                        "failed_login_count, language, created_at, updated_at) VALUES "
                        "('aud-local-admin@local', 'x', 'admin', true, false, 0, 'de', "
                        "now(), now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        sso_admin_id = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO app_user "
                        "(username, password_hash, role, is_active, is_sso, tenant_id, "
                        "failed_login_count, language, created_at, updated_at) VALUES "
                        "('aud-sso-admin@b', 'x', 'admin', true, true, :b, 0, 'de', "
                        "now(), now()) RETURNING id"
                    ),
                    {"b": b},
                )
            ).scalar_one()
        )
        await conn.commit()
        try:
            yield {"a": a, "b": b, "local_admin_id": local_admin_id, "sso_admin_id": sso_admin_id}
        finally:
            await conn.execute(
                text(
                    "DELETE FROM audit_log WHERE action IN ('test.aud_a_event', 'test.aud_b_event')"
                )
            )
            await conn.execute(
                text("DELETE FROM app_user WHERE id IN (:x, :y)"),
                {"x": local_admin_id, "y": sso_admin_id},
            )
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


@contextlib.asynccontextmanager
async def _audit_session_for(
    uid: int, *, active_tenant: int | None = None
) -> AsyncGenerator[AsyncSession]:
    """Treibt `get_audit_session` exakt wie FastAPI es pro Request täte: echtes Access-Token
    für `uid` (mit optionalem `active_tenant`-Claim), eine Owner-Session für
    `get_current_user`, dann die (ggf. tenant-gescopte) Audit-Session."""
    pair = issue_token_pair(str(uid), active_tenant=active_tenant)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_audit_session(request, user, owner)
        try:
            yield await anext(gen)
        finally:
            await gen.aclose()


async def test_local_admin_sees_all_tenants_audit_rows(audit_seed: dict[str, int]) -> None:
    """Design §2: der lokale Admin bleibt auf der Owner-Session -- er sieht das gesamte
    Protokoll, über alle Mandanten hinweg."""
    async with _audit_session_for(audit_seed["local_admin_id"]) as session:
        rows, _total = await audit_repo.list_paged(session, page=1, page_size=200)
    actions = {r.action for r in rows}
    assert "test.aud_a_event" in actions, "Lokaler Admin sah Tenant A's Zeile nicht"
    assert "test.aud_b_event" in actions, "Lokaler Admin sah Tenant B's Zeile nicht"


async def test_sso_admin_bound_to_b_cannot_see_tenant_a_audit_rows(
    audit_seed: dict[str, int],
) -> None:
    """Der Kernbeweis: ein SSO-Admin, gebunden an Tenant B, darf NUR B's Protokoll sehen --
    NICHT A's. Das ist genau die Cross-Tenant-Offenlegung, die Fix 1 schliesst."""
    b = audit_seed["b"]
    async with _audit_session_for(audit_seed["sso_admin_id"], active_tenant=b) as session:
        rows, _total = await audit_repo.list_paged(session, page=1, page_size=200)
    actions = {r.action for r in rows}
    assert "test.aud_b_event" in actions, "SSO-Admin B sah seine eigene Zeile nicht"
    assert "test.aud_a_event" not in actions, (
        "Cross-Tenant-Leck: SSO-Admin B konnte Tenant A's Audit-Zeilen sehen"
    )


async def test_sso_admin_without_claim_still_scoped_to_own_tenant(
    audit_seed: dict[str, int],
) -> None:
    """Randfall: kein `active_tenant`-Claim im Token (z. B. älteres Token) -- die
    Auflösung fällt auf `resolve_initial_tenant` zurück, landet für ein SSO-Konto aber
    ebenso auf dessen eigenem, gebundenem Tenant. Kein impliziter Owner-Fallback."""
    async with _audit_session_for(audit_seed["sso_admin_id"]) as session:
        rows, _total = await audit_repo.list_paged(session, page=1, page_size=200)
    actions = {r.action for r in rows}
    assert "test.aud_b_event" in actions
    assert "test.aud_a_event" not in actions
