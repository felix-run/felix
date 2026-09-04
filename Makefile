.PHONY: help schema install install-full install-warehouse lint fmt type test check check-ci conformance dev dev-key up up-lite up-gcp up-full up-pooled up-replicas up-observability up-temporal metrics-token down cli seed migrate doctor docker-build

COMPOSE := docker compose -f deploy/docker/compose.yml --project-directory .
COMPOSE_LITE := $(COMPOSE) -f deploy/docker/compose.lite.yml
COMPOSE_GCP := $(COMPOSE) -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml
COMPOSE_PGB := $(COMPOSE) -f deploy/docker/compose.pgbouncer.yml
COMPOSE_REPLICAS := $(COMPOSE) -f deploy/docker/compose.replicas.yml
COMPOSE_OBS := $(COMPOSE) -f deploy/docker/compose.observability.yml
COMPOSE_TEMPORAL := $(COMPOSE) -f deploy/docker/compose.temporal.yml

help:
	@echo "Felix dev targets:"
	@echo "  install           uv sync (lean core + dev) — small VMs / CI"
	@echo "  install-full      uv sync --all-extras --dev"
	@echo "  install-warehouse uv sync --extra warehouse --dev (DuckDB analytics)"
	@echo "  lint/fmt/type/test/check"
	@echo "  check-ci          check + every other gate CI runs (no infrastructure)"
	@echo "  conformance       store contract vs a real Postgres (needs FELIX_CONFORMANCE_DATABASE_URL)"
	@echo "  test              ./scripts/test.sh (in-memory stores; args: ./scripts/test.sh -k expr)"
	@echo "  dev               run API locally (auth=none)"
	@echo "  up                deploy/docker compose (lean: fs object store, mem caps)"
	@echo "  up-lite           + lite overlay (~2–4 GiB hosts)"
	@echo "  up-gcp            + gcp+lite overlays (no DB/cache publish)"
	@echo "  up-full           compose --profile full (MinIO; set FELIX_DOCKER_EXTRAS=aws)"
	@echo "  up-pooled         + PgBouncer in transaction mode (many workers, few connections)"
	@echo "  up-replicas       + two API replicas behind one origin (cross-replica proof)"
	@echo "  up-observability  + OTel Collector, Prometheus, Grafana, Jaeger, Loki, exporters"
	@echo "  up-temporal       + Temporal server, UI :8233, and felix-temporal-worker"
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

# Everything CI gates on that `check` does not: the structural and packaging jobs.
# `make check` passing while CI failed meant these had to be remembered by hand.
#
# Two CI jobs are deliberately absent. `conformance` needs a database — it has its own
# target below. `lean` is meaningful only in a lean venv: scripts/lean-import-check.py
# proves nothing when the extras are installed, and a gate that passes vacuously is worse
# than no gate. tests/unit/test_invariants.py checks the same rule statically, in any venv.
check-ci: check
	uv run felix bundle-manifests
	uv run python scripts/gen-manifest-schema.py --check
	uv run python scripts/check-scalar-sri.py
	python3 scripts/validate-toolkit.py
	FELIX_ALLOW_INSECURE=true FELIX_AUTH_MODE=none \
		FELIX_DATABASE_URL=memory://ci FELIX_OBJECT_STORE=memory \
		uv run felix eval --dataset smoke --manifest quick \
			--fixture fixtures/eval/smoke.json --mock
	uv run pre-commit run --all-files

# Needs a reachable Postgres; CI runs this as its own job against a service container.
conformance:
	@test -n "$$FELIX_CONFORMANCE_DATABASE_URL" || { \
		echo "Set FELIX_CONFORMANCE_DATABASE_URL to a Postgres URL, e.g."; \
		echo "  FELIX_CONFORMANCE_DATABASE_URL=postgresql+psycopg://u:p@localhost:5432/db make conformance"; \
		exit 1; }
	FELIX_CONFORMANCE_REQUIRE_POSTGRES=1 ./scripts/test.sh tests/conformance -q

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

up-pooled: dev-key
	$(COMPOSE_PGB) up --build

up-replicas: dev-key
	$(COMPOSE_REPLICAS) up --build

# Needs the scrape credential as well as the operator key: /metrics is auth-gated, so
# Prometheus cannot reach it without one. See scripts/metrics-token.sh.
up-observability: dev-key metrics-token
	$(COMPOSE_OBS) up --build

metrics-token:
	@./scripts/metrics-token.sh

# FELIX_DURABILITY=temporal is set inside the overlay for every process that has to
# agree about it — api, worker and the temporal-worker. The extra is appended to the
# image build there too, since durability/temporal.py raises without temporalio.
up-temporal: dev-key
	$(COMPOSE_TEMPORAL) up --build

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
