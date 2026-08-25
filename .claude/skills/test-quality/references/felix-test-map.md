# Felix test map

The repo-specific half of the **test-quality** skill. Authoritative sources: `scripts/test.sh`,
`pyproject.toml`, `.github/workflows/ci.yml`.

## The runner and its environment

`./scripts/test.sh [args]` is the only supported entry point; `make test` and CI both go through it.
It forwards every argument to the test runner (defaulting to `-q`) after exporting:

```
FELIX_ALLOW_INSECURE=true
FELIX_AUTH_MODE=none
FELIX_HOST=127.0.0.1        # auth_mode=none is only permitted on a loopback bind
FELIX_DATABASE_URL=memory://ci
FELIX_OBJECT_STORE=memory
```

A bare `uv run pytest` reads the repo `.env`, points `FELIX_DATABASE_URL` at a real Postgres, and
fails DB-touching tests with what looks like a code bug. The `PreToolUse` hook
`.claude/hooks/pytest-env-guard.sh` blocks that invocation and says so.

## The in-memory path is a real implementation, not a mock layer

`memory://` in the database URL flips every store to its in-memory twin
(`felix/db/session.py:_use_memory`, `felix/session/store.py:get_session_store`), and
`test_postgres_modules_have_an_in_memory_path` in `tests/unit/test_invariants.py` requires every
Postgres-touching module to have one. So in this repo, patching a store is almost always the wrong
move — there is already a second implementation of the same contract to run against.

Same principle for models: use the eval fixture path (`--mock`) rather than asserting on a
`MagicMock` call list.

## Conventions that change how tests are written

| Setting | Value | Consequence |
|---|---|---|
| `asyncio_mode` | `auto` | `async def test_…` needs no decorator |
| `testpaths` | `tests` | 47 files in `tests/unit/`, 3 in `tests/integration/`, plus `tests/test_smoke.py` |
| `timeout` / `timeout_method` | `120` / `thread` | A per-test backstop, not a budget. The whole suite runs in roughly 50s, so the margin is on the slowest single test, not the total |
| `addopts` | none | Coverage is deliberately not on by default so a single-test run stays fast |
| `per-file-ignores` | `tests/** = E501, RUF012, RUF034` | Long literals and mutable class attrs are fine in tests |

## Coverage: one number, ratcheted

CI's `test` job runs the lean install and then:

```bash
./scripts/test.sh -q --cov --cov-report=term:skip-covered --cov-fail-under=60
```

The comment on that line is the policy: *the coverage floor is the measured number, ratcheted up
deliberately — never an aspirational one, which only teaches people to bypass it.* Raising it means
editing that single flag in `.github/workflows/ci.yml` after the measured number rises.
`[tool.coverage.run]` covers the four source roots with `branch = false`, and
`[tool.coverage.report]` excludes `if TYPE_CHECKING:`, `raise NotImplementedError`, and `@overload`.

Read shape, not the number:

```bash
./scripts/test.sh --cov --cov-report=term-missing:skip-covered
```

## Promote a repeated finding into a structural test

When the same quality defect keeps recurring, stop reporting it and encode it.
`tests/unit/test_invariants.py` is the pattern: AST or file inspection, no runtime cost, and it
cannot be satisfied by mocking. It already pins the optional-import rule, the `memory://` twin rule,
the governance wrapper order, `.env.example` coverage of every setting, and the generated manifest
schema. `tests/unit/test_plugin_boundary.py` does the same for the plugin seam. Adding a rule there
is cheaper than catching it in review forever.

## Other CI-side gates worth knowing

```bash
uv run felix bundle-manifests                       # runs before the suite in CI
uv run felix eval --dataset smoke --manifest quick \
  --fixture fixtures/eval/smoke.json --mock         # eval smoke, no model calls
uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
```
