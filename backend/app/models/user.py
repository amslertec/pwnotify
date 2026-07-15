"""Lokale UI-Accounts + Refresh-Token-Sessions."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Column, DateTime, String, Text
from sqlmodel import Field, SQLModel

from ._base import utcnow


class AppUser(SQLModel, table=True):
    __tablename__ = "app_user"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(sa_column=Column(String(150), unique=True, index=True, nullable=False))
    password_hash: str
    display_name: str | None = Field(default=None, sa_column=Column(String(320)))
    role: str = Field(default="admin", sa_column=Column(String(32), nullable=False))
    is_active: bool = Field(default=True)
    is_sso: bool = Field(default=False)
    # UI-Sprache des Kontos (de | en). Steuert nur die Admin-Oberfläche.
    language: str = Field(
        default="de", sa_column=Column(String(8), nullable=False, server_default="de")
    )

    # 2FA (TOTP) — nur lokale Konten. Secret Fernet-verschlüsselt at-rest.
    totp_secret: str | None = Field(default=None, sa_column=Column(String(255)))
    totp_enabled: bool = Field(default=False)
    # Recovery-Codes: JSON-Array von SHA-256-Hex-Hashes ungenutzter Codes.
    recovery_codes: str | None = Field(default=None, sa_column=Column(Text))
    # Zuletzt verbrauchter TOTP-Zeitschritt (30-Sekunden-Fenster seit Epoch). Ein Code
    # bleibt rund 90 s gültig (valid_window=1) und wäre ohne diese Sperre in der Zeit
    # mehrfach einsetzbar — wer ihn abfängt, käme damit ein zweites Mal hinein.
    totp_last_step: int | None = Field(default=None)

    failed_login_count: int = Field(default=0)
    locked_until: dt.datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    last_login_at: dt.datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    created_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class UserSession(SQLModel, table=True):
    """Refresh-Token-Familie mit Rotation. Es wird nur der Token-*Hash* gespeichert."""

    __tablename__ = "user_session"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="app_user.id", index=True, nullable=False)
    refresh_jti: str = Field(sa_column=Column(String(64), unique=True, index=True, nullable=False))
    token_hash: str = Field(sa_column=Column(String(64), nullable=False))
    user_agent: str | None = Field(default=None, sa_column=Column(String(400)))
    ip_address: str | None = Field(default=None, sa_column=Column(String(64)))
    revoked: bool = Field(default=False)
    created_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    last_used_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    expires_at: dt.datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
