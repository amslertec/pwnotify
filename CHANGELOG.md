# Changelog

All notable changes to PwNotify are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] — 2026-07-20

### Changed

- **The bundled database image is now `amslertec/pwnotifydb:18`** — the same PostgreSQL 18.4 as
  the official `postgres:18-alpine`, rebuilt with **0 known HIGH/CRITICAL CVEs** (the `gosu`
  privilege-drop helper is replaced by `su-exec`, and `c-ares` is upgraded). It is a drop-in
  swap: same server version, same `PGDATA`, same environment, so existing data volumes work
  unchanged. Set `PWNOTIFY_DB_IMAGE=postgres:18-alpine` to keep stock Postgres. No application
  code changed in this release.

## [0.3.0] — 2026-07-19

This release completes a six-part security-hardening series (0.2.5–0.3.0) that worked through an
external security audit end to end. It adds the final low-severity and hardening fixes.

### Security

- **The signing key file is created with locked-down permissions atomically**, removing a brief
  window in which it could have been world-readable.
- **Application logs redact secrets more thoroughly** — nested fields and additional secret-bearing
  keys (recovery codes, TOTP secrets, tokens) are now masked.
- **`/health` no longer exposes the application version** to unauthenticated callers; the version is
  available from the authenticated `/api/version` endpoint.
- **Bulk inputs and external pagination are bounded**, preventing an oversized request or an
  unbounded upstream response from consuming excessive resources.
- **Password change, token refresh, and the activity ping are now rate-limited.**
- **First-time-setup admin creation is serialized**, so a race can no longer create two initial
  administrators.
- **The container's build metadata and CI image scanning were tightened** — the base-image digest
  label is correct and both published architectures (amd64 and arm64) are vulnerability-scanned
  before publish.

### Changed

- `GET /health` now returns only `{"status": "ok"}` (no `version` field).
- `POST /auth/password`, `POST /auth/refresh`, and `POST /auth/activity` may now return HTTP 429
  when their rate limits are exceeded.

## [0.2.9] — 2026-07-19

### Security

- **The full password policy is enforced everywhere a password is set.** First-setup admin,
  password change, and admin-created accounts now require the same strength rules already applied
  to invite and reset links, instead of only a minimum length.
- **Login no longer reveals whether an account exists or is locked.** A wrong password looks and
  behaves the same whether the account is unknown, wrong-password, or locked, closing a
  user-enumeration and lock-status oracle.
- **Recovery codes are stronger.** New two-factor recovery codes use more entropy and are stored
  with the same strong hashing as passwords; existing codes keep working until you re-enrol.
- **Customer settings and files are strictly separated per customer.** Reading a tenant's settings
  no longer folds other tenants' values together, and uploaded logos/favicons are stored in a
  per-customer location so one customer can never overwrite another's branding.
- **Profile photos are scoped to the caller's customers.** An administrator can no longer fetch the
  avatar of a user outside their own customers, and the endpoint no longer reveals whether an
  arbitrary account exists.
- **Uploaded images are capped before decoding**, preventing a small crafted file from exhausting
  memory (decompression-bomb protection) on branding and avatar uploads.
- **More security-relevant actions are written to the audit log** — manual notifications and
  exclusions, run triggers, notification retries, branding changes, SSO sync, setup, and
  two-factor enrolment — and customer-attributable events now appear in the right customer's audit
  view instead of only for the provider.

### Changed

- Setting a too-weak password on `/setup/admin`, `/auth/password`, or the admin create-user
  endpoints now returns HTTP 403 `code="password_policy"` (previously only invite/reset did).
- Uploading or deleting branding now requires write access to the customer (an auditor-only account
  can no longer change branding).

## [0.2.8] — 2026-07-19

### Security

- **Single sign-on is now protected against login CSRF and code interception.** The OIDC login
  flow now uses PKCE, a nonce, and a browser-bound state cookie, so a sign-in can only be completed
  in the same browser that started it and an intercepted authorization code cannot be redeemed.
- **Access tokens can now be revoked immediately.** Signing out, changing your password, "log out
  other devices", and the automatic response to a detected stolen refresh token now invalidate the
  short-lived access token right away, instead of leaving it usable until it expired. Your own
  current session stays signed in where appropriate.
- **The two-factor setup code can no longer be reused.** The code entered to finish enrolling
  two-factor is now consumed, closing a short window in which it could be replayed.

## [0.2.7] — 2026-07-19

### Security

- **Tenant queries now run as a dedicated non-superuser database role.** Previously the app
  connected to Postgres as the owning superuser for all work, so a compromised tenant route could
  in principle bypass row-level security by table ownership. Tenant-scoped queries — from both
  request handling and background jobs — now use a separate `pwnotify_runtime` login role
  (`NOSUPERUSER`, `NOBYPASSRLS`), so row-level security can no longer be escaped. Migrations and
  owner-context work are unchanged.
