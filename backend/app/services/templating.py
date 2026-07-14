"""E-Mail-Template-Rendering (Jinja2 Sandbox) + Vorschau-Kontext."""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

from jinja2 import select_autoescape
from jinja2.exceptions import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from ..core.errors import PwNotifyError

# Eingebettetes Logo (CID). InlineImage = (content_id, bytes, mime).
LOGO_CID = "pwnotify-logo"
_IMG_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_env = SandboxedEnvironment(autoescape=select_autoescape(["html", "xml"]))

# Für Plaintext kein Autoescape.
_text_env = SandboxedEnvironment(autoescape=False)


def render(template_str: str, context: dict[str, Any], *, html: bool = True) -> str:
    try:
        env = _env if html else _text_env
        return env.from_string(template_str).render(**context)
    except TemplateError as exc:
        raise PwNotifyError(f"Template-Fehler: {exc}", code="template_error") from exc


def resolve_logo(settings: dict[str, Any], base_url: str) -> tuple[str, bool]:
    """Logo-http-URL für die UI-Vorschau (Browser kann sie laden).

    Eigenes Logo falls hochgeladen, sonst die gerenderte PwNotify-Wortmarke (PNG).
    """
    if settings.get("branding.logo_path"):
        return f"{base_url}/api/branding/logo", True
    return f"{base_url}/brand/logo-email.png", False


def build_logo_attachments(
    settings: dict[str, Any], static_dir: str
) -> list[tuple[str, bytes, str]]:
    """Bild-Bytes fürs eingebettete E-Mail-Logo (CID). Eigenes Logo oder Default-Wortmarke.

    SVG-Uploads werden auf die Default-PNG-Wortmarke zurückgestuft (SVG rendert in
    Mail-Clients nicht). Leere Liste, wenn keine Datei gefunden wird.
    """
    default_png = os.path.join(static_dir, "brand", "logo-email.png")
    custom = settings.get("branding.logo_path")

    path: str | None = None
    if custom and os.path.exists(custom) and os.path.splitext(custom)[1].lower() != ".svg":
        path = custom
    elif os.path.exists(default_png):
        path = default_png

    if not path:
        return []
    mime = _IMG_MIME.get(os.path.splitext(path)[1].lower(), "image/png")
    with open(path, "rb") as fh:
        return [(LOGO_CID, fh.read(), mime)]


def email_logo(
    settings: dict[str, Any], base_url: str, static_dir: str
) -> tuple[str, list[tuple[str, bytes, str]]]:
    """Für den echten Versand: (logo_url, inline_images).

    Bevorzugt das eingebettete CID-Logo (funktioniert ohne Netzwerkzugriff, auch bei
    privater LAN-URL). Fällt auf eine http-URL zurück, falls keine Bilddatei existiert.
    """
    attachments = build_logo_attachments(settings, static_dir)
    if attachments:
        return f"cid:{LOGO_CID}", attachments
    url, _ = resolve_logo(settings, base_url)
    return url, []


def build_context(
    *,
    display_name: str,
    upn: str,
    days_left: int | None,
    expiry_date: dt.datetime | None,
    reset_url: str,
    company_name: str,
    app_name: str,
    logo_url: str,
    primary_color: str,
    locale: str = "de",
) -> dict[str, Any]:
    if expiry_date is not None:
        fmt = "%d.%m.%Y" if locale == "de" else "%Y-%m-%d"
        expiry_str = expiry_date.strftime(fmt)
    else:
        expiry_str = "-"
    return {
        "displayName": display_name,
        "upn": upn,
        "daysLeft": days_left if days_left is not None else "-",
        "expiryDate": expiry_str,
        "resetUrl": reset_url,
        "companyName": company_name,
        "appName": app_name,
        "logoUrl": logo_url,
        "primaryColor": primary_color,
    }


def sample_context(settings: dict[str, Any], base_url: str, locale: str = "de") -> dict[str, Any]:
    """Beispiel-Kontext für die Live-Vorschau im UI."""
    logo_url, _ = resolve_logo(settings, base_url)
    return build_context(
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
