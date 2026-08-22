.PHONY: help install install-full install-warehouse lint fmt type test check dev up up-lite up-full down cli seed migrate docker-build

help:
	@echo "Felix dev targets:"
	@echo "  install           uv sync (lean core + dev) — small VMs / CI"
	@echo "  install-full      uv sync --all-extras --dev"
	@echo "  install-warehouse uv sync --extra warehouse --dev (DuckDB analytics)"
	@echo "  lint/fmt/type/test/check"
	@echo "  dev               run API locally (auth=none)"
	@echo "  up                docker compose (lean: fs object store, mem caps)"
	@echo "  up-lite           compose + lite overlay (~2–4 GiB hosts)"
	@echo "  up-full           compose --profile full (MinIO; set FELIX_DOCKER_EXTRAS=aws)"
	@echo "  down / cli / seed / migrate"
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
	docker compose up --build

up-lite:
	docker compose -f docker-compose.yml -f docker-compose.lite.yml up --build

up-full:
	FELIX_DOCKER_EXTRAS=$${FELIX_DOCKER_EXTRAS:-aws} FELIX_OBJECT_STORE=s3 \
		docker compose --profile full up --build

down:
	docker compose --profile full down

docker-build:
	docker build --build-arg FELIX_EXTRAS="$${FELIX_DOCKER_EXTRAS:-}" -t felix:latest .

cli:
	uv run python clients/cli.py

seed:
	uv run python scripts/seed.py

migrate:
	uv run felix migrate head
