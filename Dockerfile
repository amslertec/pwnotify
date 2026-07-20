# syntax=docker/dockerfile:1.9
# =============================================================================
# PwNotify — Multi-Stage Build
#   stage 1  frontend  : Node 24 (nur Build) -> statisches React-Bundle
#   stage 2  deps      : Chainguard python:latest-dev + uv -> /app/.venv
#   stage 3  runtime   : Chainguard python:latest (0 CVE, non-root, keine Shell)
# Runtime-Image enthält KEIN Node, KEINE Build-Tools, KEIN uv, KEINEN Compiler.
# =============================================================================

# ---------- Stage 1: Frontend ------------------------------------------------
FROM node:24-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS frontend
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CI=1
WORKDIR /fe
RUN corepack enable && corepack prepare pnpm@11.12.0 --activate
# 1) nur Manifeste -> maximaler Layer-Cache für Dependencies
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store pnpm install --frozen-lockfile
# 2) Quellcode + Build
COPY frontend/ ./
RUN pnpm run build            # Ausgabe -> /fe/dist

# ---------- Stage 2: Python-Dependencies (venv) ------------------------------
FROM cgr.dev/chainguard/python:latest-dev@sha256:31d318170df60ddec4b04ed595cbe79c33eeb2cf94f9676db6f9eaf46542e6be AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python
WORKDIR /app
# Nur Lock + Manifest -> Dependency-Layer wird gecacht, solange sie unverändert sind
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
# leeres Verzeichnis als Vorlage fürs /data-Volume (Ownership setzt COPY --chown im Runtime).
RUN mkdir -p /tmp/data-empty

# ---------- Stage 3: Runtime -------------------------------------------------
FROM cgr.dev/chainguard/python:latest@sha256:2c6a2e8bdeb1336cd8545d3586d1c1e5b4f7564ef00924b0447ebfbe57a549ee AS runtime

# --- OCI-Labels (Werte via build-args aus CI) ---
ARG VERSION=0.3.3
ARG REVISION=dev
ARG CREATED=1970-01-01T00:00:00Z
LABEL org.opencontainers.image.title="PwNotify" \
      org.opencontainers.image.description="Password Expiry Notification Tool für Microsoft Entra ID (Azure AD)." \
      org.opencontainers.image.authors="Pascal Amsler <pascal.amsler@amslertec.ch>" \
      org.opencontainers.image.url="https://github.com/amslertec/pwnotify" \
      org.opencontainers.image.source="https://github.com/amslertec/pwnotify" \
      org.opencontainers.image.documentation="https://github.com/amslertec/pwnotify/blob/main/README.md" \
      org.opencontainers.image.vendor="amslertec" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="cgr.dev/chainguard/python:latest" \
      org.opencontainers.image.base.digest="sha256:2c6a2e8bdeb1336cd8545d3586d1c1e5b4f7564ef00924b0447ebfbe57a549ee"

WORKDIR /app
# TZ=UTC: the runtime image ships no /etc/timezone, so tzlocal cannot detect a zone and
# logs a UserWarning on startup, defaulting to UTC anyway. Setting TZ makes that explicit and
# silences the noise. Scheduling is unaffected -- APScheduler always receives an explicit
# timezone (Europe/Zurich) from the settings, never the process-local one.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TZ=UTC \
    PWNOTIFY_STATIC_DIR=/app/static \
    PWNOTIFY_DATA_DIR=/data \
    PWNOTIFY_PORT=8080

# venv (Chainguard-glibc-kompatibel, gleiche Python 3.14.6), App, Migrationen, Statics
COPY --from=deps /app/.venv /app/.venv
COPY --from=deps --chown=65532:65532 /tmp/data-empty /data
COPY backend/alembic.ini /app/alembic.ini
COPY backend/alembic /app/alembic
COPY backend/app /app/app
COPY --from=frontend /fe/dist /app/static

USER 65532
EXPOSE 8080
VOLUME ["/data"]

# HEALTHCHECK ohne Shell/curl -> reines Python (im Image vorhanden)
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"]

# exec-form -> python ist PID 1, SIGTERM erreicht Uvicorn -> graceful Scheduler-Shutdown
ENTRYPOINT ["python", "-m", "app.entrypoint"]
