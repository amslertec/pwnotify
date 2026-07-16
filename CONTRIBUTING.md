# Contributing to PwNotify

Thanks for helping improve PwNotify. This guide covers the local setup, the quality
gates, and how releases are cut.

## Project layout

```
backend/    FastAPI + SQLModel + APScheduler (Python 3.14, managed with uv)
frontend/   React + TypeScript + Vite + Tailwind (managed with pnpm)
Dockerfile  Multi-stage build -> hardened Chainguard runtime image
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/) `0.11.28`
- [pnpm](https://pnpm.io/) `11.12.0` (via `corepack enable`) + Node `24`
- Docker (for the container build) and a PostgreSQL instance for running locally

Every dependency is pinned exactly — see `backend/pyproject.toml` and `frontend/package.json`.

## Local development

**Backend**

```bash
cd backend
uv sync                       # install deps into .venv
uv run alembic upgrade head   # apply migrations (needs PWNOTIFY_DATABASE_URL)
uv run uvicorn app.main:app --reload --port 8080
```

**Frontend**

```bash
cd frontend
pnpm install
pnpm run dev                  # Vite dev server, proxies /api to :8080
```

Copy `example.env` to `.env` and fill in at least `PWNOTIFY_DATABASE_URL`.

## Quality gates (must pass before a PR)

These are the exact checks CI runs — run them locally first:

```bash
# Backend
cd backend
uv run ruff check .
uv run ruff format --check .
uv run mypy app               # strict
uv run pytest -q

# Frontend
cd frontend
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run build
```

## Conventions

- **Versions are pinned exactly** — no `^`/`~`/`latest`. New deps go into the lockfile
- **Backend** stays `mypy --strict` clean; repositories may use the documented per-module
  override for SQLModel column-expression false positives.
- **Frontend** is ESLint + Prettier clean. Avoid raw ASCII double quotes inside JSX
  attribute values (they break parsing — use the German curly quotes or reword).
- Keep the runtime image compliant: non-root, read-only-FS friendly, **0 HIGH/CRITICAL
  CVEs**. No build tools or secrets in the final stage.

## Commit & branch flow

- Branch off `main`, open a PR. CI (lint, types, tests, Trivy scan) must be green.
- Use clear, imperative commit messages.

## Releases

1. Bump the version in `backend/pyproject.toml`, the `Dockerfile` `VERSION` arg, and the
   compose files; add a `CHANGELOG.md` entry.
2. Merge to `main`.
3. Tag it:

   ```bash
   git tag -a v0.1.0 -m "PwNotify 0.1.0"
   git push origin v0.1.0
   ```

   The `v*` tag triggers the CI `docker` job, which builds the multi-arch image
   (amd64 + arm64) with SBOM + provenance and pushes `:<version>` to Docker Hub.

### Required repository secrets

The push step needs these to be set (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub account (e.g. `amslertec`) |
| `DOCKERHUB_TOKEN` | Docker Hub **access token** with Read/Write scope |

Without them the push/scan steps are skipped automatically, so CI still stays green.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
