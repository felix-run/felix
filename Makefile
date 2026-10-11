.PHONY: help bootstrap install install-full install-warehouse test test-cov e2e conformance lint fmt type check check-ci bundle schema-check contract-check toolkit eval lock-check deps-age compose-check helm-lint schema contract dev db migrate seed doctor cli dev-key metrics-token up up-lite up-gcp up-full up-pooled up-replicas up-observability up-self down down-all docker-build

COMPOSE := docker compose -f deploy/docker/compose.yml --project-directory .
COMPOSE_LITE := $(COMPOSE) -f deploy/docker/compose.lite.yml
COMPOSE_GCP := $(COMPOSE) -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml
COMPOSE_PGB := $(COMPOSE) -f deploy/docker/compose.pgbouncer.yml
COMPOSE_REPLICAS := $(COMPOSE) -f deploy/docker/compose.replicas.yml
COMPOSE_OBS := $(COMPOSE) -f deploy/docker/compose.observability.yml
COMPOSE_SELF := $(COMPOSE) -f deploy/docker/compose.self.yml

# Every target documents itself with a `## description` after its prerequisites; `##@ Group` lines
# start a section. Generated, so a new target cannot be missing from it.
help:  ## this list
	@awk 'BEGIN {FS = ":.*## "; print "Felix dev targets:"} \
		/^##@ / { printf "\n%s\n", substr($$0, 5) } \
		/^[a-z][a-z0-9-]*:.*## / { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""
	@echo "Tracing: FELIX_OTEL_ENABLED=true + FELIX_DOCKER_EXTRAS=otel exports to any OTLP backend."
	@echo "Warehouse: FELIX_WAREHOUSE=duckdb + FELIX_DOCKER_EXTRAS=warehouse."

##@ Setup

bootstrap:  ## fresh clone → working checkout: tools, .env with generated passwords, install, pre-commit
	@./scripts/bootstrap.sh

install:  ## uv sync — lean core + dev (small VMs, CI)
	uv sync --dev

install-full:  ## uv sync --all-extras --dev (cloud SDKs, embeddings, browser; needed by `type`)
	uv sync --all-extras --dev

install-warehouse:  ## uv sync --extra warehouse --dev (DuckDB analytics)
	uv sync --extra warehouse --dev

##@ Test

# `-n auto`: one worker per core. The suite spent more than half its wall time waiting on
# subprocesses and timers; single-file runs through ./scripts/test.sh stay serial.
test:  ## the suite on in-memory stores, one worker per core (one test: ./scripts/test.sh -k expr)
	./scripts/test.sh -n auto

# The coverage floor lives on this recipe: one home, and `make check` enforces the number CI
# does. Locally it enforced nothing before, because `check` ran the suite without coverage at
# all and the number lived only in ci.yml. It is here rather than in pyproject's
# [tool.coverage.report] because a floor there also arms every ad-hoc `--cov` run on part of
# the suite, which fails at ~17% with everything passing and teaches people `--no-cov`.
# Measured 2026-09-10: 80.96% with the extras, 80.36% lean — the floor sits under both, since
# the extras carry code a lean run cannot reach. Was 70 when the gate was written and 77 when
# this audit started; ratchet it deliberately, never aspirationally.
# `check` runs this and not `test` so the bare `test` stays fast for the edit loop.
test-cov:  ## the suite against the coverage floor (what check and CI run)
	./scripts/test.sh -q -n auto --cov --cov-report=term:skip-covered --cov-fail-under=79

e2e:  ## tests/e2e only: the production boot over HTTP, scripted model
	./scripts/test.sh tests/e2e -q

# Needs a reachable Postgres; CI runs this as its own job against a service container.
conformance:  ## store contract vs a real Postgres (FELIX_CONFORMANCE_DATABASE_URL[, _REDIS_URL])
	@test -n "$$FELIX_CONFORMANCE_DATABASE_URL" || { \
		echo "Set FELIX_CONFORMANCE_DATABASE_URL to a Postgres URL, e.g."; \
		echo "  FELIX_CONFORMANCE_DATABASE_URL=postgresql+psycopg://u:p@localhost:5432/db make conformance"; \
		exit 1; }
	@test -n "$$FELIX_CONFORMANCE_REDIS_URL" || \
		echo "FELIX_CONFORMANCE_REDIS_URL unset: the cross-replica notification arm will skip (CI runs it)."
	# `env` because an assignment produced by an expansion is a word, not an assignment.
	env FELIX_CONFORMANCE_REQUIRE_POSTGRES=1 $${FELIX_CONFORMANCE_REDIS_URL:+FELIX_CONFORMANCE_REQUIRE_REDIS=1} \
		./scripts/test.sh tests/conformance -q

