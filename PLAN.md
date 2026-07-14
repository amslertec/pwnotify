# PLAN.md — PwNotify

Password Expiry Notification Tool für Microsoft Entra ID (Azure AD).
Self-hosted, Docker-Hub-compliant (Docker Scout A-Rating angestrebt), 2-Container-Deployment.

---

## 1. Architektur-Entscheidungen (bestätigt)

| Thema | Entscheidung |
|---|---|
| Deployment-Topologie | **2 Container**: `pwnotify` (FastAPI serviert `/api/*` **und** die gebauten React-Statics als SPA-Fallback) + `postgres`. |
| Backend Base-Image | `cgr.dev/chainguard/python:latest` (digest-gepinnt, Python 3.14.6), Multi-Stage. **Trivy-verifiziert 0 HIGH/0 CRITICAL.** Runtime enthält kein Node, keine Build-Tools, kein uv, kein Compiler, keine Shell. non-root uid 65532. |
| Frontend | React 19 + TS 5.9 + Vite 8 + Tailwind v4 + shadcn/ui, als statisches Bundle in das Backend-Image kopiert. |
| Secret-at-rest | Fernet. Master-Key aus `PWNOTIFY_SECRET_KEY` **oder** auto-generiert nach `/data/secret.key` (0600) beim ersten Start. |
| Prozessmodell | Single-Worker Uvicorn (verhindert doppelte APScheduler-Jobs); Scheduler läuft im selben Prozess mit lifespan-Hook. |
| Persistenz | Named Volume auf `/data` (secret.key, Logo/Favicon-Uploads). Root-FS **read-only**, `/tmp` als tmpfs. |
| Config | Alle Settings in DB (verschlüsselt wo geheim). ENV nur **Initial-Seed** beim allerersten Start. |

## 2. Ordnerstruktur

```
pwnotify/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI-App, lifespan (scheduler start/stop), static mount
│   │   ├── core/                  # config(pydantic-settings), logging(structlog), security(argon2,jwt,fernet), errors
│   │   ├── db/                    # engine(async), session, base
│   │   ├── models/                # SQLModel-Tabellen (s. §4)
│   │   ├── schemas/               # Pydantic Request/Response-DTOs (Secret-Masking)
│   │   ├── repositories/          # DB-Zugriff, keine Business-Logik
│   │   ├── services/
│   │   │   ├── graph/             # msal-Auth, httpx-Client, Pagination, Throttling, Delta, Domain-Policy
│   │   │   ├── expiry.py          # Ablaufberechnung (rein, unit-getestet)
│   │   │   ├── notifier.py        # Reminder-Stufen, Dedup, Empfänger-Strategie
│   │   │   ├── mail/              # graph_sender.py, smtp_sender.py (gemeinsames Interface)
│   │   │   ├── templating.py      # Jinja2 Sandbox, DE/EN, Platzhalter, Vorschau
│   │   │   └── scheduler.py       # APScheduler, Run-Protokollierung
│   │   ├── api/
│   │   │   ├── deps.py            # Auth-Deps, DB-Session, Rate-Limit
│   │   │   └── routes/           # auth, setup, users, notifications, runs, settings, dashboard, branding, health
│   │   └── seed.py                # ENV → DB Initial-Seed
│   ├── alembic/                   # Migrationen
│   ├── tests/                     # pytest (Graph gemockt via respx)
│   ├── pyproject.toml + uv.lock
│   └── mypy.ini / ruff via pyproject
├── frontend/
│   ├── src/
│   │   ├── main.tsx / App.tsx / router.tsx
│   │   ├── lib/ (api-client, query-client, utils, theme, branding-css-vars)
│   │   ├── components/ui/         # shadcn-Primitives
│   │   ├── components/            # Sidebar, Topbar, KpiCard, DataTable, Charts, Drawer, Skeletons, EmptyState
│   │   ├── pages/                 # Dashboard, Users, Notifications, Runs, Settings/*, Login, Setup
│   │   └── hooks/
│   ├── package.json + pnpm-lock.yaml
│   ├── vite.config.ts / tsconfig / eslint / prettier / vitest
│   └── index.html
├── Dockerfile                     # Multi-Stage: fe-build(node) → py-build(uv) → runtime(slim, non-root)
├── docker-compose.yml             # compose-spec, healthchecks, security_opt, cap_drop, read_only, limits
├── .dockerignore  .env.example  Makefile
├── .github/workflows/ci.yml       # build, test, lint, trivy, scout, multi-arch push, sbom+provenance
├── README.md  VERSIONS.md  SECURITY.md  CHANGELOG.md  LICENSE(MIT)
```

## 3. Layering (erzwungen)
`routes` (nur I/O, Validierung, Auth) → `services` (Business-Logik) → `repositories` (DB) → `models`.
Keine Graph-/Mail-/Business-Logik in Routes. `expiry.py` und `notifier.py` sind pur und ohne I/O testbar.

## 4. Datenmodell (SQLModel + Alembic)

- **app_user** — lokale UI-Accounts: username, argon2id-hash, role, created_at.
- **session** — Refresh-Token-Familien (Rotation), user-agent/ip, revoked, expires_at.
- **setting** — key/value (JSON), `is_secret` → Fernet-verschlüsselt at-rest.
- **entra_user** — Spiegel aus Graph: entra_id(uniq), upn, display_name, mail, other_mails(JSON), account_enabled, last_password_change, password_policies, department, job_title, computed **expiry_date**, **days_left**, excluded(bool), last_synced_at.
- **notification_log** — user_id, reminder_day(stufe), channel(primary/alternate), backend(graph/smtp), recipient, status(sent/failed), error, sent_at. **Unique(entra_user, reminder_day, expiry_cycle)** → Dedup.
- **run** — started_at, finished_at, duration, checked_users, sent, failed, dry_run, status, detail_log(JSON).
- **exclusion** — user- oder gruppen-basiert.
- **branding** — app_name, company_name, primary_color, logo_path, favicon_path, reset_url.

