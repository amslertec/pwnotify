"""Auth-Schemas."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=1024)


class TenantRef(BaseModel):
    """Minimale Tenant-Darstellung fürs Frontend (Umschalter, aktiver Mandant)."""

    id: int
    name: str


class SwitchTenantRequest(BaseModel):
    tenant_id: int


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    is_sso: bool = False
    role: str
    language: str = "de"
    two_factor_enabled: bool = False
    last_login_at: dt.datetime | None = None
    has_avatar: bool = False
    # Datei-Änderungszeit als Cache-Buster -> neues Profilbild erscheint sofort.
    avatar_version: int = 0
    # Minuten ohne Aktivität bis zur automatischen Abmeldung (0 = aus). Das Frontend
    # braucht den Wert, um bei echter Untätigkeit selbst abzumelden — ein offener Tab
    # pollt sonst weiter und hielte die Sitzung am Leben.
    idle_timeout_min: int = 0
    # Aktiver Mandant (aus dem `active_tenant`-Claim/der Session aufgelöst) -- None, wenn
    # dem Konto (noch) keiner zugeordnet ist. Und die Mandanten, zu denen umgeschaltet
    # werden darf (Phase 4a Task 5) -- <=1 Eintrag heisst fürs Frontend: Umschalter
    # ausblenden, es gibt nichts zum Wechseln.
    active_tenant: TenantRef | None = None
    switchable_tenants: list[TenantRef] = []
    # Instanzweiter Schalter (Access-Modell/Superadmin-Design, Task 5) -- das Frontend
    # braucht ihn, um sein Chrome zu gaten (Mandanten-Umschalter/-Verwaltung nur sichtbar,
    # wenn Multi-Tenant-Mode aktiv ist). Default AUS, wie der zugrundeliegende Setting-Key.
    multi_tenant_mode: bool = False
    # True, wenn `active_tenant` der Default-Tenant ist (Context-Gating v2, Matrix B) --
    # das einzige Feld, über das das Frontend Default-Kontext-Chrome gatet (Instanz-
    # Einstellungen, Mandanten-/Zuweisungs-Konsole; siehe `deps.require_superadmin_default_
    # context` für die serverseitige Durchsetzung). False, wenn `active_tenant` None ist
    # (kein Tenant zugeordnet -- niemals stillschweigend "Default" behaupten).
    active_tenant_is_default: bool = False


class LanguageUpdate(BaseModel):
    language: str = Field(pattern="^(de|en)$")


class LoginResponse(BaseModel):
    two_factor_required: bool = False
    # 2FA ist Pflicht, aber noch nicht eingerichtet: Es gibt bewusst noch keine Sitzung —
    # der Weg führt direkt in die Einrichtung, erst danach werden Tokens ausgestellt.
    two_factor_setup_required: bool = False
    user: UserOut | None = None


class TwoFactorCode(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class TwoFactorSetupOut(BaseModel):
    otpauth_uri: str
    qr_png: str
    secret: str


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


# `admin_users.list_users` gibt `dict[str, list[AdminUserOut]]` zurück -- die Schlüssel
# `local`/`sso` sind für jeden Aufrufer gescopt (Task 3); der optionale Schlüssel
# `superadmins` existiert NUR in der Antwort an einen Superadmin-Aufrufer.
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
    role: str = Field(default="admin", pattern="^(admin|auditor)$")


class RoleUpdate(BaseModel):
    role: str = Field(pattern="^(admin|auditor)$")


class SuperadminCreate(BaseModel):
    """Nur für den superadmin-only Erstellungs-Endpunkt (`POST /admin/users/superadmin`,
    Task 4) -- bewusst KEIN `role`-Feld (immer fest `superadmin`) und ein explizites
    `is_sso`-Feld, das die Route hart ablehnt, statt es zu ignorieren (Design §11.3:
    Superadmin ist IMMER ein lokales Konto)."""

    username: str = Field(min_length=3, max_length=150)
    password: str = Field(min_length=10, max_length=1024)
    display_name: str | None = Field(default=None, max_length=320)
    is_sso: bool = False


class SuperadminToggle(BaseModel):
    """Body für `POST /admin/users/{user_id}/superadmin` (Task 4) -- der einzige Pfad, über
    den ein Konto zum/vom Superadmin befördert/herabgestuft werden kann (`set_role` selbst
    lehnt jeden Rollenwechsel eines Superadmin-Ziels ab, siehe dortigen Guard)."""

    promote: bool


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
