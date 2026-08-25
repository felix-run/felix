# Troubleshooting

Failure modes this repository produces regularly, and the actual fix for each. Most of them look
like a code bug and are an environment mistake — which is why they cost so much time the first time.

Before working through a symptom by hand, run the preflight; it checks configuration and
connectivity in one pass:

```bash
make doctor          # uv run felix doctor
```

## Tests

### `OperationalError` / `connection refused` from a test run

You ran a bare `pytest`. The repo `.env` points `FELIX_DATABASE_URL` at a real Postgres, and
pydantic-settings reads it, so every database-touching test fails against a server that is not
running. This is an environment failure, not a test failure.

```bash
./scripts/test.sh                                   # whole suite (~50s)
./scripts/test.sh tests/unit/test_react_loop.py -q  # one file
./scripts/test.sh -k compact                        # one theme
```

`scripts/test.sh` exports `FELIX_DATABASE_URL=memory://ci` and `FELIX_OBJECT_STORE=memory`, which
flip every store to its in-memory twin — the supported no-infrastructure path, not a mock layer.
`make test` and CI both go through the same script. A `PreToolUse` hook blocks a bare `pytest` for
agents working in this repo.

### A new test passes locally and fails in CI

CI has no services. If the test only works against real Postgres, Valkey, or an object store, it
needs the `memory://` path. Every Postgres-touching module is required to have an in-memory twin —
`tests/unit/test_invariants.py::test_postgres_modules_have_an_in_memory_path` enforces it.

### `test_env_example_documents_every_setting` fails

You added a field to `felix/config.py:Settings` without documenting it. Add a matching
`FELIX_<NAME>` line to `.env.example` (commented out is fine) with a comment explaining it, and a
README row if it changes the lean-versus-full story.

### `test_manifest_json_schema_is_current` fails

`schemas/manifest.schema.json` is generated, not hand-edited. Regenerate it:

```bash
make schema          # uv run python scripts/gen-manifest-schema.py
```

### `test_governance_wrapper_order_is_unchanged` fails

You reordered the `apply_*` wrappers in `manifests/builder.py`. Each wrapper clones the tool with a
new executor, so the order defines which control runs first — reordering changes behavior. Restore
the order rather than updating the test, unless the change is deliberate and explained.

## Types and lint

### `ty check` reports unresolved imports for temporalio, boto3, duckdb, playwright…

Your virtualenv is lean. Unresolved imports are errors by design, and `ty` cannot resolve an optional
extra that is not installed. CI installs `--all-extras` for exactly this reason.

```bash
make install-full    # uv sync --all-extras --dev
```

`make type` checks for this and says so before running. Note that `ty check` runs over `packages
apps` only — tests are excluded on purpose, because fakes and fixtures trip it without adding
production signal.

### `ty` reports a type error CI does not fail on

Six rules are downgraded to warnings in `[tool.ty.rules]` so CI gates new import breakage without
blocking on pre-existing debt. A warning on new code may still be a real defect — read it.

### ruff flags something the repo does on purpose

Check `[tool.ruff.lint] ignore` in `pyproject.toml` first; each disabled rule has a comment
explaining why. Do not add a per-line `noqa` to route around one of those.

### `ruff format --check` fails on a Markdown file

`ruff format` formats Python code blocks inside Markdown, and CI runs `ruff format --check .` over
the whole repo. A `python` fence in any `.md` — including the files under `.claude/` — is in scope.

```bash
uv run ruff format <file>.md
```

## Running the stack

### `FELIX_AUTH_MODE=none requires …` on startup

`Settings.validate_runtime()` rejected the configuration. `auth_mode=none` is only permitted on a
loopback bind. For local work set `FELIX_ALLOW_INSECURE=true` (what `make dev` does), or set
`FELIX_ENVIRONMENT=development`.

### `Unknown bundled manifest`

`load_bundled()` resolves `manifests/` from the repo root, then the cwd, then the packaged
`bundled/` directory. Run from the repo root, and check the manifest name matches the file stem in
`manifests/`.

### `Unknown pattern`

`build_agent` could not resolve `spec.pattern`. Patterns register at import time via
`register_pattern()`, so the pattern module has to be imported for its name to exist — check
`packages/harness/src/felix/patterns/__init__.py`.

### Scheduled jobs never fire

`felix-scheduler` must run alongside `felix-worker`. Cron schedules are Taskiq labels on the task;
the worker consumes, but only the scheduler enqueues. Confirm the scheduler service exists in
`deploy/docker/compose.yml` or your Helm values before concluding the task is broken.

### `ModuleNotFoundError` at runtime for an optional dependency

Either the extra is not installed (`make install-full`, or `uv sync --extra <name>`), or an optional
dependency is being imported at module scope when it should be imported lazily inside the function
that needs it. `tests/unit/test_invariants.py::test_no_optional_dependency_imported_at_module_scope`
catches the second case.

### `scripts/lean-import-check.py` fails

A module imports an optional dependency at import time, which breaks the lean install and the
default Docker image. Move the import inside the function that needs it, wrapped in `try`/`except`
with a `logger.warning`, or add the dependency to the core requirements deliberately.

```bash
uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
make install-full    # restore the full venv afterwards
```

### Compose behaves oddly, or volumes land in the wrong place

Always run Compose from the repo root. `make up` sets `--project-directory .` for this reason; a
bare `docker compose -f deploy/docker/compose.yml up` resolves relative paths differently.

### The container is killed on a small VM

The default image and Compose stack are lean on purpose. Use the tighter caps, and keep heavy extras
out of the image unless you need them.

```bash
make up-lite         # deploy/docker/compose.lite.yml — ~2–4 GiB hosts
```

## Toolkit

### `validate-toolkit.py` fails on a new hook

Hook scripts must parse under `bash -n` and be executable. A hook referenced from `settings.json`
that is not executable fails the same check.

```bash
chmod +x .claude/hooks/<name>.sh
python3 scripts/validate-toolkit.py
```

### `validate-toolkit.py` rejects skill frontmatter

Skill frontmatter is restricted to the Agent Skills spec fields (`name`, `description`, `license`,
`compatibility`, `metadata`, `allowed-tools`), the `name` must equal the directory name, and every
value must sit on one physical line — the parser is line-based, so a `>-` block scalar reads as an
empty value and fails the length check.
