.PHONY: help schema install install-full install-warehouse lint fmt type test check dev dev-key up up-lite up-gcp up-full down cli seed migrate doctor docker-build

COMPOSE := docker compose -f deploy/docker/compose.yml --project-directory .
COMPOSE_LITE := $(COMPOSE) -f deploy/docker/compose.lite.yml
COMPOSE_GCP := $(COMPOSE) -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml

help:
	@echo "Felix dev targets:"
	@echo "  install           uv sync (lean core + dev) — small VMs / CI"
	@echo "  install-full      uv sync --all-extras --dev"
	@echo "  install-warehouse uv sync --extra warehouse --dev (DuckDB analytics)"
	@echo "  lint/fmt/type/test/check"
	@echo "  test              ./scripts/test.sh (in-memory stores; args: ./scripts/test.sh -k expr)"
	@echo "  dev               run API locally (auth=none)"
	@echo "  up                deploy/docker compose (lean: fs object store, mem caps)"
	@echo "  up-lite           + lite overlay (~2–4 GiB hosts)"
	@echo "  up-gcp            + gcp+lite overlays (no DB/cache publish)"
	@echo "  up-full           compose --profile full (MinIO; set FELIX_DOCKER_EXTRAS=aws)"
	@echo "  schema            regenerate schemas/manifest.schema.json"
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
	# Same scope as CI — tests are excluded on purpose (fakes and fixtures
	# trip ty without adding production signal). Needs the optional extras:
	# unresolved imports are errors by design, and a lean venv cannot resolve
	# temporalio, boto3, duckdb, playwright, presidio, … CI installs
	# --all-extras for exactly this reason.
	@uv run --no-sync python -c "import temporalio" >/dev/null 2>&1 || { \
		echo ""; \
		echo "ty needs the optional extras installed — run 'make install-full'."; \
		echo "A lean venv ('make install') reports every optional import as an"; \
		echo "unresolved-import error; CI type-checks with --all-extras."; \
		echo ""; \
		exit 1; }
	uv run ty check packages apps

test:
	./scripts/test.sh

schema:
	# schemas/manifest.schema.json backs the yaml-language-server header in
	# manifests/*.yaml; test_invariants.py fails when it drifts from the models.
	uv run python scripts/gen-manifest-schema.py

check: lint type test
	uv run ruff format --check .

dev:
	@echo "Felix -> http://localhost:$${FELIX_PORT:-8080}"
	@echo "Set ANTHROPIC_API_KEY / OPENAI_API_KEY, or point FELIX_OLLAMA_BASE_URL at Ollama."
	FELIX_ALLOW_INSECURE=true FELIX_AUTH_MODE=none FELIX_HOST=127.0.0.1 \
		FELIX_OBJECT_STORE=$${FELIX_OBJECT_STORE:-fs} \
		uv run felix-api

dev-key:
	@./scripts/dev-key.sh

up: dev-key
	$(COMPOSE) up --build

up-lite: dev-key
	$(COMPOSE_LITE) up --build

up-gcp: dev-key
	FELIX_DOCKER_EXTRAS=$${FELIX_DOCKER_EXTRAS:-gcp} $(COMPOSE_GCP) up --build -d

up-full: dev-key
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