- **Least privilege on provider tables.** The tenant role's access to four instance-wide tables
  (invite/reset tokens, group configuration and membership snapshots) has been revoked entirely —
  they are only ever used in owner context, so the tenant surface should never reach them.
- **Write actions require write permission, not just read access.** Mutating tenant routes
  (triggering a run, changing settings, retrying or excluding, bulk actions) now authorize against
  write access. An account with only read (auditor) access to a customer can no longer perform
  them, and a role change now migrates the account's access grants so capability never lags behind
  the role.
- **A guard-coverage test now fails the build if any API route is added without an explicit access
  decision**, closing the class of "accidentally unauthenticated endpoint" at CI time.

### Upgrading

- This release adds a **required** environment variable `PWNOTIFY_RUNTIME_DB_PASSWORD`. Set it to a
  strong value (e.g. `openssl rand -base64 24`) in your `.env` before upgrading — the app fails
  fast on start if it is unset rather than silently falling back to the superuser connection. The
  runtime role is provisioned automatically by the startup migration; no manual SQL, no
  `pg_hba.conf` change, and no change to `POSTGRES_USER`/`PWNOTIFY_DATABASE_URL` is needed. See the
  "Upgrading an existing deployment" section in the README.

## [0.2.6] — 2026-07-19

### Security

- **Settings are now validated before they are stored.** A registered setting can declare a
  validation rule, and an invalid value is rejected with a clear error instead of being written.
  This closes several ways a bad or crafted value could weaken a safeguard (see below), and it is
  the foundation the other fixes in this release build on.
- **Branding paths can no longer point outside the branding folder.** A crafted logo or favicon
  path could previously make the public branding endpoints — or the e-mail logo — read an
  arbitrary file on the server (including the key that signs sessions). Branding paths are now
  rejected if they escape the branding folder, and every place that serves or reads a branding
  file re-checks containment, so even a tampered stored value cannot leak a file or reveal whether
  an arbitrary path exists.
- **The audit trail can no longer be wiped through the retention setting.** A tiny or invalid
  retention value is rejected, and a purge that would delete more than half of the audit entries is
  blocked outright — the trail can no longer be erased by a single misconfiguration. "Keep forever"
  (0) still works.
- **The mass-send safety brake can no longer be switched off.** The notification-ratio brake no
  longer accepts a value that disables it, and a second, independent absolute ceiling now caps how
  many notifications a single run may send. A misconfiguration can no longer trigger an
  uncontrolled mass send; an administrator can still deliberately raise the limits.

## [0.2.5] — 2026-07-19

### Security

- **Two-factor enrolment can no longer be hijacked.** Someone who knew a password could use the
  short-lived interim token from the first login step to overwrite an account's active
  authenticator and take the account over. Re-enrolling two-factor now requires disabling it first
  from an authenticated session.
- **The setup connection-test endpoints are protected once an instance is configured.** The
  database, Graph, and mail test endpoints were reachable without authentication after setup was
  complete; they now require an administrator once the first account exists, and stay open only
  during first-time setup. A failed database test no longer echoes raw connection details, and all
  setup routes are rate-limited.
- **A manual run stays within your own customer.** Triggering a run as a customer administrator
  affected every customer on the instance and could return another customer's run. It is now scoped
  to your own tenant; instance-wide runs remain available to the local superadmin.
- **SSO user sync is scoped and no longer discloses other customers.** Syncing SSO users ran across
  every customer and named foreign tenants in the result. It now covers only your own tenant
  (instance-wide sync stays superadmin-only) and reports a blocked-tenant count instead of names.
- **Single sign-on can no longer take over a local account.** An Entra sign-in whose username
  matched an existing local account — including the superadmin — silently converted that account to
  SSO and could lock the real owner out. Such a sign-in is now denied and the local account is left
  untouched.

## [0.2.4] — 2026-07-18

### Fixed

- **The unconfigured-Graph sync error is now fully suppressed.** 0.2.3 skipped the user sync
  cleanly, but a second step — resolving group-based exclusion rules — still built a Graph client
  and surfaced the raw Microsoft library error alongside the localized note. That step now shares
  the same guard, so a tenant without Graph configured produces only the clean skip message.

## [0.2.3] — 2026-07-18

### Fixed

- **No more silent logout while working.** With several tabs open, each tab refreshed its session
  independently and the server treated the second refresh as token reuse, revoking the whole
  session and logging every tab out with no message. Token refresh is now serialized across tabs,
  a page reload no longer discards a still-valid session, and a server-side idle logout now shows
  the inactivity message.
- **A sync no longer fails with a raw error when Microsoft Graph isn't configured.** An unconfigured
  tenant's sync now skips cleanly and reports a short localized "Graph is not configured" note
  instead of a doubled English library error.

### Changed

- **SSO is now provider-only in multi-tenant mode.** Every SSO login is authorized by Team
  membership and homed on the provider (default) tenant; the per-customer SSO settings and the
  access page's SSO tab are hidden for customers. Single-tenant mode and its SSO settings are
  unchanged.
