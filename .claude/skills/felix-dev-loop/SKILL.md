---
name: felix-dev-loop
description: The verified change loop for the Felix Python harness — how to install, run the API locally, run tests under the in-memory memory:// path, pick the right test tier (unit, e2e, conformance, eval), and pass the gates CI enforces, in the tiers make check, make check-ci and CI-only. Use before running any test or lint command in this repo, when a command fails with a Postgres connection error, or when asked to verify, check, or validate a change.
compatibility: Requires Python 3.14, uv, and (for the full stack) Docker. Designed for Claude Code.
allowed-tools: Bash(uv:*) Bash(make:*) Bash(./scripts/test.sh:*) Read Grep Glob
---

# Felix dev loop

## Run tests (read this before your first pytest)

```bash
./scripts/test.sh                                   # whole suite, about a minute
./scripts/test.sh tests/unit/test_react_loop.py -q  # one file
./scripts/test.sh -k "compact or fork" -x           # one theme
make e2e                                            # tests/e2e only
```

`scripts/test.sh` is the canonical entry point — `make test`, `make check` and CI all go through
it. It sets the in-memory stores (`FELIX_DATABASE_URL=memory://ci`, `FELIX_OBJECT_STORE=memory`),
`FELIX_AUTH_MODE=none` with `FELIX_ALLOW_INSECURE=true` on a loopback `FELIX_HOST`, points
`FELIX_REDIS_URL` at a port nothing listens on (so a local Valkey cannot share rate-limit counters
with the suite), and **blanks every vendor credential** (`FELIX_ANTHROPIC_API_KEY`,
`FELIX_OPENAI_API_KEY`, `FELIX_SEARCH_API_KEY`, `FELIX_MODEL_PROVIDER_OPTIONS`) so a test that
reaches a real model fails instead of billing one. The script's comments give the reason for each.

A bare `uv run pytest` reads the repo `.env`, points at a real Postgres with real keys, and fails
with `OperationalError: connection refused` on the DB-touching tests — an environment failure, not
a code failure. A `PreToolUse` hook blocks it.

`memory://` is not a mock layer: it is the supported in-memory implementation of every store
(`db/session.py:_use_memory`, `session/store.py:get_session_store`). New tests must pass under it.

## Test tiers

| Tier | Run | What it proves |
|---|---|---|
| unit | `./scripts/test.sh tests/unit` | module behaviour, plus repo rules as tests (`test_invariants.py`, `test_entrypoint_wiring.py`, `test_plugin_boundary.py`) |
| e2e | `make e2e` | the zero-argument `create_application()` over real HTTP with `felix_ai.providers.scripted` as the model — nothing between the socket and the model is replaced. Use `tests/e2e/conftest.py:boot`; never monkeypatch `build_tenant_agent`. |
| conformance | `./scripts/test.sh tests/conformance` | one contract over every implementation of a seam. The model and decision provider arms need no infrastructure, so a skip there is a bug. Store arms add Postgres with `FELIX_CONFORMANCE_DATABASE_URL` (`make conformance`); the cross-replica arm adds Valkey with `FELIX_CONFORMANCE_REDIS_URL`. |
| eval | `make eval` | `fixtures/eval/smoke.json` passes by construction and `negative.json` must fail; the pair is the gate, neither half alone. |

A test that needs an optional extra uses `tests/optional_deps.py:require_optional(module, extra)`,
never a module-level `pytest.importorskip` — that one silently removes a file from the run.

## The gates, in three tiers

**`make check`** — the edit loop's gate: `ruff check`, `ty check packages apps`, the suite with the
coverage floor (`make test-cov`), `ruff format --check`. `make type` needs `make install-full`: a
lean venv reports every optional import as unresolved.

**`make check-ci`** — `check` plus everything CI gates on that needs no service:
`make bundle` (every manifest loads), `make schema-check` (`schemas/manifest.schema.json` matches
the models; `make schema` regenerates it), `make toolkit` (`.claude/` hooks, settings, agents,
skills, and every path they cite), `make eval`, the Scalar SRI check, and `pre-commit run --all-files`
(ruff, trailing whitespace, end-of-file, YAML, large files, merge markers). Run it before a PR.

**CI only** — what `check-ci` cannot reproduce locally, so a green `check-ci` does not rule out:

| CI job | Why local differs | Closest local run |
|---|---|---|
| `lint` | also `uv lock --check` and a 48h dependency-age gate | `uv lock --check` |
| `test` | installs only `temporal warehouse sandbox otel`, sets `FELIX_REQUIRE_OPTIONAL_EXTRAS=1` and `FELIX_REQUIRE_HELM=1` (a missing extra or helm fails instead of skipping) | `FELIX_REQUIRE_OPTIONAL_EXTRAS=1 make test-cov` |
| `conformance` | real Postgres + Valkey services | `make conformance` with both URLs set |
| `lean` | a `--no-dev` venv with no extras | `uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py` (then re-sync) |
| `helm`, `docker` | chart lint, compose render over every overlay, image build, Trivy | `helm lint deploy/helm/felix`; `docker compose -f deploy/docker/compose.yml --project-directory . config --format json \| python3 scripts/check-compose-render.py` |
| `security.yml` | CodeQL, pip-audit, gitleaks over **full history** | `gitleaks detect` — a fake key in a test fixture fails this; use tiny placeholders |

Don't "fix" a ruff rule the repo disables: `[tool.ruff.lint] ignore` in `pyproject.toml` documents
why each one is off (`E731`, `SIM102`, `ASYNC109/240`, `RUF001/002`, …).

## Install and run

```bash
make install          # uv sync --dev — the lean core
make install-full     # uv sync --all-extras --dev — needed for `make type`
make dev              # Granian on :8080, FELIX_AUTH_MODE=none, fs object store
make cli              # httpx REPL against a running API
make doctor           # config + connectivity preflight
```

A fresh worktree has a lean venv: run `make install-full` there before `make check`, or the type
gate fails its guard after the whole suite has already run.

Full stack (Postgres+pgvector, Valkey, worker, scheduler):

```bash
cp .env.example .env         # set POSTGRES_PASSWORD: openssl rand -hex 32
make up                      # or make up-lite on a 2–4 GiB host
make migrate                 # uv run felix migrate head
curl -s localhost:8080/health | jq
```

Under Compose, a variable exported in your shell overrides `.env`. Smoke a manifest:

```bash
curl -s -X POST localhost:8080/chat -H 'content-type: application/json' \
  -d '{"manifest":"quick","messages":[{"role":"user","content":"What is 7 * 6?"}]}' | jq
```

## Reporting

Paste real command output. If a gate fails, say so with the failure text — never report a pass you
did not observe, and never leave a gate unrun without saying which one and why. A new test counts
only once you have seen it **fail** against the pre-change code; an `ERROR` there means the test is
broken and proves nothing.
