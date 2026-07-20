"""Security remediation M4 + M7 + L10: hardening of three `settings.py` routes.

Driven end-to-end through the real ASGI app (`create_app()` + `httpx.ASGITransport`, no
lifespan -- the `migrated_engine` fixture has already migrated the test DB and redirected
`PWNOTIFY_DATABASE_URL` for the run, mirroring `test_public_tokens_ratelimit.py`). Only the
full app proves the actual dependency wiring (M4 admin-gate), the slowapi rate limit (M7,
which a direct function call cannot exercise) and the pydantic 422 (L10).

- **M4** `/template/preview` is now `AdminUser` (an auditor -> 403) and guards against
  resource exhaustion (wall-clock timeout + output-size ceiling -> 4xx, no hang).
- **M7** `/mail/test` now writes an audit row (recipient in `detail`) and is rate limited.
- **L10** `POST /exclusions` takes a typed body: missing `value` / invalid `kind` -> 422.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, limiter
from app.api.routes import settings as settings_route
from app.core.config import get_settings
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.main import create_app
from app.repositories import tenant_repo, user_repo
from app.schemas.settings import ExclusionCreate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture(autouse=True)
def _limiter_reset_disabled() -> Iterator[None]:
    """Reset the shared in-memory counter around every test and keep the limiter DISABLED by
    default -- only the dedicated 429 test re-enables it. The `Limiter` is a module singleton
    (`app.api.deps.limiter`) shared with every other suite in the run."""
    prev = limiter.enabled
    limiter.enabled = False
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()
        limiter.enabled = prev


def _slug() -> str:
    return f"srh-{uuid.uuid4().hex[:10]}"


def _uname(label: str) -> str:
    return f"srh-{label}-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def env(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, Any]]:
    """A committed tenant + a write-granted local admin + a read-only local auditor on it."""
    factory = get_session_factory()
    async with factory() as session:
        tenant = await tenant_repo.create(session, name="SRH Tenant", slug=_slug())
        assert tenant.id is not None
        tid = tenant.id
        admin = await user_repo.create(
            session, username=_uname("admin"), password_hash="x", role="admin", is_sso=False
        )
        auditor = await user_repo.create(
            session, username=_uname("auditor"), password_hash="x", role="auditor", is_sso=False
        )
        assert admin.id is not None and auditor.id is not None
        await tenant_repo.add_grant(session, user_id=admin.id, tenant_id=tid, kind="admin")
        await tenant_repo.add_grant(session, user_id=auditor.id, tenant_id=tid, kind="auditor")
        admin_id, auditor_id = admin.id, auditor.id

    try:
        yield {"tid": tid, "admin_id": admin_id, "auditor_id": auditor_id}
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM exclusion WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM setting WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM admin_tenant WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM auditor_tenant WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(
                text("DELETE FROM app_user WHERE id IN (:a, :b)"),
                {"a": admin_id, "b": auditor_id},
            )
            await conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tid})
            await conn.commit()


def _client(uid: int, tid: int) -> httpx.AsyncClient:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    token = issue_token_pair(str(uid), active_tenant=tid).access_token
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies={ACCESS_COOKIE: token}
    )


# ---- M4: template/preview ---------------------------------------------------------------- #


async def test_template_preview_forbidden_for_auditor(env: dict[str, Any]) -> None:
    """RED against the old code (route was `CurrentUser` -> 200 for an auditor)."""
    async with _client(env["auditor_id"], env["tid"]) as client:
        resp = await client.post(
            "/api/settings/template/preview",
            json={"subject": "Hallo {{ displayName }}", "html": "<b>{{ displayName }}</b>"},
        )
    assert resp.status_code == 403, resp.text


async def test_template_preview_ok_for_admin(env: dict[str, Any]) -> None:
    async with _client(env["admin_id"], env["tid"]) as client:
        resp = await client.post(
            "/api/settings/template/preview",
            json={"subject": "Hallo {{ displayName }}", "html": "<b>{{ displayName }}</b>"},
        )
    assert resp.status_code == 200, resp.text
    assert "Erika" in resp.json()["subject"]


async def test_template_preview_rejects_oversized_output(env: dict[str, Any]) -> None:
    """Output-size ceiling: a huge-output expression is rejected with a clean 4xx, not
    returned. RED against the old code (unbounded output)."""
    async with _client(env["admin_id"], env["tid"]) as client:
        resp = await client.post(
            "/api/settings/template/preview",
            json={"subject": "ok", "html": "{{ 'x' * 300000 }}"},
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "template_preview_too_expensive"


async def test_template_preview_aborts_expensive_template(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wall-clock timeout: a render that exceeds the budget is aborted with a clean 4xx and
    the request returns immediately (RED against the old code, which had no timeout at all).

    A real heavy template cannot drive this here -- the Jinja sandbox caps `range()` at
    100000 and rejects a huge loop outright -- so the render itself is replaced by a blocking
    stand-in (mirroring the unbounded CPU work a nested loop would do) and the timeout is set
    tiny, proving the wrapper aborts a long render without a runaway CPU thread or a 30 s wait."""

    def _slow_render(*args: Any, **kwargs: Any) -> str:
        import time

        time.sleep(0.5)
        return ""

    monkeypatch.setattr(settings_route, "render", _slow_render)
    monkeypatch.setattr(settings_route, "_PREVIEW_TIMEOUT_S", 0.05)
    async with _client(env["admin_id"], env["tid"]) as client:
        resp = await client.post(
            "/api/settings/template/preview",
            json={"subject": "ok", "html": "irgendwas"},
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "template_preview_too_expensive"


# ---- M7: mail/test ----------------------------------------------------------------------- #


async def test_mail_test_records_audit(
    env: dict[str, Any], migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED against the old code (no `audit.record` at all on `/mail/test`)."""

    async def _fake_send(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(settings_route, "send_test_mail", _fake_send)

    async with _client(env["admin_id"], env["tid"]) as client:
        resp = await client.post(
            "/api/settings/mail/test", json={"to": "audit-target@example.com", "locale": "de"}
        )
    assert resp.status_code == 200, resp.text

    async with migrated_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT target, detail FROM audit_log "
                    "WHERE action = :a AND tenant_id = :t ORDER BY id DESC LIMIT 1"
                ),
                {"a": "settings.mail_test_sent", "t": env["tid"]},
            )
        ).one_or_none()
    assert row is not None, "mail/test did not write an audit_log row (RED without the fix)"
    assert row.target == "audit-target@example.com"
    assert row.detail.get("to") == "audit-target@example.com"


async def test_mail_test_rate_limited(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """RED against the old code (no rate limit -> unbounded external send)."""

    async def _fake_send(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(settings_route, "send_test_mail", _fake_send)

    limit = int(get_settings().mail_test_rate_limit.split("/", 1)[0])
    limiter.enabled = True
    limiter.reset()

    statuses: list[int] = []
    async with _client(env["admin_id"], env["tid"]) as client:
        for _ in range(limit + 2):
            resp = await client.post(
                "/api/settings/mail/test", json={"to": "rl@example.com", "locale": "de"}
            )
            statuses.append(resp.status_code)

    assert 429 in statuses, f"rate limit never triggered -- statuses: {statuses}"
    assert statuses[:limit] == [200] * limit, statuses


# ---- L10: typed exclusion body ----------------------------------------------------------- #


async def test_add_exclusion_missing_value_returns_422(env: dict[str, Any]) -> None:
    """RED against the old code (`body["value"]` KeyError -> unhandled 500)."""
    async with _client(env["admin_id"], env["tid"]) as client:
        resp = await client.post("/api/settings/exclusions", json={"kind": "user"})
    assert resp.status_code == 422, resp.text


async def test_add_exclusion_invalid_kind_returns_422(env: dict[str, Any]) -> None:
    async with _client(env["admin_id"], env["tid"]) as client:
        resp = await client.post(
            "/api/settings/exclusions", json={"kind": "bogus", "value": "x@example.com"}
        )
    assert resp.status_code == 422, resp.text


async def test_add_exclusion_valid_request_ok(env: dict[str, Any]) -> None:
    async with _client(env["admin_id"], env["tid"]) as client:
        resp = await client.post(
            "/api/settings/exclusions",
            json={"kind": "user", "value": "keep@example.com", "label": "VIP"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["value"] == "keep@example.com"
    assert body["kind"] == "user"


def test_exclusion_create_defaults_kind_to_user() -> None:
    """`kind` defaults to "user" (matches the old `body.get("kind", "user")` semantics)."""
    model = ExclusionCreate(value="x@example.com")
    assert model.kind == "user"
