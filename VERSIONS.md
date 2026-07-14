# VERSIONS.md — PwNotify

> Alle Versionen wurden am **2026-07-13** live gegen PyPI, npm und die Docker-Registry
> geprüft (nicht aus Trainingswissen abgeleitet). Alle Versionen werden **exakt gepinnt**
> (kein `^`, kein `~`, kein `:latest`); Lockfiles (`uv.lock`, `pnpm-lock.yaml`) werden committed.
> Base-Images werden zusätzlich per **`@sha256:`-Digest** gepinnt.

## Runtimes & Base-Images

| Komponente | Gewählte Version | Geprüft | Begründung falls nicht neueste |
|---|---|---|---|
| Python | **3.14.6** (Chainguard) | 2026-07-13 | Neueste stabile Major. cp314-Wheels für **alle** C-Extension-Deps vorhanden (asyncpg, cryptography, pydantic-core, greenlet, argon2) → kein Compiler im Final-Image. |
| Backend Base-Image (build) | `cgr.dev/chainguard/python:latest-dev@sha256:93a58bdb02c7c37785752cfab31031331448ab84aeab5d14ca101b381bc49577` | 2026-07-13 | Wolfi/glibc, enthält Shell + apk + Python 3.14.6 für venv-Build mit uv. |
| Backend Base-Image (runtime) | `cgr.dev/chainguard/python:latest@sha256:4d908c6a44ba22460e34a2f6dd665b8fcb82bd3e6c887e749bd6fef243e10094` | 2026-07-13 | **Trivy-verifiziert 0 HIGH / 0 CRITICAL** (2026-07-13). Python 3.14.6, non-root uid **65532**, keine Shell/kein Paketmanager. Free-tier: `:latest` per `@sha256`-Digest gepinnt = reproduzierbar. **Gewechselt von python:slim, weil dessen 21 ungefixte Debian-CVEs (perl/util-linux/zlib/sqlite) kein A-Rating zulassen.** |
| Node (nur Build-Stage) | **24 LTS** (`24-bookworm-slim@sha256:cb4e8f7c443347358b7875e717c29e27bf9befc8f5a26cf18af3c3dec80e58c5`) | 2026-07-13 | Node 26 ist neueste LTS, aber Node 24 LTS ist maximal stabil und erfüllt Vite-8-Engine `>=22.12`. **Node landet nicht im Runtime-Image** (Backend serviert Statics) → keine Runtime-CVE-Fläche. |
| PostgreSQL | **18.4** (`18-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15`) | 2026-07-13 | Neueste stabile Major (EOL 2030-11). Alpine-Variante = kleinste Angriffsfläche. Offizielles Image. |
| uv (Python-PM) | latest, im Dockerfile digest-gepinnt via `ghcr.io/astral-sh/uv` | 2026-07-13 | Nur Build-Stage. |
| pnpm (Node-PM) | via `corepack` an exakte Version gepinnt | 2026-07-13 | Nur Build-Stage. |

## Backend (Python) — direkte Abhängigkeiten

| Paket | Version | Geprüft | Anmerkung |
|---|---|---|---|
| fastapi | 0.139.0 | 2026-07-13 | verlangt starlette>=0.46, pydantic>=2.9 ✅ |
| uvicorn[standard] | 0.51.0 | 2026-07-13 | Single-Worker (APScheduler-Duplikatvermeidung) |
| sqlmodel | 0.0.39 | 2026-07-13 | verlangt SQLAlchemy `>=2.0.14,<2.1`, pydantic `>=2.11` ✅ passt zu unten |
| sqlalchemy | 2.0.51 | 2026-07-13 | 2.x async |
| alembic | 1.18.5 | 2026-07-13 | |
| asyncpg | 0.31.0 | 2026-07-13 | cp314-Wheel ✅ |
| greenlet | 3.5.3 | 2026-07-13 | SQLAlchemy-async Requirement |
| pydantic | 2.13.4 | 2026-07-13 | (pydantic-core 2.46.4) |
| pydantic-settings | 2.14.2 | 2026-07-13 | ENV-Seed-Konfig |
| apscheduler | 3.11.3 | 2026-07-13 | 4.0 noch pre-release → bewusst stabile 3.x-Linie |
| httpx | 0.28.1 | 2026-07-13 | Graph-Client (async) |
| msal | 1.37.0 | 2026-07-13 | Client-Credentials + Cert-Auth |
| cryptography | 49.0.0 | 2026-07-13 | Fernet (Secret-at-rest) |
| argon2-cffi | 25.1.0 | 2026-07-13 | Passwort-Hashing (Argon2id) |
| pyjwt | 2.13.0 | 2026-07-13 | JWT Access/Refresh |
| jinja2 | 3.1.6 | 2026-07-13 | E-Mail-Templates |
| structlog | 26.1.0 | 2026-07-13 | JSON-Logging |
| tenacity | 9.1.4 | 2026-07-13 | Retry/Backoff (Graph 429) |
| slowapi | 0.1.10 | 2026-07-13 | Login-Rate-Limiting |
| orjson | 3.11.9 | 2026-07-13 | schnelle JSON-Serialisierung |
| openpyxl | 3.1.5 | 2026-07-13 | XLSX-Export |
| python-multipart | 0.0.32 | 2026-07-13 | Logo/Favicon-Upload |
| email-validator | 2.3.0 | 2026-07-13 | |
| tzdata | 2026.3 | 2026-07-13 | Zeitzonen-DB — Chainguard/distroless enthalten **keine** System-tzdata; `zoneinfo` fällt auf dieses Paket zurück (APScheduler-Timezones). |
| **dev:** ruff | 0.15.21 | 2026-07-13 | Lint+Format |
| **dev:** mypy | 2.3.0 | 2026-07-13 | strict |
| **dev:** pytest | 9.1.1 | 2026-07-13 | |
| **dev:** pytest-asyncio | 1.4.0 | 2026-07-13 | |
| **dev:** respx | 0.23.1 | 2026-07-13 | httpx-Mock für Graph-Tests |

