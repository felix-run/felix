---
name: felix-conventions
description: The Python style and architecture conventions that make Felix code look like Felix code — settings, lazy optional imports, Protocols, the ruff and ty configuration, and the files that must change together when you add a setting or a model. Use when writing or reviewing Python in this repo, or when a lint or type check disagrees with an edit.
---

> This is a summary. The source of truth is CLAUDE.md (Conventions) and `.claude/skills/python-conventions/SKILL.md` — read it with `read_file` when
> the two disagree, and trust the checkout over this file.

# Felix conventions

## Settings

All settings are pydantic-settings fields on `felix/config.py:Settings` with the `FELIX_` env
prefix. A new setting lands in three places at once — `felix/config.py`, `.env.example`, and the
README table — plus a `validate_runtime()` guard if it enables an unsafe combination. An invariant
test fails the build if `.env.example` does not cover every field.

Values may be supplied as `secret:NAME` refs on outbound manifest fields; the default env secrets
backend resolves `secret:NAME` by reading the plain environment variable `NAME`.

## Optional imports go inside the function

Heavy dependencies — Playwright, sentence-transformers, DuckDB, Presidio, Temporal, docker,
cloud SDKs — live behind extras and are imported lazily inside the function that needs them,
wrapped in `try/except` with a `logger.warning` when a binding failure should degrade rather than
fail the build. Never at module top level. The default install and the default image stay lean,
and an invariant test enforces it.

## Protocols, not vendors

Storage, secrets, model providers, and the warehouse are swappable implementations behind
Protocols. A list that selects one of them is an open registry, and the setting that selects one
is an open `str` validated against that registry, never a closed `Literal`. A closed list is a
decision that needs a written reason next to it.

## Lint and types

- ruff, line-length 110, `target-version = py314`.
- The `ignore` list in `pyproject.toml` documents why each rule is off. Read it before "fixing"
  an E731 or a SIM102.
- `make type` and CI both run `ty check packages apps`. Tests are excluded on purpose, because
  the fakes trip `ty`.
- Type checking needs the optional extras installed (`make install-full`). In a lean venv every
  optional dependency reports as an unresolved import, which is an error; `make type` checks for
  this and says so.
- Python 3.14 is the target, so 3.14 syntax is fine — including PEP 758 unparenthesized multiple
  exception types (`except A, B:`). That is valid, not a Python 2 leftover.

## Database

Postgres is the system of record. A model change needs an Alembic revision, and published
revisions are never edited. Every Postgres-touching module needs a `memory://` path, because that
is the CI test path.

## Errors and logging

Degrade rather than fail when an optional binding is missing, and log a warning that names what
was lost. Log the key, never the secret. Values that reach a log from a manifest or a model are
attacker-controlled in the prompt-injection sense — pass them through `loggable` so a newline
cannot forge a log entry.

## Documentation moves with the code

`docs/ROADMAP.md` tracks in-flight work and is expected to be updated in place. Public docs live
in a separate repo and are synced from surface changes, not written from memory.
