# Contributing to Felix

Thanks for helping improve Felix. This is a self-hostable **agents harness** —
YAML manifests (`apiVersion: felix/v1`) compile into governed agents with
durable fibers, memory, skills, eval, approvals, and sandboxes, exposed over
OpenAI, A2A, MCP, and SSE.

## Development

```bash
make bootstrap    # tools check, .env with generated passwords, lean install, pre-commit hook
make check        # ruff + ty + pytest + format check (ty is skipped, loudly, on a lean install)
make up           # Compose: migrate, api, worker, scheduler, Postgres+pgvector, Valkey
```

`make help` lists every target by group. `make check-ci` adds the rest of what CI gates on
that needs no infrastructure, and it fails rather than skips `ty` on a lean install. Run
`make install-full` first.

Optional:

```bash
make install-warehouse   # DuckDB analytics spill
make install-full        # all extras
./scripts/test.sh -k <expr>   # one test / one theme
```

## Guidelines

- Prefer small, focused PRs.
- Match existing patterns in `packages/harness` (Protocols, `FELIX_` settings).
- Keep the default Docker/Compose path **lean** (no heavy extras unless gated).
- Do not add Cloudflare Workers / Durable Objects / Hyperdrive compute. Calling a hosted
  Cloudflare API over HTTPS is not that — Workers AI is a model provider and R2 is reachable
  through its S3 endpoint. The rule is about where Felix runs, not whose API it calls.
- Optional features stay out of core — register via the plugin registry /
  `felix.plugins` entry points (see `felix.plugins`).
- `make type` (and CI's typecheck job) needs the optional extras: unresolved
  imports are errors by design, and a lean venv cannot resolve
  `boto3`, `duckdb`, `playwright`, `presidio`, … On a lean install `make check`
  skips it with a notice; `make install-full` makes it run.
- Tests: `./scripts/test.sh` — it sets the in-memory stores the suite needs. A bare
  `uv run pytest` picks up `.env` and fails against a real Postgres. CI runs the same script
  after a lean, frozen `uv sync --frozen --dev`.

## Pull requests

- Describe **why**, not only what.
- Note how you tested (`make check`, Compose smoke, etc.).
- Update `.env.example` / README when adding settings.

## License

Felix is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution you agree it is licensed under those same terms, per Apache-2.0
§5 — no separate CLA is required.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Security

See [SECURITY.md](SECURITY.md) — please do not open public issues for
vulnerabilities.
