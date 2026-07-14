# Changelog

All notable changes to PwNotify are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.6] — 2026-07-14

### Added

- **Progressive Web App**: a minimal service worker (registered only in secure contexts)
  enables installation to the home screen alongside the existing web manifest and icons.
- **Mobile install hint**: on phones, a bottom-sheet prompt offers the native install
  (Android/Chrome) or step-by-step instructions (iOS/Safari, Android/Chrome). It never
  shows when the app already runs as an installed PWA, nor after it is dismissed. Fully
  localized (DE/EN).

## [0.1.5] — 2026-07-14

### Added

- **Full internationalization (German / English)** across the entire UI — every page, the
  setup wizard, all toasts, and all error messages. Backend errors carry stable error codes
  that the frontend translates (`app/core/errors.py` + `lib/errors.ts`), so nothing stays
  untranslated. Date/relative-time formatting follows the active language.
- **Per-account language**: a new `app_user.language` column (Alembic migration) stores each
  user's UI language; `POST /api/auth/language` updates it, and it is applied on login across
  devices. A **language switch (DE/EN)** sits at the bottom of the sidebar and takes effect
  immediately without a reload.
- Stack: `i18next`, `react-i18next`, `i18next-browser-languagedetector` (see VERSIONS.md).

## [0.1.4] — 2026-07-14

### Added

- **Manual update check**: a "Check now" button in Settings → General forces an immediate
  check (`GET /api/version?force=true`, bypassing the 6 h cache). The update modal also
  re-checks hourly, so long-running sessions notice a new release without a reload.

## [0.1.3] — 2026-07-14

### Added

- **In-app update notification**: the running instance periodically checks the latest
  GitHub release (`GET /api/version`, cached 6 h) and, when a newer version exists, shows a
  centered modal that must be acknowledged — including the new release's notes, so operators
  see what an update brings before installing it. The tag used (`latest` vs pinned) does not
  matter; the check compares the version baked into the running image.
- **Settings → General tab**: shows the installed vs. available version and a toggle to
  enable/disable the update check (`app.update_check`, on by default).

### Fixed

- `__version__` was stale at `0.1.0`; it is now bumped with every release (drives the
  update check).

## [0.1.2] — 2026-07-14

### Added

- **Setup wizard — optional extras in the Graph step**: public app URL (domain), the
  sync-scope group, and Microsoft-SSO configuration (enable, admin group, button label,
  with the redirect-URI shown) — all optional and editable later in Settings.
- **Automatic first sync** on finishing the setup wizard: Entra users are loaded (dry-run,
  so no e-mails are sent) and SSO users are synced, so the dashboard is populated right away.

## [0.1.1] — 2026-07-14

### Added

- **Group-scoped sync** — optionally restrict the Graph sync to members of one Entra group
  (`sync.group_id`), so only relevant users are checked and shared mailboxes / disabled
  accounts are excluded at the source. Settings → Graph carries a dynamic-group rule
  template. Reading group members needs the `GroupMember.Read.All` application permission.
- **Profile avatars** — SSO users show their Microsoft Entra photo (fetched via the existing
  `User.Read.All` permission and cached); local users can upload an avatar on the profile
  page (auto-cropped to a square). Shown in the top-right menu.
- **Logo handling** — uploaded logos are auto-trimmed of transparent borders and normalised
  to a Hi-DPI height; the sidebar logo links to the dashboard.

### Fixed

- **Setup wizard** no longer bounces back to its first step after completion — the cached
  setup status is updated immediately when the admin is created, so the guard routes to the
  dashboard.
- **Settings → Graph** — the "Microsoft Graph" and "Sync-Umfang" sections now each save only
  their own fields instead of persisting both at once.
- Removed the obsolete "Shared Mailboxes" and "Deaktiviert" filters from the users table
  (group-scoped sync makes them redundant).

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

[0.1.6]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.6
[0.1.5]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.5
[0.1.4]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.4
[0.1.3]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.3
[0.1.2]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.2
[0.1.1]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.1
[0.1.0]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.0
