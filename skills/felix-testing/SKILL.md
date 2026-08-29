---
name: felix-testing
description: How to run and write tests for the Felix harness — the in-memory memory:// path, ./scripts/test.sh instead of bare pytest, the structural invariant gates, and how optional-extra tests must be gated. Use before running any test command in this repo, when a test fails with a Postgres connection error, or when adding coverage for a change.
---

> This is a summary. The source of truth is CLAUDE.md (Running tests) and `.claude/skills/felix-dev-loop/SKILL.md` — read it with `read_file` when
> the two disagree, and trust the checkout over this file.
> The commands below are what a human or CI runs on your change. You cannot run
> them: you have no shell. Name the ones that still need running instead of
> reporting them as done.

# Felix testing

## Run tests with ./scripts/test.sh, never a bare pytest

The repo `.env` points `FELIX_DATABASE_URL` at a real Postgres, and pydantic-settings reads it, so
a bare `uv run pytest` fails on every database-touching test. `./scripts/test.sh` sets the
in-memory environment and is what `make test` and CI both run.

    ./scripts/test.sh                                   # full suite, about 50 seconds
    ./scripts/test.sh tests/unit/test_react_loop.py -q  # one file
    ./scripts/test.sh -k compact                        # one theme
    make check                                          # lint + type + test + format check

`memory://` in the database URL flips every store to its in-memory implementation. That is the
supported no-infrastructure test path, not a mock layer. Every store has an in-memory twin, and
keeping that true is an invariant.

## Prefer the real in-memory implementation over a mock

If a `memory://` twin exists for the thing under test, use it. A mock asserts that you called a
function; the twin asserts that the behavior is right. Reserve mocks for genuinely external
systems.

## Structural gates — fast, no infrastructure

    ./scripts/test.sh tests/unit/test_invariants.py
    uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
    python3 scripts/validate-toolkit.py
    uv run python scripts/gen-manifest-schema.py --check

`tests/unit/test_invariants.py` turns the repo's rules into failures: `.env.example` covers every
`Settings` field, no optional dependency is imported at module scope, every Postgres-touching
module has a `memory://` path, the governance wrapper order is unchanged,
`schemas/manifest.schema.json` still matches the pydantic models, and the CI test job installs
every extra the tests gate on. Change a rule deliberately and you update the test with it.

## Optional extras: require_optional, never a bare importorskip

A test that needs an optional extra gates on `tests/optional_deps.py:require_optional(module,
extra)`. An invariant enforces this. A module-level `pytest.importorskip` collapses a whole file
into a single collect-time skip, so it vanishes from the run without changing the skip count —
that is how six Temporal tests went unexecuted in CI. CI installs the extras and sets
`FELIX_REQUIRE_OPTIONAL_EXTRAS=1`, which turns a missing extra into a failure. Locally, without
that variable, they skip as before.

## Store conformance

`tests/conformance/` runs one contract against every backend, because the invariant above only
asserts that an in-memory twin exists — not that it behaves like the Postgres store it stands in
for. The in-memory arm runs everywhere; the Postgres arm needs a database:

    FELIX_CONFORMANCE_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/db \
      ./scripts/test.sh tests/conformance -q

Without that variable the Postgres arm skips, which is right locally and wrong in CI — so the
conformance job sets `FELIX_CONFORMANCE_REQUIRE_POSTGRES=1`. A silently skipped arm looks exactly
like a pass. Adding a backend to `BACKENDS` makes it inherit every assertion in the contract.

## Eval smoke, no model calls

    uv run felix eval --dataset smoke --manifest quick --fixture fixtures/eval/smoke.json --mock

## Writing a test that is worth having

Ask whether it would fail without the change. A test that passes against the pre-change code
proves nothing. When verifying that, FAILED is the evidence you want; ERROR means the test itself
is broken, not that you found the bug.
