"""Opake Einmal-Tokens für Einladung + Passwort-Reset: Mint + Branding-Mail-Versand.

**Bewusst KEIN JWT** (siehe `core/security.py` -- Access/Refresh/2FA sind zustandslos): ein
Einladungs-/Reset-Link muss DB-gestützt, einmal verwendbar UND widerrufbar sein, was ein
stateless JWT nicht leisten kann. Der Klartext (`secrets.token_urlsafe(32)`) reist NUR in
der E-Mail-URL -- gespeichert wird ausschliesslich `hash_token(raw)` (sha256 hex, wie
`UserSession.token_hash`), s. `repositories/user_token_repo.py`/`models/token.py`.

Verifikation (Aufrufer: `api/routes/public_tokens.py`) läuft ausschliesslich über
`user_token_repo.get_live_by_hash` -- unverbraucht + nicht abgelaufen + passender `purpose`,
sonst IMMER derselbe generische Fehlschlag (keine Enumeration).
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.security import hash_token
from ..db.tenant_context import tenant_scoped_session
from ..models._base import utcnow
from ..models.user import AppUser
from ..repositories import user_token_repo
from .mail import build_sender
from .settings_service import SettingsService, effective_base_url
from .templating import email_logo, render

# §7c: Einladung 7 Tage, Reset 1 Stunde -- der Service setzt die Spanne, nicht die Tabelle
# (siehe Docstring in `models/token.py`).
INVITE_TTL = dt.timedelta(days=7)
RESET_TTL = dt.timedelta(hours=1)

Purpose = Literal["invite", "reset"]


def _locale(settings: dict[str, object]) -> str:
    value = str(settings.get("template.language_default") or "de").lower()
    return "en" if value.startswith("en") else "de"


async def _mint(
    session: AsyncSession, *, user: AppUser, purpose: Purpose, created_by: int, ttl: dt.timedelta
) -> str:
    assert user.id is not None  # persistiertes Konto, hat also eine id
    raw = secrets.token_urlsafe(32)
    await user_token_repo.create(
        session,
        app_user_id=user.id,
        purpose=purpose,
        token_hash=hash_token(raw),
        expires_at=utcnow() + ttl,
        created_by=created_by,
    )
    return raw


async def _send(user: AppUser, *, purpose: Purpose, raw: str) -> None:
    """Branding-Mail im HEIM-Tenant-Kontext des Kontos.

    `setting` ist RLS-tenant-gescopt -- die übergebene Owner-Session (Admin-Route) würde
    als Owner-Rolle ALLE Tenants mischen (s. `SettingsService.get_all`'s `select(Setting)`
    ohne WHERE). Exakt dasselbe Muster wie `admin_users.sync_sso`: eine EIGENE
    `tenant_scoped_session` statt der Owner-Session, nur für den lesenden Settings-Zugriff
    + Versand, kein Schreibzugriff hier.
    """
    if user.tenant_id is None:
        # Sollte nach Task 3/5 nie vorkommen (jedes lokale Konto hat eine Heimat) --
        # defensiv: das Token bleibt trotzdem gültig, nur der automatische Versand entfällt.
        return
    assert user.email is not None  # von der aufrufenden Route bereits erzwungen

    async with tenant_scoped_session(user.tenant_id) as tsession:
        settings = await SettingsService(tsession).get_all()
        base_url = effective_base_url(settings)
        locale = _locale(settings)
        logo_url, inline_images = email_logo(settings, base_url, get_settings().static_dir)

        path = "einladung" if purpose == "invite" else "passwort-neu"
        action_url = f"{base_url}/{path}?token={raw}"
        context: dict[str, object] = {
            "email": user.email,
            "inviteUrl": action_url if purpose == "invite" else "",
            "resetUrl": action_url if purpose == "reset" else "",
            "companyName": settings.get("branding.company_name") or "",
            "appName": settings.get("branding.app_name") or "PwNotify",
            "logoUrl": logo_url,
            "primaryColor": settings.get("branding.primary_color") or "#4F46E5",
        }
        subject = render(str(settings[f"template.{purpose}_subject_{locale}"]), context, html=False)
        html_body = render(str(settings[f"template.{purpose}_html_{locale}"]), context, html=True)
        text_body = render(str(settings[f"template.{purpose}_text_{locale}"]), context, html=False)

        sender = build_sender(settings)
        await sender.send(
            to=[user.email],
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            inline_images=inline_images,
        )


async def issue_invite(session: AsyncSession, *, user: AppUser, created_by: int) -> None:
    """Mint ein `purpose='invite'`-Token (7 Tage gültig) und verschickt die Einladungs-Mail
    an `user.email` (von der aufrufenden Route bereits als gesetzt erzwungen)."""
    raw = await _mint(session, user=user, purpose="invite", created_by=created_by, ttl=INVITE_TTL)
    await _send(user, purpose="invite", raw=raw)


async def issue_reset(session: AsyncSession, *, user: AppUser, created_by: int) -> None:
    """Entwertet zunächst alle noch gültigen Reset-Tokens des Kontos (§7c: ein neu
    ausgestelltes Token ersetzt ältere), mint dann ein neues `purpose='reset'`-Token
    (1 Stunde gültig) und verschickt die Reset-Mail."""
    assert user.id is not None
    await user_token_repo.consume_live_for_user(session, app_user_id=user.id, purpose="reset")
    raw = await _mint(session, user=user, purpose="reset", created_by=created_by, ttl=RESET_TTL)
    await _send(user, purpose="reset", raw=raw)