##@ Gates

lint:  ## ruff check over the repo
	uv run ruff check .

fmt:  ## ruff format the repo in place
	uv run ruff format .

type:  ## ty over packages and apps (needs install-full; skipped with a notice on a lean venv)
	# Same scope as CI — tests are excluded on purpose (fakes and fixtures
	# trip ty without adding production signal). Needs the optional extras:
	# unresolved imports are errors by design, and a lean venv cannot resolve
	# boto3, duckdb, playwright, presidio, … CI installs --all-extras for
	# exactly this reason. duckdb (warehouse) stands in for "the extras are there".
	# On a lean venv this skips, loudly, so `make check` still runs the rest; under
	# CI=true or STRICT=1 (which `check-ci` sets) a skip is a failure instead.
	@if ! uv run --no-sync python -c "import duckdb" >/dev/null 2>&1; then \
		echo ""; \
		echo "SKIPPED type check: ty needs the optional extras — run 'make install-full'."; \
		echo "A lean venv reports every optional import as unresolved; CI type-checks"; \
		echo "with --all-extras."; \
		echo ""; \
		[ "$${CI:-}" != true ] && [ "$${STRICT:-}" != 1 ]; \
	else \
		echo "uv run ty check packages apps"; \
		uv run ty check packages apps; \
	fi

check: lint type test-cov  ## lint + type + test-cov + format check — the everyday gate
	uv run ruff format --check .
	@uv run --no-sync python -c "import duckdb" >/dev/null 2>&1 || \
		echo "make check passed WITHOUT the type check: this venv is lean (make install-full)."

# Everything CI gates on that `check` does not: the structural and packaging jobs.
# `make check` passing while CI failed meant these had to be remembered by hand.
#
# Four CI jobs are deliberately absent. `compose-check` and `helm-lint` need docker and
# helm, and have their own targets. `conformance` needs a database — it has its own
# target below. `lean` is meaningful only in a lean venv: scripts/lean-import-check.py
# proves nothing when the extras are installed, and a gate that passes vacuously is worse
# than no gate. tests/unit/test_invariants.py checks the same rule statically, in any venv.
# STRICT=1: a lean venv fails `type` here instead of skipping it.
check-ci: export STRICT := 1
check-ci: check bundle schema-check contract-check toolkit eval lock-check deps-age  ## check + every other gate CI runs that needs no infrastructure
	uv run python scripts/check-scalar-sri.py
	uv run pre-commit run --all-files

# check-ci's parts, runnable alone: each is the fast answer to "did I break X".
bundle:  ## every bundled manifest validates (felix bundle-manifests)
	uv run felix bundle-manifests

schema-check:  ## schemas/manifest.schema.json is current
	uv run python scripts/gen-manifest-schema.py --check

contract-check:  ## the wire contract (OpenAPI + SSE events) is current
	uv run python scripts/gen-wire-contract.py --check

toolkit:  ## the .claude toolkit and every path it cites
	python3 scripts/validate-toolkit.py

eval:  ## eval smoke plus its counter-smoke, mocked model
	FELIX_ALLOW_INSECURE=true FELIX_AUTH_MODE=none \
		FELIX_DATABASE_URL=memory://ci FELIX_OBJECT_STORE=memory \
		uv run felix eval --dataset smoke --manifest quick \
			--fixture fixtures/eval/smoke.json --mock
	# The counter-smoke: the run above passes by construction, so on its own it proves the
	# pipeline executes and nothing about whether the scorer can reject an answer. Shared with
	# the CI eval job so the two cannot drift; the script's header explains its checks.
	./scripts/eval-counter-smoke.sh

# The same command as CI's lint job (ci.yml); change both together.
lock-check:  ## uv.lock matches pyproject (CI's lint job)
	uv lock --check

# The 48h hold is policy, written here and in ci.yml's lint job (an invariant pins that copy).
deps-age:  ## no locked dependency younger than 48h (CI's lint job; asks PyPI)
	python3 scripts/check-dependency-age.py --hours 48

compose-check:  ## every Compose overlay parses and renders as required (CI's docker job; needs docker)
	./scripts/check-compose.sh

# The same command as CI's helm job (ci.yml); change both together.
helm-lint:  ## helm lint the chart (CI's helm job; needs helm)
	helm lint deploy/helm/felix

##@ Generate

schema:  ## regenerate schemas/manifest.schema.json
	# schemas/manifest.schema.json backs the yaml-language-server header in
	# manifests/*.yaml; test_invariants.py fails when it drifts from the models.
	uv run python scripts/gen-manifest-schema.py

