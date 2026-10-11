# Tests

Run them with `./scripts/test.sh`, never a bare `pytest`. The repo `.env` points at a real
Postgres and carries real model keys; the script swaps in the `memory://` stores and blanks the
keys. `make test` adds `-n auto`.

```bash
./scripts/test.sh -n auto                       # everything, one worker per core
./scripts/test.sh tests/unit/test_x.py -k name  # one file, one test
make e2e                                        # tests/e2e only
```

## Where a test goes

| Directory | What it holds |
|---|---|
| `unit/` | One module, or one route through `create_app` with the in-memory store twins. Most tests belong here. |
| `e2e/` | The production boot, `create_application()` with no arguments, over HTTP with a scripted model. Use it when the whole request path is the subject. Use the `boot` fixture; never stub `build_tenant_agent`. |
| `conformance/` | One contract run against every implementation of a seam: memory vs Postgres stores, model and decision providers. |
| `integration/` | That the app mounts a surface at all (health, metrics, MCP, `/artifacts`). These predate `e2e/`; new tests go in `unit/` or `e2e/`. |
| `test_smoke.py` | Older smoke checks over the bundled manifests and core seams. |

Extend the file for the surface you are testing before creating a new one.

## Shared code

- **`support/`** holds helpers, fakes and factories. A test imports shared code from
  `tests.support` and never from another test module or a conftest.
  `unit/test_invariants.py` fails any import that does.
  Each module's docstring says what it is for. The ones you will reach for first:
  - `factories.py`:
    - `make_settings(**overrides)` builds the `memory://` baseline. Name only the fields your
      test cares about.
    - `app_client(settings)` builds an ASGI client into `create_app`.
  - `optional_deps.py:require_optional`: the only way to gate on an optional extra. A bare
    `importorskip` vanishes from the run, and an invariant forbids it.
  - `git_fixture.py:git`: the only way a test runs git.
- **`fixtures/`** holds data only the tests read, such as the skill bundles in
  `fixtures/skills/`. The eval datasets live in the repo-root `fixtures/eval/`, because
  `felix eval` and CI read them too.
- **`conftest.py`** (root): `_isolate_process_global_stores` clears the process-global
  `memory://` stores, caches and agent hooks it lists, around each test. That reset is what
  isolates tests from each other. The name after `memory://` does not: every store checks only
  the prefix. A new in-memory store needs its reset added to that list.

## Writing one

- `asyncio_mode = "auto"`: write `async def test_…` with no marker.
- A misspelt marker or ini key is an error, and so is an exception raised where nothing can
  catch it.
- Use the in-memory twin or a fake at the transport rather than a mock.
- Prove a new test can fail: break the code it guards, watch it go red, then restore the code.
