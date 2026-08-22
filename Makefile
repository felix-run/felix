.PHONY: help install install-full install-warehouse lint fmt type test check dev up up-lite up-gcp up-full down cli seed migrate doctor docker-build

COMPOSE := docker compose -f deploy/docker/compose.yml --project-directory .
COMPOSE_LITE := $(COMPOSE) -f deploy/docker/compose.lite.yml
COMPOSE_GCP := $(COMPOSE) -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml

help:
	@echo "Felix dev targets:"
	@echo "  install           uv sync (lean core + dev) — small VMs / CI"
	@echo "  install-full      uv sync --all-extras --dev"
	@echo "  install-warehouse uv sync --extra warehouse --dev (DuckDB analytics)"
	@echo "  lint/fmt/type/test/check"
	@echo "  dev               run API locally (auth=none)"
	@echo "  up                deploy/docker compose (lean: fs object store, mem caps)"
	@echo "  up-lite           + lite overlay (~2–4 GiB hosts)"
	@echo "  up-gcp            + gcp+lite overlays (no DB/cache publish)"
	@echo "  up-full           compose --profile full (MinIO; set FELIX_DOCKER_EXTRAS=aws)"
	@echo "  down / cli / seed / migrate / doctor"
	@echo "  Warehouse: FELIX_WAREHOUSE=duckdb + FELIX_DOCKER_EXTRAS=warehouse"

install:
	uv sync --dev

install-full:
	uv sync --all-extras --dev

install-warehouse:
	uv sync --extra warehouse --dev

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

type:
	uv run ty check

test:
	uv run pytest -q

check: lint type test
	uv run ruff format --check .

dev:
	@echo "Felix -> http://localhost:$${FELIX_PORT:-8080}"
	@echo "Set ANTHROPIC_API_KEY / OPENAI_API_KEY, or point FELIX_OLLAMA_BASE_URL at Ollama."
	FELIX_ALLOW_INSECURE=true FELIX_AUTH_MODE=none FELIX_OBJECT_STORE=$${FELIX_OBJECT_STORE:-fs} \
		uv run felix-api

up:
	$(COMPOSE) up --build

up-lite:
	$(COMPOSE_LITE) up --build

up-gcp:
	FELIX_DOCKER_EXTRAS=$${FELIX_DOCKER_EXTRAS:-gcp} $(COMPOSE_GCP) up --build -d

up-full:
	FELIX_DOCKER_EXTRAS=$${FELIX_DOCKER_EXTRAS:-aws} FELIX_OBJECT_STORE=s3 \
		$(COMPOSE) --profile full up --build

down:
	$(COMPOSE) --profile full down

docker-build:
	docker build -f deploy/docker/Dockerfile --build-arg FELIX_EXTRAS="$${FELIX_DOCKER_EXTRAS:-}" -t felix:latest .

migrate:
	uv run felix migrate head

doctor:
	uv run felix doctor

cli:
	uv run python clients/cli.py

seed:
	uv run python scripts/seed.py
