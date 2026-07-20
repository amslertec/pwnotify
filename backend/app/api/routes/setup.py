"""First-Time-Setup-Wizard: DB -> Admin -> Graph -> Mail."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from ...core.config import get_settings
from ...core.errors import ConflictError, ForbiddenError
from ...core.http import client_ip, client_user_agent
from ...core.security import (
    WEAK_PASSWORD_MESSAGE,
    hash_password,
    hash_token,
    issue_token_pair,
    password_meets_policy,
)
from ...db.migrate import run_migrations
from ...repositories import tenant_repo, user_repo
from ...schemas.auth import UserOut
from ...schemas.common import Message
from ...schemas.settings import GraphTestRequest, GraphTestResult, MailTestRequest
from ...services import audit
from ...services.connectivity import send_test_mail, test_graph
from ..deps import (
    SessionDep,
    get_current_user,
    limiter,
    require_admin,
    set_auth_cookies,
)

_settings = get_settings()

router = APIRouter(prefix="/setup", tags=["setup"])


class SetupStatus(BaseModel):
    needs_setup: bool
    has_admin: bool
    database_ready: bool
    graph_configured: bool
    mail_configured: bool


class DatabaseStatus(BaseModel):
    connected: bool
    migrated: bool
    error: str | None = None


class AdminCreate(BaseModel):
    username: str = Field(min_length=3, max_length=150)
    password: str = Field(min_length=10, max_length=1024)
    display_name: str | None = Field(default=None, max_length=320)
    # Optional: benennt den vom Phase-1-Migration bereits angelegten Default-Tenant
    # (Slug 'default') auf den Firmennamen um -- Setup legt ihn NICHT neu an (Design §9.2).
    default_tenant_name: str | None = Field(default=None, min_length=1, max_length=200)


async def _admin_count(session: SessionDep) -> int:
    return await user_repo.count(session)


async def _require_setup_open_or_admin(request: Request, session: SessionDep) -> None:
    """Gate for the setup TEST endpoints (database/graph/mail): open while first-time setup is
    still running (no admin yet), otherwise restricted to an authenticated admin. Sealing a
    provisioned instance -- these endpoints probe Graph, send real mail, and touch the DB, so
    they must not stay world-reachable after setup completes."""
    if await _admin_count(session) == 0:
        return
    user = await get_current_user(request, session)
    await require_admin(user)


SetupGuard = Annotated[None, Depends(_require_setup_open_or_admin)]

# Serialize the unauthenticated first-setup path so two racing requests cannot both pass the
# count==0 guard and create a second superadmin. Transaction-scoped: released when
# user_repo.create commits the INSERT below (or on rollback). A fixed arbitrary key namespaces
# this lock; it does NOT block later authenticated create_superadmin calls (different path).
_SETUP_ADMIN_LOCK_KEY = 0x50_57_4E_01  # "PWN\x01" -- arbitrary, stable


@router.get("/status", response_model=SetupStatus)
@limiter.limit(_settings.setup_rate_limit)
async def status(request: Request, session: SessionDep) -> SetupStatus:
    from ...services.settings_service import SettingsService

    has_admin = (await user_repo.count(session)) > 0
    db_ready = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ready = False
    svc = SettingsService(session)
    graph_ok = await svc.is_configured("graph.tenant_id", "graph.client_id", "graph.client_secret")
    mail_ok = await svc.is_configured("mail.from")
    return SetupStatus(
        needs_setup=not has_admin,
        has_admin=has_admin,
        database_ready=db_ready,
        graph_configured=graph_ok,
        mail_configured=mail_ok,
    )


@router.post("/database/test", response_model=DatabaseStatus)
@limiter.limit(_settings.setup_rate_limit)
async def database_test(request: Request, session: SessionDep, _: SetupGuard) -> DatabaseStatus:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        return DatabaseStatus(
            connected=False, migrated=False, error="Datenbankverbindung fehlgeschlagen."
        )
    migrated = True
    try:
        await session.execute(text("SELECT 1 FROM alembic_version"))
    except Exception:
        migrated = False
    return DatabaseStatus(connected=True, migrated=migrated)


@router.post("/database/migrate", response_model=DatabaseStatus)
@limiter.limit(_settings.setup_rate_limit)
async def database_migrate(request: Request, session: SessionDep) -> DatabaseStatus:
    # Nur während des Setups (kein Admin) frei; danach laufen Migrationen beim Start.
    if await _admin_count(session) > 0:
        raise ConflictError("Setup bereits abgeschlossen.", code="setup_done")
    try:
        await asyncio.to_thread(run_migrations)
    except Exception as exc:
        return DatabaseStatus(connected=True, migrated=False, error=str(exc))
    return DatabaseStatus(connected=True, migrated=True)


@router.post("/admin", response_model=UserOut)
@limiter.limit(_settings.setup_rate_limit)
async def create_admin(
    body: AdminCreate, response: Response, request: Request, session: SessionDep
) -> UserOut:
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SETUP_ADMIN_LOCK_KEY})
    if await _admin_count(session) > 0:
        raise ConflictError("Es existiert bereits ein Administrator.", code="admin_exists")
    # Full server-side password policy (Security Phase 5, Task 2) -- pydantic's
    # `min_length=10` on `AdminCreate.password` is only a floor, not the policy itself.
    if not password_meets_policy(body.password):
        raise ForbiddenError(WEAK_PASSWORD_MESSAGE, code="password_policy")
    # Das erste Setup-Konto ist IMMER der (lokale) Superadmin -- instanzweit, nicht der
    # alte "Drei-Wege"-Admin (Design §9.1). Multi-Tenant-Mode bleibt dabei AUS (in Task 1
    # false geseedet) -- Setup schaltet ihn nicht ein, das macht der Superadmin bewusst
    # später über Settings->General.
    user = await user_repo.create(
        session,
        username=body.username,
        password_hash=hash_password(body.password),
        role="superadmin",
        display_name=body.display_name,
        is_sso=False,
    )
    if body.default_tenant_name is not None:
        # Der Default-Tenant existiert bereits (Phase-1-Migration) -- Setup NENNT ihn nur,
        # legt ihn nicht an. Slug bleibt 'default' (kein Slug-Feld hier, wie in InstanceUpdate).
        default = await tenant_repo.default_tenant(session)
        assert default.id is not None
        await tenant_repo.update(session, default.id, name=body.default_tenant_name)
    # First-setup superadmin creation was previously unaudited entirely (Security Phase 5,
    # Task 8/M10) -- reuse SUPERADMIN_CREATED (same meaning: a superadmin account came into
    # existence), `actor=user` since the account IS its own creator here, `detail` marks it
    # as the first-setup path (vs. a later superadmin invited by `create_superadmin`).
    await audit.record(
        session,
        action=audit.SUPERADMIN_CREATED,
        actor=user,
        target=user.username,
        request=request,
        detail={"first_setup": True},
    )
    # Auto-Login, damit der Wizard nahtlos mit Graph/Mail weitermacht.
    pair = issue_token_pair(str(user.id))
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=client_user_agent(request),
        ip=client_ip(request),
    )
    set_auth_cookies(response, pair)
    return UserOut.model_validate(user, from_attributes=True)


@router.post("/graph/test", response_model=GraphTestResult)
@limiter.limit(_settings.setup_rate_limit)
async def graph_test(
    request: Request, body: GraphTestRequest, session: SessionDep, _: SetupGuard
) -> GraphTestResult:
    from ...services.settings_service import SettingsService

    settings = await SettingsService(session).get_all()
    result = await test_graph(
        settings,
        tenant_id=body.tenant_id,
        client_id=body.client_id,
        client_secret=body.client_secret,
        cloud=body.cloud,
    )
    return GraphTestResult(**result.__dict__)


@router.post("/mail/test", response_model=Message)
@limiter.limit(_settings.setup_rate_limit)
async def mail_test(
    request: Request, body: MailTestRequest, session: SessionDep, _: SetupGuard
) -> Message:
    from ...services.settings_service import SettingsService, effective_base_url

    settings = await SettingsService(session).get_all()
    await send_test_mail(
        settings, to=body.to, locale=body.locale, base_url=effective_base_url(settings)
    )
    return Message(message=f"Test-Mail an {body.to} versendet.")
