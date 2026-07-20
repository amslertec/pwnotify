"""Infrastructure configuration from ENV (pydantic-settings).

Important: these settings are the *infrastructure* configuration and the one-time
**seed** on the very first start. The running application settings (Graph, Mail,
Schedule, Branding, Template) live in the DB table ``setting`` and are managed via
the Settings UI. ENV is no longer read after the first seed.
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

    # ---- Infrastructure ----
    database_url: str = "postgresql+asyncpg://pwnotify:pwnotify@localhost:5432/pwnotify"
    # Password for the non-superuser `pwnotify_runtime` login role (tenant-scoped sessions
    # only; see `app/db/session.py::get_runtime_engine`). No default -- `runtime_database_url`
    # fails fast rather than silently falling back to the owner/superuser DSN.
    runtime_db_password: str | None = None
    secret_key: str | None = None  # empty -> auto-generated in {data_dir}/secret.key
    data_dir: str = "/data"
    static_dir: str = "/app/static"
    base_url: str = "http://localhost:8080"
    # Secure by default: cookies only over HTTPS. Anyone deliberately running the app
    # over plaintext HTTP (LAN test, scenario A) must disable this explicitly —
    # a forgotten value must not silently allow tokens over HTTP.
    cookie_secure: bool = True
    log_level: str = "INFO"
    log_json: bool = True
    timezone: str = "Europe/Zurich"
    port: int = 8080

    # Who is trusted to set ``X-Forwarded-For``? Only requests from these peers may
    # override the client IP. This value is security-relevant: the rate limit and the
    # login lockout key off the client IP — trusting the header unconditionally ("*")
    # lets an attacker set it themselves and bypass both protections entirely.
    # Behind a reverse proxy: enter its IP or network (e.g. "172.18.0.0/16").
    trusted_proxies: str = "127.0.0.1"

    # Allowed values in the Host header, comma-separated. Empty = no check (default).
    # Deliberately open by default: too tight a list makes the app unreachable, while
    # the benefit here is small — email links and cookies come from PWNOTIFY_BASE_URL,
    # not from the Host header. Anyone who wants it tight enters their domain(s).
    allowed_hosts: str = ""

    # Interactive OpenAPI docs (`/api/docs`) and the schema (`/api/openapi.json`). OFF by
    # default (M6): both publish the complete route map + request/response schemas to any
    # anonymous caller. Turn on deliberately (e.g. during integration work) with
    # PWNOTIFY_ENABLE_DOCS=true.
    enable_docs: bool = False

    # Hard cap on the request body size (M5), enforced by an ASGI guard BEFORE any handler
    # reads the body. Comfortably above the largest legitimate upload (avatar 5 MB), so
    # normal uploads are unaffected while a multi-GB body is rejected at the transport layer.
    max_request_body_bytes: int = 10 * 1024 * 1024

    # Rate limit for the unauthenticated `/ready` probe (M6). Moderate on purpose: high enough
    # for orchestrator/health polling, low enough that a flood cannot exhaust the DB pool.
    ready_rate_limit: str = "60/minute"

    # ---- Auth / JWT ----
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 14

    # Logout on inactivity. The refresh token alone would otherwise keep a session
    # alive for `refresh_token_ttl_days` — even if nobody is working anymore.
    # Kicks in once `idle_timeout_min` has passed without activity; the session is
    # then deleted, not just revoked. 0 = disabled.
    # The frontend additionally logs out actively on real inactivity (mouse/keyboard) —
    # only that way does it also take effect when a tab stays open and polls in the background.
    idle_timeout_min: int = 30
    login_rate_limit: str = "10/minute"
    login_max_failures: int = 5
    login_lockout_min: int = 15
    setup_rate_limit: str = "30/minute"

    # Test mails (M7) go out over the customer's own mail identity to an arbitrary recipient.
    # They are rare in normal operation, so a tight limit caps abuse (unbounded external
    # send) without hindering legitimate connection testing.
    mail_test_rate_limit: str = "5/minute"

    # Shared by `/auth/refresh` and `/auth/activity` (L7): both are normal, frequent
    # requests during an active session (refresh roughly every `access_token_ttl_min`,
    # activity pings sparser) -- generous headroom so ordinary usage is never affected,
    # while still capping the endpoints as a brute-force/abuse target.
    auth_refresh_rate_limit: str = "60/minute"

    # ---- Initial seed (only evaluated on the very first start) ----
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
    # A6: comma-separated allowlist of SMTP hosts (IP literals or hostnames) that are
    # permitted to be internal targets (loopback/RFC1918/link-local) and/or run
    # unencrypted (tls=none). Empty (default): an internal SMTP host or plaintext auth
    # is rejected as an SSRF/cleartext misconfiguration until a relay is explicitly allowed here.
    smtp_allowed_hosts: str = ""

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
        """Alembic/psycopg-style, synchronous URL (asyncpg -> psycopg driver removed)."""
        return self.database_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")

    @property
    def runtime_database_url(self) -> str:
        """DSN for the non-superuser tenant-data engine (`pwnotify_runtime`). Derived from the
        owner DSN with the username/password swapped -- host/db/query preserved."""
        from sqlalchemy.engine import make_url

        if not self.runtime_db_password:
            raise RuntimeError(
                "PWNOTIFY_RUNTIME_DB_PASSWORD is required (no silent fallback to the superuser DSN)"
            )
        url = make_url(self.database_url).set(
            username="pwnotify_runtime", password=self.runtime_db_password
        )
        return url.render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