- **Deprovisioning cleans up fully.** When a provider staff member is removed from every Team, the
  next group sync removes their account entirely — no leftover row in the database or the UI —
  under a strict guard that never touches an account still holding a Team or a manual assignment.
- **The customer console's redundant "Settings" tab was removed** (instance settings live under
  Settings).

## [0.2.2] — 2026-07-18

### Added

- **Teams carry a role.** Each Assignment-Group (Team) now has an **Admin** or **Auditor** role,
  chosen when the Team is created or edited and shown as a badge in the list. In multi-tenant
  mode the Team's role — not the member's global role — decides whether its mapped customers grant
  admin or read-only access; when two Teams map the same customer, admin wins.
- **Teams govern SSO for provider staff.** In multi-tenant mode, a provider staff member's SSO
  login is authorized purely by Team membership (no separate admin/auditor login group needed), and
  their role is the highest role across their Teams. The SSO settings role-group fields and the
  access page's "SSO users" tab are hidden in multi-tenant mode, where everything is managed through
  Teams on the Customers page. Single-tenant mode and a customer's own SSO users are unchanged —
  they still use the settings role-groups.

### Security

- Grant materialization stays confined to the single, provider-gated code path: a Team's role only
  ever produces `admin_tenant`/`auditor_tenant` rows for provider accounts, a customer-homed account
  can never gain a foreign grant, and manual assignments are never touched. The SSO
  group-authorization path is reachable only in multi-tenant mode for the provider tenant; every
  other login path is unchanged. Both changes ship with adversarial isolation test matrices.

## [0.2.1] — 2026-07-18

### Fixed

- **SSO admins see their customer's accounts on the access page again.** The account list
  blanket-excluded every SSO caller, so an SSO admin — who by the access model holds their home
  tenant and manages that customer like a local admin — got empty lists. The listing now scopes
  by role and tenant grants exactly as it does for local admins; a forged claim for a tenant the
  caller does not hold stays denied.

### Changed

- **Superadmin management moved to the access page.** The superadmin table is now a third tab on
  the access page, shown only to a superadmin in the provider (default) context, instead of a tab
  in the customer console.

## [0.2.0] — 2026-07-18

Multi-tenancy. One PwNotify instance can now manage many customer Entra tenants with
database-level isolation, a superadmin/admin/auditor access model, group-based access, and
e-mail invitations. Single-tenant installs are unaffected — the mode is off by default.

### Added

- **Multi-tenant mode.** A single toggle in Settings → General turns one instance into a
  multi-customer console. Each customer's data — users, notifications, runs, settings, audit
  log — is isolated at the database level via PostgreSQL Row-Level Security enforced under a
  restricted `NOBYPASSRLS` role, not just filtered in application code. Off by default;
  existing single-tenant installs behave exactly as before.
- **Three-tier access model.** A global **superadmin** (the first-run account, instance-wide),
  **local admins** and **read-only auditors** scoped to one or more customers via explicit
  grants, and **Microsoft-SSO accounts** with a home tenant. A superadmin-only customer
  console manages tenants and assignments; the access page is scoped to the active customer.
- **Teams — group-based access.** Map an Entra security group to one or more customers so
  group membership drives access. PwNotify fetches the group's members from Graph (per-group
  manual sync plus a paginated member list) and materializes access for already-onboarded
  provider accounts; members who haven't signed in yet get access at their first SSO login.
- **E-mail invitations.** Create a local account or a superadmin by e-mail: the invitee opens
  a branded link and sets their own first/last name, username, and password behind a live
  password-policy checklist. Admins can trigger a branded **password-reset link** from the
  access page, and local accounts can edit their own e-mail on the profile page.
- **Profile photos across the user tables.** The Entra users list, the group-members list, and
  the access page show real profile photos when available — Entra directory photos are fetched
  lazily via Graph (`User.Read.All`, no extra permission) and cached, local/SSO account photos
  come from the avatar store — with an initials fallback.
- **Assigned customers as a popover.** A group's assigned customers collapse into a
  click-to-open list instead of a long inline badge row.

### Security

- **Tenant isolation enforced at the database.** Runtime queries run under a restricted DB
  role with per-tenant Row-Level Security, so a bug in application code cannot leak another
  customer's data. Group-based access is materialized only for provider accounts through a
  single gated code path; a customer-homed account can never be cross-granted a foreign
  tenant. Adversarial test matrices assert the isolation invariant directly against the grant
  tables.
- **Hardening.** Atomic single-use invitation and reset tokens (no double-consume),
  rate-limited public token endpoints, provider Graph config read from the default-tenant
  scope (no cross-tenant setting mixing), and path-traversal-guarded photo caching.

### Changed

- **Audit log fully localized.** Every audit action and its detail fields render as readable
  DE/EN text instead of raw keys.

## [0.1.15] — 2026-07-16

### Added