## Frontend (Node/React) — direkte Abhängigkeiten

| Paket | Version | Geprüft | Anmerkung |
|---|---|---|---|
| react / react-dom | 19.2.7 | 2026-07-13 | |
| typescript | **5.9.3** | 2026-07-13 | ⚠ **Nicht 7.0.2**: TS 7 (nativer Go-Port) wird von `typescript-eslint` (peer `>=4.8.4 <6.1.0`) noch nicht unterstützt; TS 6.0 ist npm-`beta`-getaggt. 5.9.3 = neueste voll & stabil unterstützte Linie. Siehe „Version Decisions" im README. |
| vite | 8.1.4 | 2026-07-13 | Engine `>=22.12` → Node 24 Builder |
| @vitejs/plugin-react | 6.0.3 | 2026-07-13 | peer vite ^8 ✅ |
| tailwindcss | 4.3.2 | 2026-07-13 | v4 CSS-first, via Vite-Plugin |
| @tailwindcss/vite | 4.3.2 | 2026-07-13 | peer vite ^5\|\|6\|\|7\|\|8 ✅ |
| @tanstack/react-query | 5.101.2 | 2026-07-13 | |
| @tanstack/react-table | 8.21.3 | 2026-07-13 | |
| recharts | 3.9.2 | 2026-07-13 | react 19 ✅ |
| lucide-react | 1.24.0 | 2026-07-13 | v1 (vormals 0.x) |
| react-router-dom | 7.18.1 | 2026-07-13 | |
| react-hook-form | 7.81.0 | 2026-07-13 | |
| @hookform/resolvers | 5.4.0 | 2026-07-13 | |
| zod | 4.4.3 | 2026-07-13 | |
| i18next | 26.3.6 | 2026-07-14 | i18n-Kern |
| react-i18next | 17.0.9 | 2026-07-14 | react 19 ✅ (peer i18next ≥26.2) |
| i18next-browser-languagedetector | 8.2.1 | 2026-07-14 | Sprach-Erkennung (Pre-Login) |
| class-variance-authority | 0.7.1 | 2026-07-13 | shadcn-Baustein |
| clsx | 2.1.1 | 2026-07-13 | |
| tailwind-merge | 3.6.0 | 2026-07-13 | |
| @radix-ui/react-* (dialog 1.1.19 u.a.) | current | 2026-07-13 | shadcn/ui-Primitives, exakt in pnpm-lock |
| sonner | 2.0.7 | 2026-07-13 | Toasts |
| next-themes | 0.4.6 | 2026-07-13 | Dark/Light-Persistenz |
| date-fns | 4.4.0 | 2026-07-13 | |
| **dev:** eslint | 10.7.0 | 2026-07-13 | |
| **dev:** typescript-eslint | 8.63.0 | 2026-07-13 | peer eslint ^10 ✅, typescript <6.1 → begrenzt TS-Wahl |
| **dev:** prettier | 3.9.5 | 2026-07-13 | |
| **dev:** vitest | 4.1.10 | 2026-07-13 | |
| **dev:** @testing-library/react | 16.3.2 | 2026-07-13 | |
| **dev:** @types/react | 19.2.17 | 2026-07-13 | |
| **dev:** @types/node | 24.x (exakt in pnpm-lock) | 2026-07-13 | an Node-24-Builder ausgerichtet |

> Exakte Patch-Pins der transitiven Abhängigkeiten stehen in `uv.lock` bzw.
> `pnpm-lock.yaml` (beide committed).

## Version Decisions & bewusste Abweichungen

| Thema | Entscheidung | Begründung |
|---|---|---|
| Base-Image | **Chainguard/Wolfi** statt `python:slim` | `python:slim` trug 21 ungefixte HIGH/CRITICAL Debian-CVEs → kein A-Rating. Chainguard = Trivy-verifiziert 0/0 (Image gebaut & gescannt 2026-07-13). |
| TypeScript | **5.9.3** statt 7.0.2 | TS 7 (nativer Go-Port) wird von `typescript-eslint` (peer `<6.1.0`) nicht unterstützt; TS 6.0 ist npm-`beta`. 5.9.3 = neueste voll stabile Linie. |
| WSGI/ASGI-Server | **Uvicorn single-worker**, kein gunicorn | Mehrere Worker würden den In-Process-APScheduler duplizieren (doppelte Jobs). Ein Worker + Scheduler im Lifespan. |
| APScheduler | **3.11.3** statt 4.x | 4.0 ist noch pre-release. |
| Users-Tabelle | **Server-side Custom-Table** (TanStack Query), kein `@tanstack/react-table` | Bei server-seitiger Sortierung/Filterung/Pagination bringt das Client-Table-Modell keinen Mehrwert; unbenutzte Dep vermieden. Funktionsumfang (sortier-/filter-/durchsuchbar, Spalten ein-/ausblendbar, Pagination, Export, Bulk) voll umgesetzt. |
| mypy strict | Per-Modul-Override für `app.repositories.*` + `settings_service` | SQLModel annotiert Modell-Attribute mit Python-Typen (nicht als SQLAlchemy-Column-Expressions) → bekannte `--strict`-Falsch-Positive nur in der Query-Schicht. Rest der App bleibt strict-clean. |
| tzdata | Als PyPI-Dependency | Chainguard hat keine System-Zeitzonen-DB. |
