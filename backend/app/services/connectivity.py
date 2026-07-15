"""Verbindungstests für Graph und Mail (von Setup-Wizard und Settings genutzt)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..core.config import get_settings
from .graph import GraphClient, GraphConfig, GraphConnectionResult
from .graph.client import GROUP_PERMISSION
from .mail import build_sender
from .settings_schema import MASK
from .templating import build_context, email_logo, render


def _pick(value: str | None, fallback: str) -> str:
    """Formularwert bevorzugen; Masken-Marker/None -> gespeicherten Wert nutzen."""
    if value in (None, "", MASK):
        return fallback
    return value  # type: ignore[return-value]


async def test_graph(
    settings: dict[str, Any],
    *,
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    cloud: str | None = None,
) -> GraphConnectionResult:
    cfg = GraphConfig(
        tenant_id=_pick(tenant_id, settings.get("graph.tenant_id") or ""),
        client_id=_pick(client_id, settings.get("graph.client_id") or ""),
        client_secret=_pick(client_secret, settings.get("graph.client_secret") or ""),
        cloud=_pick(cloud, settings.get("graph.cloud") or "global"),
    )
    if not (cfg.tenant_id and cfg.client_id and cfg.client_secret):
        return GraphConnectionResult(
            connected=False, error="Tenant-ID, Client-ID und Client-Secret sind erforderlich."
        )
    return await GraphClient(cfg).test_connection(
        extra_permissions=required_group_permissions(settings)
    )


def required_group_permissions(settings: dict[str, Any]) -> list[str]:
    """``GroupMember.Read.All`` nur verlangen, wenn eine Gruppe konfiguriert ist.

    Sonst meldete der Verbindungstest „alle Berechtigungen vorhanden“, während der
    gruppenbasierte Sync und das SSO-Rollen-Mapping mit 403 scheiterten — die Diagnose,
    auf die man sich beim Einrichten verlässt, war also falsch.
    """
    keys = ("sync.group_id", "oidc.admin_group_id", "oidc.auditor_group_id")
    return [GROUP_PERMISSION] if any(settings.get(k) for k in keys) else []


async def send_test_mail(settings: dict[str, Any], *, to: str, locale: str, base_url: str) -> None:
    locale = "en" if locale.lower().startswith("en") else "de"
    sender = build_sender(settings)
    logo_url, inline_images = email_logo(settings, base_url, get_settings().static_dir)
    context = build_context(
        display_name="Erika Mustermann",
        upn="erika.mustermann@example.com",
        days_left=7,
        expiry_date=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
        reset_url=settings.get("branding.reset_url") or "",
        company_name=settings.get("branding.company_name") or "",
        app_name=settings.get("branding.app_name") or "PwNotify",
        logo_url=logo_url,
        primary_color=settings.get("branding.primary_color") or "#4F46E5",
        locale=locale,
    )
    subject = render(settings[f"template.subject_{locale}"], context, html=False)
    html = render(settings[f"template.html_{locale}"], context, html=True)
    text = render(settings[f"template.text_{locale}"], context, html=False)
    await sender.send(
        to=[to],
        subject=f"[Test] {subject}",
        html_body=html,
        text_body=text,
        inline_images=inline_images,
    )
