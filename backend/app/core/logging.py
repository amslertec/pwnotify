"""Strukturiertes JSON-Logging via structlog. Redacted Secrets, Level per ENV."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from .config import get_settings

# Schlüssel, deren Werte niemals im Klartext geloggt werden dürfen.
_REDACT_KEYS = {
    "password",
    "client_secret",
    "secret",
    "secret_key",
    "smtp_password",
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "refresh_token",
}


# Schlüssel mit Personenbezug. Sie werden nicht entfernt, sondern gekürzt: Logs werden
# oft zentral gesammelt und lange aufbewahrt, dort gehören keine vollständigen Adressen
# hin — für die Fehlersuche braucht man aber erkennen können, wen es betraf. Die Domain
# bleibt deshalb stehen. Für Forensik existiert das Audit-Protokoll mit Klartext.
_PII_KEYS = {"upn", "addr", "recipient", "mail", "email", "username", "user", "to"}


def mask_pii(value: str) -> str:
    """``pascal.amsler@example.com`` -> ``pa***@example.com``; sonst Anfang + ``***``."""
    if not value:
        return value
    if "@" in value:
        lokal, _, domain = value.partition("@")
        sichtbar = lokal[:2] if len(lokal) > 3 else lokal[:1]
        return f"{sichtbar}***@{domain}"
    return f"{value[:2]}***" if len(value) > 4 else "***"


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        wert = event_dict[key]
        if not wert:
            continue
        k = key.lower()
        if k in _REDACT_KEYS:
            event_dict[key] = "***redacted***"
        elif k in _PII_KEYS and isinstance(wert, str):
            event_dict[key] = mask_pii(wert)
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # stdlib-Logger (uvicorn, sqlalchemy) auf denselben Stream/Level bringen
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
