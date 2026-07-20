"""Reusable value validators for the settings registry.

A validator takes the raw value a client wants to persist and either returns a (possibly
normalised) value or raises :class:`ValidationError` (HTTP 400). Specs in
``settings_schema`` opt in via ``SettingSpec(default, validate=...)``; keys without a
validator keep their previous free-form behaviour (backwards compatible).
"""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.config import get_settings
from ..core.errors import ValidationError
from .retention import AUDIT_RETENTION_FLOOR_DAYS


def number_range(
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    exclusive_min: bool = False,
    integer_only: bool = False,
    allow_none: bool = False,
    message: str | None = None,
) -> Callable[[Any], Any]:
    """Build a validator enforcing a numeric range.

    ``min_value``/``max_value`` are inclusive unless ``exclusive_min`` makes the lower
    bound strict. ``integer_only`` rejects fractional values. ``allow_none`` permits an
    unset value. The original value is returned unchanged on success so the stored JSON
    keeps its input type.
    """

    def _validate(value: Any) -> Any:
        if value is None:
            if allow_none:
                return None
            raise ValidationError(message or "A value is required.")
        if isinstance(value, bool):
            # bool is an int subclass but never a meaningful numeric setting here.
            raise ValidationError(message or f"Expected a number, got {value!r}.")
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(message or f"Expected a number, got {value!r}.") from exc
        if not math.isfinite(num):
            raise ValidationError(message or f"Expected a finite number, got {value!r}.")
        if integer_only and not float(num).is_integer():
            raise ValidationError(message or f"Expected a whole number, got {value!r}.")
        if min_value is not None:
            if exclusive_min and num <= min_value:
                raise ValidationError(message or f"Value must be greater than {min_value}.")
            if not exclusive_min and num < min_value:
                raise ValidationError(message or f"Value must be >= {min_value}.")
        if max_value is not None and num > max_value:
            raise ValidationError(message or f"Value must be <= {max_value}.")
        return value

    return _validate


_AUDIT_RETENTION_MSG = (
    f"Audit-Aufbewahrung: 0 (unbegrenzt) oder mindestens {AUDIT_RETENTION_FLOOR_DAYS} Tage."
)


def audit_retention_days(value: Any) -> Any:
    """Validator for ``audit.retention_days``: 0 (keep forever) OR >= the retention floor.

    ``number_range`` alone cannot express "0 OR >= 30": that is a disjoint set, not a
    contiguous range. So this reuses ``number_range`` for the type/integer/negative checks and
    then rejects the ``1..FLOOR-1`` band. The floor (see ``retention.AUDIT_RETENTION_FLOOR_DAYS``)
    guarantees the recent audit trail cannot be shrunk away, closing the iterative
    "cover your tracks" purge.
    """
    checked = number_range(min_value=0, integer_only=True, message=_AUDIT_RETENTION_MSG)(value)
    if 0 < int(checked) < AUDIT_RETENTION_FLOOR_DAYS:
        raise ValidationError(_AUDIT_RETENTION_MSG)
    return checked


def branding_dir() -> Path:
    """Resolved directory that legitimately holds uploaded branding assets.

    Deliberately the SHARED branding root, not a per-tenant subdirectory, even though new
    uploads are written tenant-scoped under ``{root}/{tenant_id}/`` (see
    ``routes/branding._tenant_branding_dir``). Two reasons this asymmetry is intentional and
    safe, NOT a cross-tenant read (L5):

    * Legacy ``branding.*_path`` values still point at the flat, pre-tenant-scoping layout
      (``{root}/logo.png``); the containment base must stay at the root so those keep
      resolving. A tenant subdir is itself inside the root, so new uploads pass too.
    * Every path-resolving branding route (``get_logo``/``get_favicon``/``public_branding``)
      runs on ``PublicTenantSettingsDep`` -- ALWAYS the default tenant -- so only the default
      tenant's own stored path is ever resolved and served, regardless of caller. The value
      comes from that tenant's persisted setting, never from free request input. The
      traversal guard (``resolve()`` + ``relative_to``, defeating ``..`` and symlinks) is the
      actual security boundary here and is unaffected.
    """
    return (Path(get_settings().data_dir) / "branding").resolve()


def contained_path(base: Path, candidate: str | Path) -> Path | None:
    """Resolve ``candidate`` and return it only if it lies within ``base`` (already resolved),
    else None. Non-strict resolution normalises ``..`` even for a not-yet-existing file — the
    guard against path traversal.
    """
    try:
        resolved = Path(candidate).resolve()
        resolved.relative_to(base)
    except ValueError, OSError, RuntimeError:
        return None
    return resolved


