"""Async-SQLAlchemy-Engine + Session-Factory (asyncpg)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..core.config import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )

        from sqlalchemy import event

        event.listen(_engine.sync_engine, "begin", _begin_tenant_wrapper)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _factory


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI-Dependency: liefert eine Session pro Request."""
    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None


def _begin_tenant_wrapper(conn: object) -> None:
    """SQLAlchemy 'begin'-Event-Callback (läuft auf der Sync-Engine, auch für AsyncEngine --
    die asyncio-Bridge führt den Callback im selben Greenlet wie den umgebenden async-Aufruf
    aus). `conn` ist eine Core-`Connection`; die rohe DBAPI-Verbindung trägt der Cursor,
    über den `SET LOCAL ROLE`/GUC gesetzt werden.

    Lokaler Import von `tenant_context`, um den Zirkularimport zu vermeiden
    (`tenant_context` importiert `get_session_factory` aus diesem Modul).
    """
    from .tenant_context import apply_tenant_on_begin

    apply_tenant_on_begin(conn.connection.dbapi_connection, None)  # type: ignore[attr-defined]
