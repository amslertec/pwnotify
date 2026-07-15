# Security

PwNotify is designed to run as a hardened, self-hosted container. This document
describes its security posture, the known-CVE handling, and how to report issues.

## Reporting a vulnerability

Please report security issues privately to **pascal.amsler@amslertec.ch**. Do not
open public GitHub issues for security-relevant findings.

## Container hardening

| Control | Implementation |
|---|---|
| Base image | `cgr.dev/chainguard/python:latest` — **Trivy-verified 0 HIGH / 0 CRITICAL** (see below) |
| Non-root | Runs as UID/GID **65532**; `USER` set in the image |
| Read-only root FS | `read_only: true` in compose; only `/data` (volume) and `/tmp` (tmpfs) are writable |
| Dropped capabilities | `cap_drop: [ALL]` |
| No privilege escalation | `security_opt: [no-new-privileges:true]` |
| Minimal image | Multi-stage build; final image contains no Node, build tools, uv, compilers, tests, `.git`, or package caches |
| Signal handling | `ENTRYPOINT` in exec form; Python is PID 1, SIGTERM triggers a graceful scheduler shutdown |
| Healthcheck | Pure-Python `HEALTHCHECK` (no shell in the runtime image) |
| SBOM + provenance | Generated in CI via `docker buildx --sbom=true --provenance=true` |

## Application security

- **Password hashing:** Argon2id (`argon2-cffi`) with automatic rehash-on-login.
- **Sessions:** JWT in `HttpOnly`, `SameSite=Strict`, `Secure` (behind TLS) cookies.
  Refresh tokens are **rotated** on every use; only their SHA-256 hash is stored.
  Reuse of a rotated/revoked refresh token revokes the entire session family.
- **Brute-force protection:** per-IP rate limiting on `/api/auth/login` (slowapi) plus
  an account lockout after repeated failures.
- **Secrets at rest:** Graph client secret and SMTP password are encrypted with
  **Fernet** (`cryptography`) before being written to the database. The master key
  comes from `PWNOTIFY_SECRET_KEY` or an auto-generated `/data/secret.key` (mode 0600).
- **Secret masking:** secrets are never returned to the frontend — the API emits a
  `__SECRET_SET__` marker instead; they are never written to logs (structlog redaction).
- **Template rendering:** e-mail templates render in a **sandboxed** Jinja2 environment.
- **Fault isolation:** a Graph or mail error for one user is logged and the run
  continues; a single failure never aborts a scheduler run.

## Known CVEs

As of the last build there are **no known unfixed HIGH or CRITICAL CVEs** in the
runtime image. The base image was chosen specifically for this: the previously
considered `python:3.14-slim-bookworm` carried 21 unfixable Debian base-package CVEs
(perl, util-linux/ncurses, zlib, libsqlite3) that made a Docker Scout A rating
impossible; `python:slim` was therefore replaced with Chainguard/Wolfi. See
`VERSIONS.md` for the decision record.

CI fails the build on any HIGH/CRITICAL finding (Trivy `--exit-code 1`, Docker Scout
policy gate), so this file is kept accurate automatically. If a future dependency
introduces an unavoidable, unfixed CVE, it will be documented here with a justification
and, where appropriate, a `.trivyignore` entry referencing this section.

## Required Microsoft Graph permissions (least privilege)

PwNotify requests three **application** permissions, plus one that is only needed for
optional group features:

| Permission | Purpose |
|---|---|
| `User.Read.All` | Read users, UPN, `lastPasswordChangeDateTime`, `passwordPolicies` |
| `Domain.Read.All` | Read `passwordValidityPeriodInDays` per domain |
| `Mail.Send` | Send reminder e-mails (Graph backend only) |
| `GroupMember.Read.All` | **Optional** — only when the sync is scoped to a group or Microsoft SSO maps admin/auditor groups. Read group members. |

It never requests write access to directory objects. The connection test only demands
`GroupMember.Read.All` once a group is actually configured, so instances that don't use
group features are not pushed toward a permission they don't need.

`Mail.Send` (application) allows sending as **any** mailbox in the tenant. To limit the
blast radius of a leaked client secret, scope it to the single sender mailbox with an
[application access policy](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access):

```powershell
New-ApplicationAccessPolicy -AppId <client-id> `
  -PolicyScopeGroupId <mail-enabled-security-group> `
  -AccessRight RestrictAccess -Description "PwNotify: sender mailbox only"
```