def branding_path(value: Any) -> Any:
    """Validator for branding.*_path: allow clearing (None/"") or a path inside branding_dir()."""
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        raise ValidationError("Branding path must be a string.")
    if contained_path(branding_dir(), value) is None:
        raise ValidationError("Branding path escapes the branding directory.")
    return value


def url_setting(value: Any) -> Any:
    """Validator for public URL settings (``app.public_url`` / ``branding.reset_url``).

    Both feed the one-time-token links of outgoing reset/invite mails: ``app.public_url``
    builds ``effective_base_url()``, ``branding.reset_url`` is emitted verbatim. An unset
    value ("") is allowed and falls back to the ENV/default. A *set* value must be a plain
    ``https://`` URL with a host -- this rejects link-injection via dangerous schemes
    (``javascript:``/``data:``) and header/log injection via CR/LF, so a tenant admin cannot
    redirect the token links to an attacker-controlled destination (A7).
    """
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        raise ValidationError("URL muss eine Zeichenkette sein.")
    # Check raw for CR/LF FIRST: urlsplit() silently strips leading control chars, so a later
    # parse would not see an injected newline.
    if "\r" in value or "\n" in value or "\t" in value:
        raise ValidationError("URL darf keine Zeilenumbrüche enthalten.")
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise ValidationError("URL muss mit https:// beginnen.")
    if not parsed.netloc:
        raise ValidationError("URL muss einen gültigen Host enthalten.")
    return value


# Hostnames that are effectively loopback without being an IP literal.
_LOCALHOST_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _smtp_allowlist() -> set[str]:
    """Operator-configured hosts (``PWNOTIFY_SMTP_ALLOWED_HOSTS``, comma-separated) that may be
    internal SMTP targets and/or receive plaintext (tls=none). Default empty."""
    raw = get_settings().smtp_allowed_hosts or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _normalise_host(host: str) -> str:
    return host.strip().strip("[]").lower()


def is_internal_host(host: str) -> bool:
    """True if ``host`` denotes a loopback / link-local / private (RFC1918/ULA) target.

    A bare hostname (not an IP literal) counts as internal only when it is an obvious
    localhost alias -- full DNS-rebinding protection is out of scope; the goal is catching the
    obvious SSRF / misconfiguration on IP literals + localhost.
    """
    h = _normalise_host(host)
    if not h:
        return False
    if h in _LOCALHOST_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_link_local or ip.is_private


def smtp_host(value: Any) -> Any:
    """Validator for ``mail.smtp_host`` (A6): reject internal targets, allow external + empty.

    An internal/link-local/loopback/private host is a blind-SSRF / internal-port-scan vector,
    so it is rejected UNLESS the operator explicitly allowlisted it via
    ``PWNOTIFY_SMTP_ALLOWED_HOSTS`` (a legitimate internal relay). Clearing the host ("") is
    always allowed.
    """
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        raise ValidationError("SMTP-Host muss eine Zeichenkette sein.")
    if is_internal_host(value) and _normalise_host(value) not in _smtp_allowlist():
        raise ValidationError(
            "Interner SMTP-Host abgelehnt. Nur explizit über PWNOTIFY_SMTP_ALLOWED_HOSTS "
            "freigegebene Relays dürfen auf interne/lokale Adressen zeigen."
        )
    return value


def check_smtp_tls_allowed(host: Any, tls_mode: Any) -> None:
    """Cross-key rule (A6): plaintext SMTP (``tls=none``) is only permitted to an INTERNAL
    relay, never to an external host, where it would leak the SMTP credentials in cleartext.

    Lives in the set path (``SettingsService.set_many``), not in a per-key validator, because
    a single-key validator cannot see the other key: a PUT may change only ``smtp_tls`` while
    the host already sits in the DB. An internal host can only have been persisted after
    passing ``smtp_host`` (i.e. it is allowlisted), so "internal" here already implies the
    operator opted the relay in.
    """
    if tls_mode != "none":
        return
    if not host:
        # No host configured yet -> nothing is sent; the send-time smtp_no_host guard handles it.
        return
    if not is_internal_host(str(host)):
        raise ValidationError(
            "Unverschlüsseltes SMTP (TLS=none) ist nur für interne Relays zulässig."
        )
