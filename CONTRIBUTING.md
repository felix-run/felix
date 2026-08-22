# Contributing to Felix

Thanks for helping improve Felix. This is a self-hostable **agents harness** —
manifest-driven agents with governance, durable execution, and multi-protocol
surfaces (`apiVersion: felix/v1`).

## Development

```bash
cp .env.example .env
# openssl rand -hex 32  → set POSTGRES_PASSWORD

make install      # lean core + dev
make check        # ruff + ty + pytest
make up           # Compose: api, worker, Postgres+pgvector, Valkey
make migrate
```

Optional:

```bash
make install-warehouse   # DuckDB analytics spill
make install-full        # all extras
pre-commit install       # after make install
```

## Guidelines

- Prefer small, focused PRs.
- Match existing patterns in `packages/harness` (Protocols, `FELIX_` settings).
- Keep the default Docker/Compose path **lean** (no heavy extras unless gated).
- Do not add Cloudflare Workers / Durable Objects / Hyperdrive compute.
- Optional features stay out of core — register via the plugin registry /
  `felix.plugins` entry points (see `felix.plugins`).
- Tests: `uv run pytest -q` (CI uses lean `uv sync --dev`).

## Pull requests

- Describe **why**, not only what.
- Note how you tested (`make check`, Compose smoke, etc.).
- Update `.env.example` / README when adding settings.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Security

See [SECURITY.md](SECURITY.md) — please do not open public issues for
vulnerabilities.
