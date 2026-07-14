"""Auth-Schemas."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    is_sso: bool = False
    role: str
    language: str = "de"
    last_login_at: dt.datetime | None = None
    has_avatar: bool = False
    # Datei-Änderungszeit als Cache-Buster -> neues Profilbild erscheint sofort.
    avatar_version: int = 0


class LanguageUpdate(BaseModel):
    language: str = Field(pattern="^(de|en)$")


class AdminUserOut(BaseModel):
    id: int
    username: str
    display_name: str | None
    is_sso: bool
    is_active: bool
    role: str
    last_login_at: dt.datetime | None
    created_at: dt.datetime


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=150)
    password: str = Field(min_length=10, max_length=1024)
    display_name: str | None = Field(default=None, max_length=320)


class SessionOut(BaseModel):
    id: int
    user_agent: str | None
    ip_address: str | None
    created_at: dt.datetime
    last_used_at: dt.datetime
    current: bool = False


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=10, max_length=1024)


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=320)
