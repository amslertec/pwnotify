"""First-Time-Setup-Wizard: DB -> Admin -> Graph -> Mail."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from ...core.errors import ConflictError
from ...core.security import hash_password, hash_token, issue_token_pair
from ...db.migrate import run_migrations
from ...repositories import user_repo
from ...schemas.auth import UserOut
from ...schemas.common import Message
from ...schemas.settings import GraphTestRequest, GraphTestResult, MailTestRequest
from ...services.connectivity import send_test_mail, test_graph
from ..deps import SessionDep, set_auth_cookies

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


async def _admin_count(session: SessionDep) -> int:
    return await user_repo.count(session)


@router.get("/status", response_model=SetupStatus)
async def status(session: SessionDep) -> SetupStatus:
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
async def database_test(session: SessionDep) -> DatabaseStatus:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        return DatabaseStatus(connected=False, migrated=False, error=str(exc))
    migrated = True
    try:
        await session.execute(text("SELECT 1 FROM alembic_version"))
    except Exception:
        migrated = False
    return DatabaseStatus(connected=True, migrated=migrated)


@router.post("/database/migrate", response_model=DatabaseStatus)
async def database_migrate(session: SessionDep) -> DatabaseStatus:
    # Nur während des Setups (kein Admin) frei; danach laufen Migrationen beim Start.
    if await _admin_count(session) > 0:
        raise ConflictError("Setup bereits abgeschlossen.", code="setup_done")
    try:
        await asyncio.to_thread(run_migrations)
    except Exception as exc:
        return DatabaseStatus(connected=True, migrated=False, error=str(exc))
    return DatabaseStatus(connected=True, migrated=True)


@router.post("/admin", response_model=UserOut)
async def create_admin(
    body: AdminCreate, response: Response, request: Request, session: SessionDep
) -> UserOut:
    if await _admin_count(session) > 0:
        raise ConflictError("Es existiert bereits ein Administrator.", code="admin_exists")
    user = await user_repo.create(
        session,
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    # Auto-Login, damit der Wizard nahtlos mit Graph/Mail weitermacht.
    pair = issue_token_pair(str(user.id))
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    set_auth_cookies(response, pair)
    return UserOut.model_validate(user, from_attributes=True)


@router.post("/graph/test", response_model=GraphTestResult)
async def graph_test(body: GraphTestRequest, session: SessionDep) -> GraphTestResult:
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
async def mail_test(body: MailTestRequest, session: SessionDep) -> Message:
    from ...services.settings_service import SettingsService, effective_base_url

    settings = await SettingsService(session).get_all()
    await send_test_mail(
        settings, to=body.to, locale=body.locale, base_url=effective_base_url(settings)
    )
    return Message(message=f"Test-Mail an {body.to} versendet.")
