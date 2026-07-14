"""Einmaliger Seed aus ENV beim allerersten Start (danach: DB ist die Quelle)."""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .core.config import get_settings
from .core.logging import get_logger
from .core.security import hash_password
from .repositories import user_repo
from .services.settings_service import SettingsService

log = get_logger("seed")


def _env_to_settings(s: Any) -> dict[str, Any]:
    """Bildet nicht-leere ENV-Werte auf die DB-Setting-Keys ab."""
    mapping: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value not in (None, ""):
            mapping[key] = value

    put("graph.tenant_id", s.graph_tenant_id)
    put("graph.client_id", s.graph_client_id)
    put("graph.client_secret", s.graph_client_secret)
    put("graph.cloud", s.graph_cloud)
    put("mail.backend", s.mail_backend)
    put("mail.from", s.mail_from)
    put("mail.recipient_strategy", s.mail_recipient_strategy)
    put("mail.smtp_host", s.smtp_host)
    put("mail.smtp_port", s.smtp_port)
    put("mail.smtp_username", s.smtp_username)
    put("mail.smtp_password", s.smtp_password)
    put("mail.smtp_tls", s.smtp_tls)
    put("schedule.cron", s.schedule_cron)
    put("schedule.timezone", s.timezone)
    if s.reminder_days:
        with contextlib.suppress(ValueError):
            mapping["schedule.reminder_days"] = [
                int(x.strip()) for x in s.reminder_days.split(",") if x.strip() != ""
            ]
    mapping["schedule.dry_run"] = bool(s.dry_run)
    put("policy.validity_days_override", s.password_validity_days)
    put("branding.app_name", s.app_name)
    put("branding.company_name", s.company_name)
    put("branding.primary_color", s.primary_color)
    put("branding.reset_url", s.reset_url)
    return mapping


async def run_seed(session_factory: async_sessionmaker[AsyncSession]) -> None:
    settings = get_settings()
    async with session_factory() as session:
        svc = SettingsService(session)
        # 1) Settings nur beim allerersten Start seeden.
        if not await svc.has_any():
            values = _env_to_settings(settings)
            if values:
                await svc.set_many(values)
                log.info("settings_seeded", keys=len(values))

        # 2) Admin aus ENV anlegen, falls konfiguriert und noch keiner existiert.
        if (
            settings.admin_username
            and settings.admin_password
            and await user_repo.count(session) == 0
        ):
            await user_repo.create(
                session,
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
            )
            log.info("admin_seeded", username=settings.admin_username)
