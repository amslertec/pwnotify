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
    "new_password",
    "current_password",
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
    "totp_secret",
    "recovery_codes",
    "graph_client_secret",
    "id_token",
    "api_key",
    "private_key",
}

# Zusätzlich zum exakten Match: Schlüssel, die eines dieser Suffixe/Substrings
# enthalten, gelten ebenfalls als geheim (z. B. "graph_client_secret",
# "smtp_password", "new_password" -- ohne jedes Präfix einzeln aufzählen zu
# müssen). Bewusst getrennt von `_PII_KEYS` gehalten: Redaction (löschen) und
# PII-Maskierung (kürzen) sind unterschiedliche Behandlungen; der Redact-Check
# läuft zuerst.
_REDACT_KEY_SUBSTRINGS = ("password", "secret", "token", "recovery_codes")


def _is_secret_key(key: str) -> bool:
    return key in _REDACT_KEYS or any(part in key for part in _REDACT_KEY_SUBSTRINGS)


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


def _redact_value(key: str, value: Any) -> Any:
    """Redact/mask ``value`` recursively based on ``key``, descending into nested
    dicts and lists/tuples so secrets are never missed at depth (e.g. a nested
    ``detail={...}`` sub-dict or a list of dicts).

    List elements are only recursed into when they are themselves containers
    (dict/list) so a nested dict's OWN keys get checked. Scalar list elements
    (e.g. a plain list of recipient strings) are intentionally left as-is: PII
    masking only ever applied to a directly-keyed string value, never to loose
    elements inherited from an outer key -- changing that would silently start
    masking list-of-string fields that were previously logged in full.
    """
    k = key.lower()
    if _is_secret_key(k):
        return "***redacted***"
    if isinstance(value, dict):
        return {kk: _redact_value(kk, vv) for kk, vv in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(
            _redact_value(key, v) if isinstance(v, (dict, list, tuple)) else v for v in value
        )
    if k in _PII_KEYS and isinstance(value, str):
        return mask_pii(value)
    return value


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        event_dict[key] = _redact_value(key, event_dict[key])
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
