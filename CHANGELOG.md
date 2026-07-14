# Changelog

All notable changes to PwNotify are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-13

Initial release.

### Added

- **Microsoft Graph integration** (client-credentials flow): user sync with
  `$select`, `@odata.nextLink` pagination, 429/`Retry-After` throttling with
  exponential backoff, per-domain password-validity detection, and permission
  discovery via the token `roles` claim.
- **Expiry engine**: password-expiry calculation with `DisablePasswordExpiration`
  handling and staged reminders (default 14/7/3/1/0 days), deduplicated per
  user + stage + expiry cycle via a database unique constraint.
- **Mail backends**: Microsoft Graph `sendMail` and SMTP (STARTTLS/SSL/none),
  switchable at runtime, with a configurable recipient strategy
  (primary / alternate / both / alternate-fallback-primary).
- **Templating**: sandboxed Jinja2 HTML e-mails (DE/EN) with live preview,
  placeholder reference, and reset-to-default; plaintext fallback.
- **Scheduler**: APScheduler with a configurable cron expression and timezone,
  "run now", dry-run mode, and per-run logging; graceful shutdown.
- **Web UI**: dashboard, users (server-side table, detail drawer, CSV/XLSX
  export, bulk actions), notifications (with retry), runs, and a full settings
  area (Graph connector with step-by-step guide, mail, schedule, password policy,
  branding, template editor, account). Dark/light theme with system preference.
- **Shared-mailbox handling**: accounts matching configurable UPN/mail glob patterns
  (default `noreply@*`, `home@*`, `info@*`, …) are flagged, hidden from the user list,
  shown under a dedicated "Shared Mailboxes" filter, and excluded from reminders.
- **First-run setup wizard**: database → admin account → Graph → mail.
- **Auth**: local login (Argon2id), JWT in HttpOnly/SameSite=Strict cookies with
  refresh-token rotation and reuse detection, login rate limiting and lockout.
- **Security**: Fernet-encrypted secrets at rest, secret masking, structured JSON
  logging with redaction.
- **Container**: Chainguard-based, non-root (UID 65532), read-only root FS, 0
  known HIGH/CRITICAL CVEs, multi-arch (amd64/arm64), SBOM + provenance, full
  OCI labels, and a pure-Python healthcheck.
- **CI**: GitHub Actions running lint, type-checks, tests, Trivy and Docker Scout
  scans (build fails on HIGH/CRITICAL), and multi-arch publish.

[0.1.0]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.0
