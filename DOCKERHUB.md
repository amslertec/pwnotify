# PwNotify

**Password Expiry Notification Tool for Microsoft Entra ID (Azure AD).**

PwNotify checks your Entra ID users via Microsoft Graph, computes when each password
expires, and sends staged reminder e-mails — to the primary mailbox, the alternate (SSPR)
address, or both. Self-hosted, hardened, and driven by a modern admin UI with a first-run
setup wizard.

```
docker pull amslertec/pwnotify:0.1.0
```

---

## Highlights

- **Graph sync** — client-credentials flow, pagination, 429 throttling, per-domain
  password-validity detection. Optionally scope the sync to a single Entra **group**
  (dynamic or static) so only the right users are checked.
- **Staged reminders** — configurable days before expiry (default 14/7/3/1/0), deduplicated
  per user + stage + expiry cycle, with catch-up after downtime.
- **Two mail backends** — Microsoft Graph `sendMail` or SMTP, switchable at runtime.
- **Modern UI** — dashboard, users table with CSV/XLSX export, notification & run history,
  editable DE/EN templates, branding, local + Microsoft-SSO logins, profile avatars.
- **Hardened & compliant** — Chainguard base (**0 known HIGH/CRITICAL CVEs**), non-root,
  read-only rootfs, `no-new-privileges`, all capabilities dropped, SBOM + provenance
  attestations, multi-arch (amd64 + arm64).

## Supported tags

| Tag | Meaning |
|---|---|
| `0.1.0` | Pinned release (recommended for production) |
| `latest` | Most recent release |

Multi-arch manifests: **linux/amd64** and **linux/arm64**.

---

## Quick start (Docker Compose)

You need two files on the target server: `docker-compose-prod.yml` and `example.env`
(copy them from the source repository — see below for their full contents).

```bash
# 1. Place docker-compose-prod.yml and example.env in a directory, then:
cp example.env .env
#   -> edit .env:  POSTGRES_PASSWORD, PWNOTIFY_BASE_URL, PWNOTIFY_COOKIE_SECURE

# 2. Pull and start
docker compose -f docker-compose-prod.yml pull
docker compose -f docker-compose-prod.yml up -d

# 3. Watch it become healthy
docker compose -f docker-compose-prod.yml ps
```

> Both files live in the source repository (`docker-compose-prod.yml`, `example.env`).
> Copy them to the server via `scp`/clone, then follow the steps above.

Then open the app (default `http://localhost:8080`) — the **first-run setup wizard** guides
you through database check → admin account → Microsoft Graph (with a built-in Entra
registration guide) → mail backend.

> The bundled PostgreSQL is reachable only on the internal Docker network. By default the
> app binds to `127.0.0.1:8080` — put a TLS reverse proxy in front, or set
> `PWNOTIFY_BIND=0.0.0.0:8080` in `.env` to expose it on your LAN.

### Minimal run without Compose

PwNotify needs a PostgreSQL database. If you already have one:

```bash
docker run -d --name pwnotify \
  -p 127.0.0.1:8080:8080 \
  -e PWNOTIFY_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/pwnotify" \
  -e PWNOTIFY_BASE_URL="https://pwnotify.example.com" \
  -e PWNOTIFY_COOKIE_SECURE=true \
  -v pwnotify-data:/data \
  --read-only --tmpfs /tmp \
  --security-opt no-new-privileges:true --cap-drop ALL \
  amslertec/pwnotify:0.1.0
```

---

## Configuration

Environment variables are an **initial seed** only — after the first start everything is
managed in the database via the Settings UI. Secrets are Fernet-encrypted at rest.

| Variable | Default | Description |
|---|---|---|
| `PWNOTIFY_DATABASE_URL` | — | **Required.** Async DSN, e.g. `postgresql+asyncpg://user:pass@db:5432/pwnotify` |
| `PWNOTIFY_SECRET_KEY` | auto | Fernet master key. Empty → generated into `/data/secret.key` |
| `PWNOTIFY_BASE_URL` | `http://localhost:8080` | Public URL (e-mail links, cookies, OIDC redirect) |
| `PWNOTIFY_COOKIE_SECURE` | `true` | Require HTTPS cookies (set `false` for plain HTTP) |
| `PWNOTIFY_TIMEZONE` | `Europe/Zurich` | Scheduler timezone |
| `PWNOTIFY_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `PWNOTIFY_LOG_JSON` | `true` | JSON logs (`false` = colored console) |
| `PWNOTIFY_ADMIN_USERNAME` / `_PASSWORD` | — | Optional first-admin seed (else via wizard) |
| `PWNOTIFY_GRAPH_TENANT_ID` / `_CLIENT_ID` / `_CLIENT_SECRET` | — | Optional Graph seed |
| `PWNOTIFY_MAIL_BACKEND` | `graph` | `graph` \| `smtp` |
| `PWNOTIFY_MAIL_FROM` | — | Sender address |

See `example.env` for the full list.

### Ports & volumes

| | |
|---|---|
| **Port** | `8080` (HTTP, app + API) |
| **Volume** `/data` | Fernet key, uploaded logos/favicons, avatars — **back this up** |
| DB volume `pgdata` | PostgreSQL data directory |

### Entra app registration

Create an app registration (client-credentials) with these **application** permissions and
grant admin consent:

| Permission | Purpose |
|---|---|
| `User.Read.All` | Read users, UPN, last password change (+ SSO profile photos) |
| `Domain.Read.All` | Read per-domain password validity |
| `Mail.Send` | Send reminder e-mails via Graph |
| `GroupMember.Read.All` | *Optional* — only for group-scoped sync and/or Microsoft SSO |

The setup wizard and the Settings → Graph tab contain a step-by-step guide.

---

## Security & compliance

This image is built for a Docker Scout **A rating**:

- **0 known HIGH/CRITICAL CVEs** — Chainguard/Wolfi minimal base, no shell, no package
  manager, no compilers in the runtime image.
- **Non-root** — runs as UID `65532`.
- **Read-only root filesystem** compatible (`--read-only` + a small `tmpfs`).
- **Multi-stage build** — no build tools, Node, `uv`, tests, or VCS metadata in the runtime.
- **Digest-pinned base image**, full OCI labels.
- **SBOM + provenance** (`mode=max`) attestations attached to the manifest.

Inspect the attestations yourself:

```bash
docker scout cves amslertec/pwnotify:0.1.0            # vulnerability report
docker buildx imagetools inspect amslertec/pwnotify:0.1.0   # platforms + attestations
docker scout sbom amslertec/pwnotify:0.1.0            # software bill of materials
```

---

## Upgrade

```bash
docker compose -f docker-compose-prod.yml pull
docker compose -f docker-compose-prod.yml up -d
```

Database schema migrations (Alembic) run automatically on start.

## Backup

Back up two things: the **`/data` volume** (Fernet key + uploads) and the **PostgreSQL
database**.

```bash
# Database dump
docker compose -f docker-compose-prod.yml exec db \
  pg_dump -U pwnotify pwnotify > pwnotify-backup.sql

# /data volume (tar)
docker run --rm -v pwnotify_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/pwnotify-data.tgz -C /data .
```

> If you lose `/data/secret.key` without a `PWNOTIFY_SECRET_KEY` set, the encrypted
> secrets in the database can no longer be decrypted. Keep it (or an explicit key) safe.

---

## License

MIT © amslertec — source: <https://github.com/amslertec/pwnotify>