contract:  ## regenerate schemas/openapi.json and schemas/sse-events.json (the wire contract)
	uv run python scripts/gen-wire-contract.py

##@ Run from this checkout

dev:  ## the API on :8080 from this checkout, auth=none (pair with `make db`)
	@echo "Felix -> http://localhost:$${FELIX_PORT:-8080}"
	@echo "Models: FELIX_ANTHROPIC_API_KEY in .env, or FELIX_OPENAI_API_KEY plus FELIX_DEFAULT_MODEL_ID=gpt-4.1."
	FELIX_ALLOW_INSECURE=true FELIX_AUTH_MODE=none FELIX_HOST=127.0.0.1 \
		FELIX_OBJECT_STORE=$${FELIX_OBJECT_STORE:-fs} \
		uv run felix-api

# Postgres and Valkey alone, published on 127.0.0.1 (5432, 6379) where .env's
# FELIX_DATABASE_URL and FELIX_REDIS_URL already point, so the API can run from this
# checkout: `make db migrate dev`. `make down` stops them. dev-key because Compose
# interpolates the whole file, and FELIX_AUTH_API_KEYS is required even for services not started.
db: dev-key  ## only Postgres + Valkey in Docker, on localhost, for `make migrate dev`
	$(COMPOSE) up -d --wait postgres valkey

migrate:  ## felix migrate head against FELIX_DATABASE_URL
	uv run felix migrate head

seed:  ## seed the quick/deep/router manifests, a heartbeat job and a smoke dataset
	uv run python scripts/seed.py

doctor:  ## felix doctor — config and connectivity preflight
	uv run felix doctor

cli:  ## httpx chat REPL against a running API
	uv run python clients/cli.py

##@ Docker Compose

dev-key:  ## write a local admin API key into .env once (make up does this)
	@./scripts/dev-key.sh

metrics-token:  ## write the /metrics scrape credential into .env once
	@./scripts/metrics-token.sh

up: dev-key  ## the stack in Docker: migrate, api :8080, worker, scheduler, Postgres, Valkey
	$(COMPOSE) up --build

up-lite: dev-key  ## up with tighter memory caps for ~2–4 GiB hosts
	$(COMPOSE_LITE) up --build

up-gcp: dev-key  ## up -d with the gcp + lite overlays (published image, no DB/cache publish)
	FELIX_DOCKER_EXTRAS=$${FELIX_DOCKER_EXTRAS:-gcp} $(COMPOSE_GCP) up --build -d

up-full: dev-key  ## up --profile full: MinIO and the aws extra
	FELIX_DOCKER_EXTRAS=$${FELIX_DOCKER_EXTRAS:-aws} FELIX_OBJECT_STORE=s3 \
		$(COMPOSE) --profile full up --build

up-pooled: dev-key  ## up with PgBouncer in transaction mode
	$(COMPOSE_PGB) up --build

up-replicas: dev-key  ## up with two API replicas behind one origin
	$(COMPOSE_REPLICAS) up --build

# Needs the scrape credential as well as the operator key: /metrics is auth-gated, so
# Prometheus cannot reach it without one. See scripts/metrics-token.sh.
up-observability: dev-key metrics-token  ## up with OTel Collector, Prometheus, Grafana, Jaeger, Loki
	$(COMPOSE_OBS) up --build

# Felix builds Felix (docs/SELF.md). The builder image is FROM felix:latest, so the base
# image must exist before the overlay builds on top of it.
up-self: dev-key  ## up with the self-build stack (docs/SELF.md)
	./scripts/shell-runner-token.sh
	$(COMPOSE) build api
	$(COMPOSE_SELF) up --build

# Every overlay shares the project name `felix`, so services started by one are orphans
# to the next. Without --remove-orphans they survive `down` and keep running — which is
# how a stack ends up with four overlays' worth of containers up at once, several of
# them receiving nothing because the last `up` recreated api/worker without their env.
# `down` here means "this project is down", so it removes them.
down:  ## stop every overlay's services (keeps volumes)
	$(COMPOSE) --profile full down --remove-orphans

# Volumes too: Postgres, Valkey, MinIO and the data dir. Destroys local state.
down-all:  ## down and delete volumes — destroys local Postgres/Valkey/MinIO state
	$(COMPOSE) --profile full down --remove-orphans --volumes

docker-build:  ## build the felix:latest image alone (FELIX_DOCKER_EXTRAS selects extras)
	docker build -f deploy/docker/Dockerfile --build-arg FELIX_EXTRAS="$${FELIX_DOCKER_EXTRAS:-}" -t felix:latest .