- **Profile page reworked.** Avatar and name now share one *Profile* card (the avatar sits in
  its own bordered box, set off from the name fields), password and two-factor sit side by side
  instead of stacked, and two-factor is a status card with a large shield — green when on, red
  when off — and a red *Disable* button. The name fields carry the same lock-after-save control
  as the settings tabs. The local vs. SSO split is unchanged: SSO accounts still get no name
  editing, avatar upload, password or 2FA — only the profile and their sessions.
- **Protected settings fields lock after saving.** Connection and credential fields — Graph
  (tenant, client id, client secret, group id), all SSO fields, all mail fields — become
  read-only once saved and only reopen via a lock button on the right of the field; they
  re-lock on the next save. Unlocking is admin-only.
- **Notification search and date filter.** The notifications table can be searched by recipient
  address and filtered by a date range; status shows as a coloured badge, and the (uninformative)
  backend column is gone.

### Security

- **The last administrator can no longer be demoted.** Downgrading the only remaining admin to
  auditor would lock everyone out of user management and settings. The role change is now
  refused server-side (`cannot_demote_last_admin`) whenever it would leave zero admins, and the
  access page disables the auditor option for the last admin. Replaces the narrower
  "can't demote yourself" rule, which missed demoting the last admin who happened to be someone
  else.

### Changed

- **Runs page and status indicators reworked.** The runs table is cleaner: the status is a
  single coloured dot instead of a duplicated pill and text, a dry run shows as `Manual · dry
  run` in the trigger column instead of squeezed under the date, counts are right-aligned with
  zeros shown as a faint `–` so real values stand out, and each row carries a relative time
  ("12 minutes ago"). The **run buttons** ("dry run" / "run now") moved to Settings → Schedule,
  next to where the schedule is configured. The old status bar was split up: **next/last run**
  moved to the dashboard, and the **Graph/Mail connection status** now sits as a status line
  inside its own settings tab.
- **Notification history in the user drawer** shows only the latest 5 entries (with a "5 of N"
  note) instead of up to 50 — a quick overview rather than the full log.

### Fixed

- **You could be signed out for inactivity while actually working.** The idle timer on the
  client resets on mouse/keyboard, but the server tracked activity only through token
  refreshes — so on a page without background polling, active reading or scrolling made no API
  calls and the session went stale, logging you out despite the activity. The client now sends
  a lightweight activity ping (at most every 4 minutes) that keeps the server's clock in sync,
  so an actively used session stays alive.
- **Idle sign-outs left no audit entry.** The server-side idle logout deleted the session
  silently; it now records a sign-out with `reason: idle_timeout`, so the log no longer looks
  as if the session just vanished.
- **SSO sign-ins were missing from the audit log.** The OIDC callback issues its tokens
  directly instead of going through the shared login path (it redirects rather than returning
  JSON) and therefore never wrote an entry. With SSO enabled that meant *no* sign-in was
  recorded at all — only sign-outs showed up, which made the log look broken. Successful SSO
  sign-ins are now recorded (with role), and so are **rejected** ones, including the attempted
  username and the reason: an attack on the group mapping would otherwise leave no trace.

## [0.1.14] — 2026-07-16

### Added

- **Two-factor authentication can now be enforced** (Settings → General → Sign-in security,
  off by default). Until now 2FA was purely opt-in: an administrator could not require it, so
  the protection existed only for those who happened to switch it on.
  When enabled, a local account without 2FA gets **no session at all** — after the password
  the path leads straight into enrolment, and tokens are only issued once the first code is
  confirmed. Deliberately stricter than issuing a session and locking the UI afterwards: that
  would hand out a valid access token first. Verified against a running instance: with the
  requirement on, a correct password yields `two_factor_setup_required` and `/api/users`
  answers 401, while enrolment works via the short-lived intermediate token; after activation
  the session exists and the next sign-in asks for the code as usual.
  SSO accounts are exempt — their MFA is Entra's job. Existing sessions keep running until
  they expire.
- **The encryption key can now be rotated without re-entering any secrets.**
  `PWNOTIFY_SECRET_KEY` accepts several keys, comma-separated: encryption always uses the
  first, decryption the first that fits. Put the new key in front, restart, save the settings
  once (or wait until they change anyway), then drop the old one — no downtime. Previously a
  compromised key could only be replaced by re-typing every secret, and until then everything
  looked "not configured". The JWT signing key deliberately follows only the first key, so a
  rotation does not invalidate everyone's sessions.
- **Optional Host-header check** (`PWNOTIFY_ALLOWED_HOSTS`, empty = off). Deliberately open by
  default: too narrow a list makes the app unreachable, while the gain is small — e-mail links
  and cookies come from `PWNOTIFY_BASE_URL`, not the Host header. `127.0.0.1` and `localhost`
  stay allowed so the container healthcheck keeps working.

### Security

- **Personal data is now shortened in logs.** UPNs and recipient addresses were written in
  full; logs are often shipped to a central system and kept for a long time, where complete
  addresses do not belong. They are shortened rather than removed — `pa***@example.com` keeps
  the domain, so a failed send can still be traced. Secrets are still removed entirely, and the
  audit log keeps the clear text for forensics.
