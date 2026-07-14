"""Infrastruktur-Konfiguration aus ENV (pydantic-settings).

Wichtig: Diese Settings sind die *Infrastruktur*-Konfiguration und der einmalige
**Seed** beim allerersten Start. Die laufenden Anwendungs-Einstellungen (Graph,
Mail, Schedule, Branding, Template) leben in der DB-Tabelle ``setting`` und werden
über die Settings-UI verwaltet. ENV wird nach dem ersten Seed nicht mehr gelesen.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PWNOTIFY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Infrastruktur ----
    database_url: str = "postgresql+asyncpg://pwnotify:pwnotify@localhost:5432/pwnotify"
    secret_key: str | None = None  # leer -> auto-generiert in {data_dir}/secret.key
    data_dir: str = "/data"
    static_dir: str = "/app/static"
    base_url: str = "http://localhost:8080"
    cookie_secure: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    timezone: str = "Europe/Zurich"
    port: int = 8080

    # ---- Auth / JWT ----
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 14
    login_rate_limit: str = "10/minute"
    login_max_failures: int = 5
    login_lockout_min: int = 15

    # ---- Erst-Seed (nur beim allerersten Start ausgewertet) ----
    admin_username: str | None = None
    admin_password: str | None = None

    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None
    graph_cloud: str = "global"  # global | usgov | china

    mail_backend: str = "graph"  # graph | smtp
    mail_from: str | None = None
    mail_recipient_strategy: str = "primary"  # primary|alternate|both|alternate_fallback_primary
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_tls: str = "starttls"  # starttls | ssl | none

    schedule_cron: str = "0 8 * * *"
    reminder_days: str = "14,7,3,1,0"
    dry_run: bool = False
    password_validity_days: int | None = None

    app_name: str = "PwNotify"
    company_name: str | None = None
    primary_color: str = "#4F46E5"
    reset_url: str = "https://account.activedirectory.windowsazure.com/ChangePassword.aspx"

    @property
    def sync_database_url(self) -> str:
        """Alembic/psycopg-artige, synchrone URL (asyncpg -> psycopg-Treiber weg)."""
        return self.database_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
