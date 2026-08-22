---
name: python-conventions
description: The Python style and architecture conventions that make Felix code look like Felix code — settings, Protocols, lazy optional imports, async patterns, dataclasses, error handling, logging, and the ruff/ty configuration and its deliberate exemptions. Use when writing or reviewing Python in this repo, or when a lint or type check disagrees with an edit.
allowed-tools: Read Grep Glob Bash(uv run ruff:*) Bash(uv run ty:*)
---

# Felix Python conventions

Python 3.14, uv workspace (`packages/*`, `apps/*`), ruff line-length 110, `ty` for types.

## Structure

- **Settings**: one place — `felix/config.py:Settings` (pydantic-settings, `FELIX_` prefix,
  `.env` file, `extra="ignore"`). Never read `os.environ` directly for configuration. Unsafe
  combinations belong in `validate_runtime()`. New setting ⇒ `.env.example` + README.
- **Protocols over inheritance**: infrastructure is a `Protocol` with swappable implementations
  (`ModelProvider`, object stores, secrets backends, warehouse). Follow the shape; don't introduce
  an ABC hierarchy.
- **Dataclasses** for runtime types (`@dataclass(slots=True)` in `patterns/types.py`); pydantic for
  anything parsed from YAML/JSON (manifests, settings).
- **Registries**: open registration at import time (`register_pattern`, plugin registry). Core
  never enumerates the things that register.
- `from __future__ import annotations` at the top of every module. `__all__` on modules with a
  public surface.

## Imports

```python
# top level: stdlib, third-party, first-party — ruff "I" enforces the order
from felix.config import Settings


# inside the function: anything optional or heavy
def build() -> None:
    try:
        from felix.tools.browser import tools_from_browser_refs
    except Exception:
        logger.warning("browser tool binding failed", exc_info=True)
```

Optional extras (Playwright, DuckDB, Presidio, Temporal, boto3, google-cloud) are **never**
imported at module top level. Degrade with a `logger.warning` when a binding failure should not
fail the whole build; raise when it must.

## Async

- Everything on the request path is `async`. Use `httpx.AsyncClient`, `AsyncSession`, async
  generators for streaming (`stream_events`).
- Never block the loop: no `time.sleep`, no sync DB driver, no sync `requests`.
- `asyncio_mode = "auto"` in pytest — write `async def test_…` with no decorator.
- Timeouts: pass them explicitly. The repo disables `ASYNC109`/`ASYNC240` because some sync
  `Path`/timeout APIs in the CLI and approval waiters are deliberate.

## Errors and logging

- `logging.getLogger("felix.<area>")`. Log a warning with `exc_info=True` for degraded paths;
  never `except: pass` silently.
- Tool wrappers return an error `ToolOutput` rather than raising into the model loop, unless the
  run must abort.
- Error strings must never contain secrets, tokens, or full credentials.

## Lint and types

```bash
uv run ruff check .          # E, F, I, UP, B, ASYNC, SIM, RUF
uv run ruff format .
uv run ty check packages apps
```

`ruff format` also formats Python code blocks **inside Markdown**, and CI runs
`ruff format --check .` over the whole repo — a snippet in a `.md` file (including `.claude/`
skills) can fail the format gate. Run `uv run ruff format <file.md>` after writing one.

`[tool.ruff.lint] ignore` in `pyproject.toml` documents each disabled rule and why (`B008` FastAPI
`Depends`, `E731` the `now_ms = lambda` clock idiom, `SIM102` explicit nested auth guards,
`RUF001/002` intentional en-dashes). Do not "fix" these or add per-line `noqa` to route around
them. Tests relax `E501`, `RUF012`, `RUF034`.

`ty` runs with `error-on-warning = false` and several rules downgraded to `warn` (see
`[tool.ty.rules]`) — unresolved imports are still errors, which is the point: CI gates new import
breakage without blocking on pre-existing typing debt.

## Comments

Match the file's density. Comment *why*, not what — the load-bearing ones in this repo explain
ordering and invariants (`# Governance pipeline (order matters — matches TS builder).`). Keep them.
