# Felix test map

The repo-specific half of the **test-quality** skill. Authoritative sources: `scripts/test.sh`,
`pyproject.toml`, `.github/workflows/ci.yml`.

## The runner and its environment

`./scripts/test.sh [args]` is the only supported entry point; `make test` and CI both go through it.
It forwards every argument to the test runner (defaulting to `-q`) after exporting an in-memory,
credential-free environment. **The variable list lives in `scripts/test.sh`, with the reason for
each beside it — read it there rather than a copy here.** What each family is for:

- **In-memory stores** (`FELIX_DATABASE_URL=memory://ci`, `FELIX_OBJECT_STORE=memory`) — the
  supported no-infrastructure path, not a mock layer.
- **Auth off on loopback** (`FELIX_AUTH_MODE=none`, `FELIX_HOST=127.0.0.1`) — `none` is only
  allowed on a loopback bind, and the repo `.env` binds `0.0.0.0`.
- **A cache that answers nothing** (`FELIX_REDIS_URL` at port 9) — with the local stack up, the
  suite otherwise shared one rate-limit counter with Valkey and failed with 429s that moved run to
  run.
- **Every credential blank** (the vendor keys, `FELIX_SEARCH_API_KEY`, and
  `FELIX_MODEL_PROVIDER_OPTIONS`, whose per-provider `api_key` outranks the named fields).
  `test_invariants.py` pins this list against `Settings`, so a new credential field fails the suite.
  The repo `.env` carries real credentials and pydantic-settings reads it, so before they were
  blanked a test that reached a model called the vendor and billed it — which the first run of
  `tests/e2e/` did. A mis-routed model call must fail closed, not succeed quietly against
  production.
- **No embedding download** (`FELIX_MEMORY_EMBEDDER=none`) — tests needing vectors set their own.

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
| `testpaths` | `tests` | `tests/unit/`, `tests/e2e/`, `tests/conformance/`, `tests/integration/`, plus `tests/test_smoke.py`; `ls tests/*/test_*.py \| cut -d/ -f2 \| uniq -c` for the current split |
| `timeout` / `timeout_method` | `120` / `thread` | A per-test backstop, not a budget. The whole suite runs in about a minute, so the margin is on the slowest single test, not the total |
| `addopts` | none | Coverage is deliberately not on by default so a single-test run stays fast |
| `per-file-ignores` | `tests/** = E501, RUF012, RUF034` | Long literals and mutable class attrs are fine in tests |

## Coverage: one number, ratcheted

CI's `test` job runs the lean install and then `make test-cov`, which is also what `make check`
runs:

```bash
./scripts/test.sh -q -n auto --cov --cov-report=term:skip-covered --cov-fail-under=79
```

The floor is that flag on the `test-cov` recipe in the `Makefile`, and the comment beside it is the
policy: *the coverage floor is the measured number, ratcheted up deliberately — never an
aspirational one, which only teaches people to bypass it.* Raising it means editing that one value
after the measured number rises, and `test_the_coverage_floor_is_what_check_and_ci_both_run` fails
if it disappears, ratchets down, or stops being what `make check` and CI run.

It used to live in `.github/workflows/ci.yml`, which meant `make check` measured no coverage at all
and enforced nothing — a local run could not tell you what CI would say. It deliberately does *not*
live in `[tool.coverage.report]` as `fail_under`, because a floor there arms on **every** run that
measures coverage — and `[tool.coverage.run] source` names all five roots however few tests were
selected. Add `--cov` to a one-file run and it reports the whole tree at ~17% and exits 1 with
every test passing. A guard that fires when nothing is wrong is how `--no-cov` becomes muscle
memory. Use the full-suite form below when reading coverage for shape.

`[tool.coverage.run]` covers the five source roots with `branch = false`, and
`[tool.coverage.report]` excludes `if TYPE_CHECKING:`, `raise NotImplementedError`, and `@overload`.

Read shape, not the number:

```bash
./scripts/test.sh --cov --cov-report=term-missing:skip-covered
```

## The layer that runs the whole path

`tests/e2e/` boots the zero-argument `create_application()` — the factory Granian is handed in
production — sends real HTTP through it, and lets the compiled agent call
`felix_ai.providers.scripted` instead of a vendor. Everything else on the path is the real
thing: the middleware stack, manifest resolution, `build_agent`, the governance wrappers, the
pattern, the reply controls.

Use it whenever the thing under test is *wiring* rather than a unit: a control that must still
be applied when a request arrives, an event that must reach the store, a frame a client depends
on. `tests/e2e/conftest.py:boot` takes a script, optional env overrides and optional manifests,
and yields an HTTP client plus a spy holding every model client the registry built. Do not
monkeypatch `build_tenant_agent` in a new test — the harness exists so that stops.

## Promote a repeated finding into a structural test

When the same quality defect keeps recurring, stop reporting it and encode it.
`tests/unit/test_invariants.py` is the pattern: AST or file inspection, no runtime cost, and it
cannot be satisfied by mocking.

**Every scanner there carries a positive control, and a new one must too.** `_python_files()` and
`_py_files()` fail on a missing or empty root rather than returning `[]`, and each scan asserts a
floor on what it actually matched — modules reaching Postgres, `httpx` clients constructed,
functions taking a `tenant_id`, and so on — before asserting the offender list is empty. The floor
is the measured count, not an aspiration: two of them were set by guess when the guards went in and
both had to come down, and one of those measurements found an invariant covering a single call site
where its docstring claimed eight. Count *before* any exemption filter, so the floor measures whether
the match still works rather than how many sites are excused. A scan that stops matching must go red,
not quiet.

`test_invariants.py` already pins the optional-import rule, the `memory://` twin rule,
the governance wrapper order, `.env.example` coverage of every setting, and the generated manifest
schema. `tests/unit/test_plugin_boundary.py` does the same for the plugin seam. Adding a rule there
is cheaper than catching it in review forever.

## Other CI-side gates worth knowing

```bash
uv run felix bundle-manifests                       # runs before the suite in CI
uv run felix eval --dataset smoke --manifest quick \
  --fixture fixtures/eval/smoke.json --mock         # eval smoke, no model calls
./scripts/eval-counter-smoke.sh                     # its counter-smoke: the run above passes
                                                    # by construction, so alone it proves only
                                                    # that the pipeline executes
uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
```