- **Uploads are checked against their actual content, not the client's claim.** Only the
  `Content-Type` sent by the browser was verified. For branding uploads that mattered: when
  Pillow cannot process a file the original is kept on purpose (SVGs cannot be rasterised), so
  arbitrary bytes could land under a `.png` name in the served directory. The magic bytes must
  now match the declared type — for logos, favicons and avatars alike.

## [0.1.13] — 2026-07-15

### Added

- **Retention for personal data** (`privacy.user_retention_days`, `privacy.log_retention_days`,
  both default `0` = keep forever). PwNotify mirrors Entra accounts (name, UPN, mail addresses)
  and records every send. Neither was ever deleted: people who left the tenant long ago stayed
  stored in full, and the send history kept their addresses indefinitely. With 1000+ accounts
  that is a data-protection issue, not cosmetics. Accounts that stop appearing in the sync for
  the configured number of days are removed; the send history is trimmed by age.
  Guarded twice, because deletion cannot be undone: retention only runs after a **successful**
  sync (a failed one makes every account look stale at once), and it refuses to delete more
  than half the records in one go. Verified against the database: 5 of 112 departures are
  removed, while a simulated broken sync (100 of 107) is blocked and reported.

### Security

- **TOTP codes are now single-use.** A code stays valid for about 90 seconds (`valid_window=1`),
  so anyone who captured one — shoulder surfing, a recording, malware — could use it a second
  time within that window, which is exactly what the second factor is meant to prevent. The
  consumed time step is now stored per account and rejected on reuse. Verified against a running
  instance: the first sign-in succeeds, the identical code afterwards is refused and recorded in
  the audit log as `totp_replay`. Clock-drift tolerance is unchanged.
- **Rate limit on `/2fa/setup`, `/2fa/enable` and `/2fa/disable`.** Only sign-in was limited, so
  a stolen access token could hammer the disable endpoint with guessed codes until 2FA was off.

### Fixed

- **The top bar showed "PwNotify" instead of "Audit log"** on the audit page. Its title map
  did not know `/audit` and fell back to the default. Display only — the page, its guards and
  the API were unaffected.

## [0.1.12] — 2026-07-15

### Added

- **Audit log** — a new admin-only page (sidebar → *Audit log*) recording who changed what and
  when. There was previously **no record at all**: neither sign-ins, role changes, 2FA changes,
  user creation/deletion nor secret changes left a trace, so "who granted this account admin
  rights?" had no answer. Covers sign-ins (including failures — and the *attempted* username
  when it does not exist, which is how account enumeration becomes visible), account lockouts,
  password changes, 2FA enable/disable, session revocation, user CRUD, role changes (with
  `from` → `to`), and settings/secret changes.
  Entries are append-only: there is no API to edit or delete individual records, and they
  survive deletion of the account that caused them (no foreign key, the name is copied in).
  Secret **values** are never recorded — for settings changes only the keys are, so a changed
  Graph secret shows up as `keys=["graph.client_secret"]` and nothing more. The log is
  filterable by action, outcome and time range, and readable by administrators only —
  auditors get 403. `audit.retention_days` (default `0` = keep forever) prunes old entries
  after each scheduled run for installations that need a deletion deadline.

- **Automatic sign-out after inactivity** (`PWNOTIFY_IDLE_TIMEOUT_MIN`, default 30 minutes,
  `0` disables). Previously a refresh token kept a session alive for up to 14 days, so an
  unattended browser stayed signed in for two weeks. Applies to local and SSO accounts alike,
  and the session row is **deleted**, not just revoked.
  Two layers, because only the client can tell "working" from "tab left open":
  the browser signs out after that long without mouse or keyboard activity (a background tab
  keeps polling, so the server cannot see idleness), and the server ends idle sessions on the
  next token refresh — which covers a closed browser or a stolen token. Activity is shared
  across tabs, so working in one does not time out another. The login page explains the
  automatic sign-out (DE/EN) instead of looking like an error.
- **SSO now works for users in more than 200 groups.** Above that limit Entra stops putting
  the group list into the ID token and sends a reference instead ("overage"). PwNotify treated
  a missing list as "no groups" and refused the sign-in — which hits precisely the accounts
  with many memberships, i.e. the administrators of a large tenant. Membership in the
  configured admin/auditor groups is now checked directly against Graph
  (`checkMemberGroups`) when the token carries no list. No extra permission needed:
  `GroupMember.Read.All` is already required once groups are configured. If the lookup fails,
  the sign-in is still denied — never granted on doubt.
- **Warning before the Graph client secret expires.** Entra secrets expire after 6–24 months.
  When that happened unnoticed the tool went silent: the sync failed and no reminders went
  out — an outage nobody notices, because missing e-mails don't announce themselves. Enter the
  expiry date in Settings → Graph and the dashboard warns 30 days ahead, with the admin digest
  carrying the notice from 14 days on. The date is maintained by hand on purpose: reading it
  automatically would require `Application.Read.All`, which grants read access to **every** app
  registration in the tenant — disproportionate for a warning, and at odds with the
  least-privilege promise in SECURITY.md.
