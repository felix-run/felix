---
name: felix-dev-loop
description: The verified change loop for the Felix Python harness — how to install, run the API locally, run tests under the in-memory memory:// path, and pass the exact gates CI enforces (ruff, ty, pytest, manifest bundling, mock eval). Use before running any test or lint command in this repo, when a command fails with a Postgres connection error, or when asked to verify, check, or validate a change.
compatibility: Requires Python 3.14, uv, and (for the full stack) Docker. Designed for Claude Code.
allowed-tools: Bash(uv:*) Bash(make:*) Bash(./scripts/test.sh:*) Read Grep Glob
---

# Felix dev loop

## Run tests (read this before your first pytest)

```bash
./scripts/test.sh                                   # whole suite, ~50s
./scripts/test.sh tests/unit/test_react_loop.py -q  # one file
./scripts/test.sh -k "compact or fork" -x           # one theme
make test                                           # same script
```

`scripts/test.sh` is the canonical entry point — `make test`, CI, and
`./scripts/test.sh` all delegate to it. It exports:

```
FELIX_ALLOW_INSECURE=true FELIX_AUTH_MODE=none
FELIX_DATABASE_URL=memory://ci FELIX_OBJECT_STORE=memory
```

A bare `uv run pytest` reads the repo `.env`, points at a real Postgres, and fails with
`sqlalchemy.exc.OperationalError: connection refused` on the DB-touching tests. That is an
environment failure, not a code failure — a `PreToolUse` hook blocks it and prints the fix.

`memory://` is not a mock layer: it is the supported in-memory implementation of every store
(`db/session.py:_use_memory`, `session/store.py:get_session_store`). New tests must pass under it.

## The gates, in the order worth running them

| Gate | Command | Notes |
|---|---|---|
| Lint | `uv run ruff check .` | line-length 110, `py314` target |
| Format | `uv run ruff format .` / `--check .` | CI runs the check separately |
| Types | `uv run ty check packages apps` | `make type`; needs `make install-full` (unresolved imports are errors, and a lean venv cannot resolve the extras); CI excludes `tests/` deliberately |
| Tests | `./scripts/test.sh` | 195 tests, 1 skipped |
| Manifests | `uv run felix bundle-manifests` | loads + validates every file in `manifests/` |
| Invariants | `./scripts/test.sh tests/unit/test_invariants.py` | env coverage, lean imports, memory twins, wrapper order |
| Entrypoints | `./scripts/test.sh tests/unit/test_entrypoint_wiring.py` | console scripts, ASGI factory and broker strings, boot with no arguments |
| Test is real | `./scripts/prove-fails.sh <target>` | runs a test against pre-change source: PROVEN / VACUOUS / BROKEN |
| Lean imports | `uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py` | proves the default image can import every module |
| Toolkit | `python3 scripts/validate-toolkit.py` | `.claude/` hooks, settings, subagents, skills |
| Eval | `uv run felix eval --dataset smoke --manifest quick --fixture fixtures/eval/smoke.json --mock` | no model calls |

`make check` runs all four gates (lint, type, test, format check) and matches CI exactly.

Don't "fix" a ruff rule the repo disables: `[tool.ruff.lint] ignore` in `pyproject.toml` documents
why each one is off (`E731`, `SIM102`, `ASYNC109/240`, `RUF001/002`, …).

## Install and run

```bash
make install          # uv sync --dev — lean core, what CI uses
make install-full     # uv sync --all-extras --dev — aws, gcp, mcp, browser, embeddings, …
make dev              # Granian on :8080, FELIX_AUTH_MODE=none, fs object store
make cli              # httpx REPL against a running API
make doctor           # config + connectivity preflight
```

Full stack (Postgres+pgvector, Valkey, worker):

```bash
cp .env.example .env         # set POSTGRES_PASSWORD: openssl rand -hex 32
make up                      # or make up-lite on a 2–4 GiB host
make migrate                 # uv run felix migrate head
curl -s localhost:8080/health | jq
```

Smoke a manifest:

```bash
curl -s -X POST localhost:8080/chat -H 'content-type: application/json' \
  -d '{"manifest":"quick","messages":[{"role":"user","content":"What is 7 * 6?"}]}' | jq
```

## Reporting

Paste real command output. If a gate fails, say so with the failure text — never report a pass you
did not observe, and never leave a gate unrun without saying which one and why.
