# PwNotify — Entwickler-Kommandos
IMAGE ?= amslertec/pwnotify:0.1.0
COMPOSE ?= docker compose
VERSION ?= 0.1.0
REVISION ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
CREATED ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

.DEFAULT_GOAL := help
.PHONY: help up down logs migrate revision shell psql test test-fe lint lint-fe \
        typecheck build buildx scan sbom fmt

help: ## Diese Hilfe anzeigen
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## Stack starten (build + detached)
	$(COMPOSE) up -d --build

down: ## Stack stoppen
	$(COMPOSE) down

logs: ## Logs folgen
	$(COMPOSE) logs -f --tail=200

migrate: ## Alembic-Migrationen anwenden (im laufenden app-Container)
	$(COMPOSE) exec app python -m app.entrypoint migrate

revision: ## Neue Alembic-Revision (autogenerate). Aufruf: make revision m="text"
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

shell: ## Python-REPL im app-Container (Runtime hat keine Shell)
	$(COMPOSE) exec app python

psql: ## psql in den DB-Container
	$(COMPOSE) exec db psql -U pwnotify -d pwnotify

test: ## Backend-Tests (pytest)
	cd backend && uv run pytest -q

test-fe: ## Frontend-Tests (vitest)
	cd frontend && pnpm run test

lint: ## Backend lint+format-check (ruff) + mypy
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app

fmt: ## Backend auto-format (ruff)
	cd backend && uv run ruff format . && uv run ruff check --fix .

lint-fe: ## Frontend lint (eslint) + typecheck (tsc)
	cd frontend && pnpm run lint && pnpm run typecheck

build: ## Image lokal bauen (single-arch)
	docker build -t $(IMAGE) \
	  --build-arg VERSION=$(VERSION) --build-arg REVISION=$(REVISION) --build-arg CREATED=$(CREATED) .

buildx: ## Multi-Arch Build + Push (amd64+arm64, SBOM + Provenance)
	docker buildx build --platform linux/amd64,linux/arm64 \
	  --sbom=true --provenance=true \
	  --build-arg VERSION=$(VERSION) --build-arg REVISION=$(REVISION) --build-arg CREATED=$(CREATED) \
	  -t $(IMAGE) --push .

scan: ## Trivy-Scan des Images (fail bei HIGH/CRITICAL)
	trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed $(IMAGE)

sbom: ## SBOM (CycloneDX) erzeugen
	docker buildx build --sbom=true -o type=local,dest=./sbom .