- **Signing out now removes the session record** instead of only marking it revoked.

### Security

- **CI actions pinned to commit SHAs** instead of moving tags. A tag can be repointed at any
  time; if a third-party action were compromised that way, it would run with access to the
  build and its secrets (as in the `tj-actions` incident). The tag stays as a comment so it
  is still readable which version runs, and Dependabot updates pin and comment together.
- **Dependabot** for Docker base images, GitHub Actions, `uv` and npm. Digest-pinned base
  images freeze the CVE state of the day they were pinned — that is exactly how
  CVE-2026-11940 slipped in for 0.1.10 and only surfaced in the CI scan. Alerts and automated
  security fixes were disabled on the repository and are now switched on.
- **Security headers on every response.** The app previously sent none at all: it could be
  embedded in an iframe (clickjacking on the login form and admin actions) and had no
  defence-in-depth against XSS. Added `Content-Security-Policy`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`,
  and HSTS (only when `PWNOTIFY_COOKIE_SECURE=true`, since it is pointless over plain HTTP).
  The CSP uses **no `'unsafe-inline'` for scripts**: `index.html` needs two inline scripts
  (theme before first paint, branding pre-fetch), so their SHA-256 hashes are read from the
  served file at startup and put into the policy — change a script and the hash follows
  automatically, with no hand-maintained list to go stale. `style-src` does allow inline
  styles, which React needs for `style={{…}}`; CSS injection is far less dangerous than
  script execution. Routes that already send a stricter policy (the branding assets with
  their `sandbox` CSP) keep it.

- **Changing your password did not end other sessions.** A stolen refresh token kept full
  access for up to `refresh_token_ttl_days` (14 by default) even after the victim changed the
  password — the one action people take to cut an intruder off. Reproduced against a running
  instance: a second device kept refreshing successfully after the password change. Other
  sessions are now revoked (your own stays), the count is reported back, and the event is
  logged.
- **Validation errors echoed submitted passwords in clear text.** Pydantic attaches the
  rejected input to 422 responses, so a too-short password came back as
  `"input":"..."` — and from there into proxy, browser or monitoring logs. Found while
  testing. Sensitive fields (passwords, client secrets, TOTP codes, tokens) are now stripped
  from validation errors; harmless fields keep their value for debugging.
- **`PWNOTIFY_COOKIE_SECURE` now defaults to `true`.** Forgetting the variable silently
  served auth cookies without the `Secure` flag. Plain-HTTP setups (LAN testing) must now
  opt out explicitly — `docker-compose.yml` for development already does.

### Fixed

- **A crash during a run left it marked "running" forever.** Runs are created as `running` and
  only finalised at the end; if the process died in between (restart, deploy, OOM), the record
  never closed — the history showed a run that never ends and the statistics were off. Such
  runs are now closed on startup with a clear reason. Safe to do: only one run can be active
  per process (`max_instances=1` plus a lock), so nothing real can be open at startup.
- **Every reminder e-mail opened its own connection to Graph.** `send_mail` created a fresh
  HTTP client per message, so each mail paid for a new TCP and TLS handshake. Measured against
  the real Graph endpoint: 29 ms per call without pooling vs. 4 ms with — about 26 ms of pure
  overhead per mail. The client is now reused across a run and closed afterwards (also on
  failure), so the connection is established once instead of once per recipient.
- **CSV/XLSX export blocked the whole server while it ran.** `openpyxl` and `csv` are pure CPU
  work and ran directly in the async handler; with `workers=1` (the scheduler shares that
  process) nothing else was served meanwhile. Measured: ~0.28 s of full blocking per 10,000
  rows — noticeable rather than fatal, but avoidable. Both formats are now built in a worker
  thread; blocking drops to timer granularity and `/health` answers in ~1 ms while an export
  runs.
- **Exports silently truncated at 100,000 rows.** The limit was passed as a page size, so a
  larger tenant would have received a file that looks complete but isn't. Larger exports are
  now rejected with a clear message asking you to filter — a quietly incomplete export is
  worse than an error.
- **A wrong Fernet key looked like "not configured".** Secrets that fail to decrypt were
  silently replaced with an empty string, so a lost or changed `PWNOTIFY_SECRET_KEY` made
  Graph and SMTP appear unconfigured instead of broken — sending you to debug the wrong end.
  Decryption failures are now logged with the affected key and what to check.

## [0.1.11] — 2026-07-15

> ### ⚠️ `.env` change — read before upgrading
>
> This release adds **`PWNOTIFY_TRUSTED_PROXIES`** (default `127.0.0.1`), which controls
> whose `X-Forwarded-For` header may override the client IP. It closes a bypass of the login
> rate limit and account lockout.
>
> Set it to the address the app actually **sees** — which depends on where the proxy runs:
>
> - **No reverse proxy** (direct access, `PWNOTIFY_BIND=0.0.0.0:8080`): **nothing to do.**
>   The default is correct — the header is ignored and the real peer IP counts.
> - **Proxy on a separate server:** enter its LAN IP, e.g.
>   `PWNOTIFY_TRUSTED_PROXIES=10.10.10.200`. Requests from another host keep their source
>   address (DNAT only), so this works as written — the tightest and clearest setup.
>   Requires `PWNOTIFY_BIND=0.0.0.0:8080`; `127.0.0.1` would lock the proxy out entirely.
> - **Proxy on the same host** as PwNotify: use `PWNOTIFY_TRUSTED_PROXIES=172.16.0.0/12`.
>   Docker rewrites the source of host-local requests, so the app sees the **Docker gateway**
>   (`172.x.0.1`) and never the host's own LAN address — entering that address would silently
>   do nothing, leaving all users sharing **one** rate limit.
> - **Proxy as a container**, app port not published: enter the proxy container's IP.
>
> If you get it wrong, access still works — but the header is ignored and everyone shares a
> single login rate limit, so one attacker can lock all users out. Verify after upgrading:
> log in once and check `SELECT ip_address FROM user_session ORDER BY created_at DESC LIMIT 3;`
> — it must show the real client IP, not a gateway. Never use `*`; comma-separated lists and
> CIDR ranges work. `example.env` documents every scenario. No other `.env` changes; the
> mass-send guard (`schedule.max_notify_ratio`) is a database setting with a safe default.

This is a **security release**. Every issue below was reproduced against a running instance
before it was fixed, and each is covered by a regression test (23 → 59 tests).

### Security

- **Login rate limiting and account lockout could be bypassed via `X-Forwarded-For`.**
  Uvicorn ran with `forwarded_allow_ips="*"`, so *any* client — not just a real reverse
  proxy — could override its own source IP. Since both the rate limit and the lockout key
  on the client IP, an attacker rotating that header looked like a new client on every
  request and defeated both. Reproduced: 15 login attempts with a rotating header passed
  unthrottled; the same run now blocks from the 11th.
  A new `PWNOTIFY_TRUSTED_PROXIES` setting controls which peers may set the header,
  defaulting to `127.0.0.1`. Regression tests keep the wildcard from returning.

- **An empty Entra group could delete every SSO administrator, locking you out of your own
  instance.** The SSO sync removes users who are no longer in the admin/auditor group. When
  the group exists but returns no members (emptied group, wrong group ID), the target set was
  empty and *all* SSO users were deleted — including the last administrator. Reproduced against
  a live instance: the sync attempted to delete every SSO account. The sync now refuses to
  remove anything when the target set is empty or when it would remove more than half of all
  SSO users; the run is marked `partial`, logs the reason, and triggers the admin alert, so
  the misconfiguration surfaces instead of failing silently.

- **Stored XSS via SVG logo upload.** SVG is XML and may contain scripts. Uploaded SVGs were
  stored verbatim (raster images go through Pillow, SVGs skipped that path) and served from
  `/api/branding/logo` and `/api/branding/favicon` as `image/svg+xml` — both routes are
  **unauthenticated**. Opening such a URL executed the script in the application's own origin,
  letting it act as the signed-in user (HttpOnly cookies and SameSite=Strict do not help here,
  since the request is same-site). Reproduced against a live instance.
  SVGs containing scripts, event handlers, `javascript:` URLs, `foreignObject`, `iframe` or XML
  entities are now rejected on upload rather than sanitised — a logo needs none of them, and
  sanitisers are easy to bypass. Both routes additionally serve with
  `Content-Security-Policy: … sandbox` and `X-Content-Type-Options: nosniff`, which also
  neutralises files uploaded before this release. Embedding as `<img>`/`<link rel=icon>` —
  how the app itself uses them — is unaffected.

- **No safeguard against mass mis-sending.** The notification loop had no limit of any kind. A
  single wrong setting — validity period, sync group — makes every account look due at once, so
  a tenant with 1000+ users would receive 1000 e-mails before anyone noticed, and sent mail
  cannot be recalled. A run now estimates how many notifications it would send **before** the
  first one goes out and aborts if that exceeds `schedule.max_notify_ratio` (default 50 % of
  all checked users), marking the run `partial` and triggering the admin alert. Small tenants
  are never blocked (3 of 5 users due is real, not a misconfiguration), dry runs are exempt,
  and `0` disables the guard.

- **The second factor could be brute-forced: a wrong TOTP code never locked the account.**
  Failed passwords increment a counter and lock the account; failed 2FA codes did neither, and
  `/auth/2fa/verify` had no lockout check at all. Anyone already holding the password (phishing,
  credential leak) could keep guessing the six-digit code — the IP rate limit alone does not stop
  an attacker with several addresses, which made the second factor largely decorative. Both
  factors now share one lockout path: after `login_max_failures` wrong codes the account locks
  for `login_lockout_min` minutes. Verified against a running instance: guessing locks out from
  the 6th attempt. Lockouts are now also logged (`account_locked`, with the factor), so
  brute-force attempts are visible instead of silent.

### Fixed

- **"Test connection" reported all permissions present while group features failed with 403.**
  The check only compared the token against `User.Read.All`, `Domain.Read.All` and `Mail.Send`,
  but group-scoped sync and SSO role mapping call `/groups/{id}/transitiveMembers`, which needs
  `GroupMember.Read.All`. The very diagnostic you rely on while setting things up was wrong.
  The connection test now additionally requires that permission **once a group is configured**
  (`sync.group_id`, `oidc.admin_group_id` or `oidc.auditor_group_id`) — and stays quiet about
  it otherwise, so instances without group features aren't pushed toward a permission they
  don't need. Documented in README and SECURITY.md, which had omitted it (the in-app Entra
  guide and the Docker Hub page already listed it).

- **Users could not be deleted once they had logged out.** `delete()` only removed *active*
  sessions, while revoked and expired ones kept a foreign key on the account, so deletion
  failed with an integrity error (`user_session_user_id_fkey`). This broke both user management
  and the SSO sync's removal of departed members. All sessions are now removed first, via an
  explicit statement — `AppUser` and `UserSession` have no ORM relationship, so the deletion
  order was otherwise undefined.

### Changed

- **Behaviour change when running behind a reverse proxy.** `X-Forwarded-For` is no longer
  trusted by default. If a proxy sits in front of PwNotify and `PWNOTIFY_TRUSTED_PROXIES`
  is not set to match it, every request appears to come from the proxy — all users then
  share a single rate limit, so one attacker can lock everyone else out. See `example.env`
  for the two supported setups (proxy on the host vs. proxy as a container).

## [0.1.10] — 2026-07-15

### Added

- **Pull-to-refresh** in the installed PWA: pulling down from the top of the page reloads it,
  with a drag indicator and a threshold before it triggers. Standalone mode has no browser
  reload control, so this restores it. Deliberately inactive in a normal browser tab, where
  the reload button and the browser's own pull-to-refresh already exist. Localized (DE/EN).

### Security

- Base image digests updated to Chainguard `python 3.14.6-r3`, which fixes **CVE-2026-11940**
  (HIGH — `tarfile.extractall()` filter bypass). The image is back to 0 known HIGH/CRITICAL CVEs.

## [0.1.9] — 2026-07-14

### Fixed

- User management now shows the role for **SSO users** too (previously only local users
  had a role indicator).

### Changed

- User management groups accounts **by role**: separate Administrators and Auditors tables
  in both the local and the SSO tab.

## [0.1.8] — 2026-07-14

### Added

- **SSO role mapping via Entra groups**: members of the admin group get the `admin` role,
  members of a new optional **auditor group** (`oidc.auditor_group_id`) get the read-only
  `auditor` role (admin wins if in both). The role is applied on SSO login and on the SSO
  user sync; users in neither group are removed. Configurable in Settings → SSO and in the
  first-run setup wizard.

### Changed

- Auditors no longer see **Settings** and **User management** in the sidebar, and those
  routes redirect them to the dashboard.

## [0.1.7] — 2026-07-14

### Added

- **Two-factor authentication (TOTP)** for local accounts: enrol via QR code on the profile
  page, one-time recovery codes, and a two-step sign-in. Secrets are Fernet-encrypted at
  rest (`pyotp`, `qrcode`).
- **Roles**: `admin` (full) and read-only `auditor`. All write endpoints require admin
  (`RequireAdmin`); the UI hides write actions for auditors. Role is selectable when creating
  a local user and changeable in user management.
- **Admin notifications**: an optional digest after each scheduled run plus immediate
  failure alerts, sent to a configurable recipient list (bilingual DE/EN email).

### Database

- Migration adds `app_user.totp_secret`, `totp_enabled`, `recovery_codes` (runs automatically
  on start).

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
- Stack: `i18next`, `react-i18next`, `i18next-browser-languagedetector` (pinned in `frontend/package.json`).

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

[0.3.1]: https://github.com/amslertec/pwnotify/releases/tag/v0.3.1
[0.3.0]: https://github.com/amslertec/pwnotify/releases/tag/v0.3.0
[0.2.9]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.9
[0.2.8]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.8
[0.2.7]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.7
[0.2.6]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.6
[0.2.5]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.5
[0.2.4]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.4
[0.2.3]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.3
[0.2.2]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.2
[0.2.1]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.1
[0.2.0]: https://github.com/amslertec/pwnotify/releases/tag/v0.2.0
[0.1.15]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.15
[0.1.14]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.14
[0.1.13]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.13
[0.1.12]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.12
[0.1.11]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.11
[0.1.10]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.10
[0.1.9]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.9
[0.1.8]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.8
[0.1.7]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.7
[0.1.6]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.6
[0.1.5]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.5
[0.1.4]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.4
[0.1.3]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.3
[0.1.2]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.2
[0.1.1]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.1
[0.1.0]: https://github.com/amslertec/pwnotify/releases/tag/v0.1.0
