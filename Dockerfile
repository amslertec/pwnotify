# syntax=docker/dockerfile:1.9
# =============================================================================
# PwNotify — Multi-Stage Build
#   stage 1  frontend  : Node 24 (nur Build) -> statisches React-Bundle
#   stage 2  deps      : Chainguard python:latest-dev + uv -> /app/.venv
#   stage 3  runtime   : Chainguard python:latest (0 CVE, non-root, keine Shell)
# Runtime-Image enthält KEIN Node, KEINE Build-Tools, KEIN uv, KEINEN Compiler.
# =============================================================================

# ---------- Stage 1: Frontend ------------------------------------------------
FROM node:24-bookworm-slim@sha256:cb4e8f7c443347358b7875e717c29e27bf9befc8f5a26cf18af3c3dec80e58c5 AS frontend
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
FROM cgr.dev/chainguard/python:latest-dev@sha256:b5ce829f93559a3a724837305f267244529bad30b878dc5623940af0a255c6b9 AS deps
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
FROM cgr.dev/chainguard/python:latest@sha256:ce9aaca1f826f7f963cd031e98f8c19f993b1843096d395ea919b646e72cb8de AS runtime

# --- OCI-Labels (Werte via build-args aus CI) ---
ARG VERSION=0.1.15
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
      org.opencontainers.image.base.digest="sha256:4d908c6a44ba22460e34a2f6dd665b8fcb82bd3e6c887e749bd6fef243e10094"

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
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
