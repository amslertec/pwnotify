"""A6 + A7: SMTP target/URL hardening of settings validation.

A7 -- ``app.public_url`` / ``branding.reset_url`` feed the one-time token links of
outgoing reset/invite mails. Without a validator a tenant admin can point them at an
attacker host / a ``javascript:`` scheme / a CRLF injection. ``url_setting`` enforces
https, forbids dangerous schemes and newlines (empty = "not set").

A6 -- ``mail.smtp_host`` without an allowlist permits blind SSRF against internal targets
(169.254.169.254 / 127.0.0.1 / RFC1918); ``mail.smtp_tls=none`` sends the SMTP credentials
in cleartext to a freely chosen external target. ``smtp_host`` rejects internal targets
(unless allowed via ``PWNOTIFY_SMTP_ALLOWED_HOSTS``); the tls=none cross-check in the set
path permits cleartext only for internal relays.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.core.config import get_settings
from app.core.errors import ValidationError
from app.db.tenant_context import tenant_scoped_session
from app.services.settings_service import SettingsService
from app.services.settings_validators import is_internal_host, smtp_host, url_setting
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def temp_tenant(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Throwaway tenant on its own committed connection; rows removed in finally."""
    async with migrated_engine.connect() as conn:
        tid = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) "
                    "VALUES ('InputHardening', 'input-hardening', true, now()) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await conn.commit()
        try:
            yield tid
        finally:
            await conn.execute(text("DELETE FROM setting WHERE tenant_id = :id"), {"id": tid})
            await conn.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tid})
            await conn.commit()


# --- A7: url_setting (pure) --------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["", None, "https://pwnotify.kunde.tld", "https://host:8443/x"])
def test_url_setting_accepts_https_and_empty(value: str | None) -> None:
    assert url_setting(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.tld",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://host/x",
        "//evil.tld",
        "https://ok.tld\r\nSet-Cookie: x",
        "https://ok.tld\nX",
        "https:///no-host",
    ],
)
def test_url_setting_rejects_dangerous(value: str) -> None:
    with pytest.raises(ValidationError) as ei:
        url_setting(value)
    assert ei.value.status_code == 400


# --- A6: smtp_host (pure) ----------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        "169.254.169.254",  # link-local (cloud metadata)
        "127.0.0.1",  # loopback
        "10.1.2.3",  # RFC1918
        "172.16.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "localhost",  # loopback alias
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 ULA
    ],
)
def test_smtp_host_rejects_internal(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "smtp_allowed_hosts", "")
    with pytest.raises(ValidationError):
        smtp_host(value)


@pytest.mark.parametrize("value", ["", "smtp.example.com", "93.184.216.34", "mail.kunde.tld"])
def test_smtp_host_accepts_external_and_empty(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "smtp_allowed_hosts", "")
    assert smtp_host(value) == value


def test_smtp_host_allowlist_permits_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "smtp_allowed_hosts", "127.0.0.1, relay.internal")
    assert smtp_host("127.0.0.1") == "127.0.0.1"


# --- M-01: numeric/legacy IPv4 forms of loopback must count as internal ------------------- #
@pytest.mark.parametrize(
    "value",
    [
        "2130706433",  # 127.0.0.1 as a 32-bit decimal integer
        "0177.0.0.1",  # octal first octet
        "127.1",  # short form (127.0.0.1)
        "0x7f.0.0.1",  # hex first octet
    ],
)
def test_is_internal_host_catches_numeric_loopback(value: str) -> None:
    assert is_internal_host(value) is True


@pytest.mark.parametrize("value", ["8.8.8.8", "mail.example.com"])
def test_is_internal_host_leaves_external_external(value: str) -> None:
    assert is_internal_host(value) is False


def test_is_internal_host_canonical_loopback_regression() -> None:
    assert is_internal_host("127.0.0.1") is True


@pytest.mark.parametrize("value", ["2130706433", "0177.0.0.1", "127.1", "0x7f.0.0.1"])
def test_smtp_host_rejects_numeric_internal(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "smtp_allowed_hosts", "")
    with pytest.raises(ValidationError):
        smtp_host(value)


# --- A6: tls=none cross-check in the set path --------------------------------------------- #
async def test_set_many_rejects_external_host_with_tls_none(temp_tenant: int) -> None:
    async with tenant_scoped_session(temp_tenant) as s:
        svc = SettingsService(s)
        with pytest.raises(ValidationError) as ei:
            await svc.set_many({"mail.smtp_host": "smtp.example.com", "mail.smtp_tls": "none"})
        assert ei.value.status_code == 400
        assert await svc.get("mail.smtp_tls") == "starttls"  # nothing persisted


async def test_set_many_external_host_with_starttls_ok(temp_tenant: int) -> None:
    async with tenant_scoped_session(temp_tenant) as s:
        svc = SettingsService(s)
        await svc.set_many({"mail.smtp_host": "smtp.example.com", "mail.smtp_tls": "starttls"})
        assert await svc.get("mail.smtp_host") == "smtp.example.com"


async def test_set_many_internal_allowlisted_host_with_tls_none_ok(
    temp_tenant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "smtp_allowed_hosts", "127.0.0.1")
    async with tenant_scoped_session(temp_tenant) as s:
        svc = SettingsService(s)
        await svc.set_many({"mail.smtp_host": "127.0.0.1", "mail.smtp_tls": "none"})
        assert await svc.get("mail.smtp_tls") == "none"


async def test_set_many_tls_none_alone_sees_persisted_external_host(temp_tenant: int) -> None:
    """Switching only tls=none must still see the host already in the DB (cross-key merge)."""
    async with tenant_scoped_session(temp_tenant) as s:
        svc = SettingsService(s)
        await svc.set_many({"mail.smtp_host": "smtp.example.com", "mail.smtp_tls": "starttls"})
        with pytest.raises(ValidationError):
            await svc.set_many({"mail.smtp_tls": "none"})