## 5. First-Time-Setup-Wizard (dein Wunsch, erweitert)
Erscheint solange **kein** `app_user` existiert; danach gesperrt. Schritte:
1. **Datenbank** — DATABASE_URL aus ENV vorbefüllt, „Verbindung testen", dann **Alembic-Migrationen anwenden** (Button/auto). Status grün/rot.
2. **Admin-Account** — Username + Passwort (Argon2id, Passwort-Policy, Bestätigung).
3. **Graph** — Tenant/Client-ID/Secret → „Verbindung testen" zeigt erkannte Permissions.
4. **Mail** — Backend (Graph/SMTP), Absender, Test-Mail an beliebige Adresse.
5. **Fertig** — Zusammenfassung, Weiterleitung ins Dashboard.

> Migrationen laufen im Container-Entrypoint ohnehin automatisch (idempotent); der Wizard-DB-Schritt zeigt/verifiziert nur und erlaubt Re-Run.

## 6. Sicherheit & Compliance (A-Rating)
- Non-root UID/GID **10001**, `USER` gesetzt; read-only Root-FS; tmpfs `/tmp`; `no-new-privileges`, `cap_drop: [ALL]`.
- Multi-Stage: Final-Image = slim + venv + Statics, **keine** Build-Tools/Caches/.git/Tests/node_modules.
- HEALTHCHECK im Dockerfile (`/health`); `/ready` prüft DB (+ optional Graph).
- Vollständige OCI-Labels inkl. base.name/base.digest; exec-form CMD (SIGTERM sauber → graceful scheduler shutdown, tini falls nötig).
- Secrets: nie in ENV-Defaults, nie im Image, in Responses maskiert, at-rest Fernet-verschlüsselt, nie im Log (structlog-Redaction).
- Auth: Argon2id, JWT in httpOnly/SameSite=Strict/Secure-Cookie, Refresh-Rotation, slowapi-Rate-Limit + Brute-Force-Lockout am Login.
- OIDC (Entra) als optionales, gekapseltes Modul vorbereitet (Feature-Flag).
- `.dockerignore` vollständig; `--no-install-recommends`, apt-lists gelöscht, kein blindes `apt upgrade`.
- CI failt bei kritischen/hohen CVEs (Trivy + Scout); SBOM + Provenance via buildx; Multi-Arch amd64+arm64.

## 7. Kernlogik-Details
- **Ablaufberechnung**: `expiry = lastPasswordChange + validityDays`; validityDays aus Domain `passwordValidityPeriodInDays` (Graph) mit Settings-Override; `DisablePasswordExpiration`/disabled → „kein Ablauf" (grau), aber sichtbar.
- **Graph**: `$select` minimal, `@odata.nextLink`-Pagination, 429 → Retry-After/exponential backoff (tenacity), Batch wo sinnvoll, Delta-Query geprüft.
- **Notifier**: konfigurierbare Reminder-Tage (Chips), pro User+Stufe+Zyklus genau einmal (Unique-Constraint), Strategie primary|alternate|both|alternate_fallback_primary, Dry-Run.
- **Fehler-Isolation**: Graph-/Mail-Fehler pro User geloggt, Lauf läuft weiter, nie Job-Kill.
- **Scheduler**: Cron+TZ aus Settings (Default `0 8 * * *`, Europe/Zurich), „Jetzt ausführen", Run-Protokoll.

## 8. GUI
Fixe kollabierbare Sidebar (Zustand persistiert) + Topbar (Breadcrumb, Suche, Theme-Toggle, User-Menü). Dark/Light + System, persistiert. Design-System: Spacing/Type/Radius/Elevation-Scale, Skeleton-Loader, Empty States, Toasts (sonner), WCAG-AA, Keyboard-Nav. Primärfarbe aus Branding setzt CSS-Variablen (inkl. Charts). Seiten: Dashboard, Users (TanStack Table server-side + Drawer + Bulk + CSV/XLSX), Notifications, Runs, Settings (Tabs: Graph/Mail/Schedule/Password-Policy/Branding/Template/Account), Login, Setup-Wizard.

## 9. Bau-Reihenfolge (Zwischenstand nach jedem Block)
1. **Gerüst + Docker**: Repo-Struktur, Dockerfile, compose, `.dockerignore` → sofort `trivy image` gegen Base + Skeleton-Runtime (**0 High/Critical verifizieren**), finale Grösse ausweisen.
2. **Datenmodell + Alembic** (+ Seed).
3. **Services**: Graph, Mail (Graph+SMTP), Templating, Expiry, Notifier, Scheduler — mit Unit-Tests (Kernlogik).
4. **API**: auth/setup/users/notifications/runs/settings/dashboard/branding/health.
5. **Frontend** Seite für Seite (Setup-Wizard → Login → Dashboard → Users → Notifications → Runs → Settings).
6. **CI**, README/SECURITY/CHANGELOG, Makefile.
7. **Abschluss**: Trivy + Scout Scan, Image-Grösse, Compliance-Checkliste abhaken.

## 10. Deliverables
Repo, docker-compose.yml, Dockerfile(s), .env.example, Alembic-Migrationen, VERSIONS.md, SECURITY.md, LICENSE(MIT), CHANGELOG.md, README (inkl. Entra-App-Registration Schritt-für-Schritt + Permissions + Admin-Consent, Reverse-Proxy/Traefik-Beispiel, Backup/Restore, Troubleshooting, Version Decisions), GitHub Actions, Makefile.
