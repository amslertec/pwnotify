"""Strukturiertes JSON-Logging via structlog. Redacted Secrets, Level per ENV."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from .config import get_settings
from .redaction import is_secret_key

# Which keys count as secret is defined once in `core.redaction.is_secret_key` and shared
# with the audit trail (finding I1), so the logger and the audit log can never diverge on
# what gets redacted. Redaction (deletion) stays deliberately separate from `_PII_KEYS`
# masking (truncation) below -- different treatments; the redact check runs first.

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
    if is_secret_key(k):
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
