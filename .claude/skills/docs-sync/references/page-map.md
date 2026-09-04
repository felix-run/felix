# Surface → documentation page map

`$DOCS` = `${FELIX_DOCS_ROOT:-~/Projects/felix-web/apps/docs}/src/content`

## Public site (felix-web repo)

| Felix surface | Page |
|---|---|
| `apps/api/.../routes/chat.py`, `openai_compat.py`, `a2a.py`, `mcp.py`, `well_known.py` | `$DOCS/guide/rest-api.mdx` |
| `apps/api/.../routes/{audit,approvals,plans,jobs,manifests,eval,usage,internal}.py` | `$DOCS/guide/management-api.mdx` |
| `packages/harness/src/felix/manifests/schema.py`, `manifests/*.yaml` | `$DOCS/guide/manifest-reference.mdx` |
| `manifests/{builder,resolver,pin}.py` | `$DOCS/internals/manifest-pipeline.mdx` |
| `patterns/{react,registry,types}.py` | `$DOCS/internals/patterns.mdx` |
| `patterns/model*.py` (client, registry, composites) | `$DOCS/internals/model-client.mdx` |
| `auth/*`, `manifests/inbound_auth.py` | `$DOCS/internals/auth.mdx` |
| `governance/*`, `security/*`, `manifests/governance.py` | `$DOCS/internals/governance.mdx` |
| `db/*`, `session/store.py`, `migrations/versions/*` | `$DOCS/internals/persistence.mdx` |
| `observability/*`, `audit/*`, `usage/*` | `$DOCS/internals/observability.mdx` |
| `config.py`, `deploy/**`, `Makefile`, `.env.example` | `$DOCS/guide/deploy.mdx`, `$DOCS/guide/getting-started.mdx` |
| `sdk.py`, `clients/cli.py`, `packages/cli/.../main.py` | `$DOCS/guide/getting-started.mdx` |
| `felix/skills/*`, `skills/*/SKILL.md` | `$DOCS/guide/concepts.mdx` |
| `tests/**` (only if the testing story changed) | `$DOCS/internals/testing.mdx` |

Landing page: `$DOCS/index.mdx`. Collection config: `apps/docs/src/content.config.ts` — prose lives
at `src/content/` (not `src/content/docs/`), loaded by a glob loader with Starlight's `docsSchema`.

## In-repo companions (change with the code, same PR)

| Change | Also update |
|---|---|
| New/changed `FELIX_` setting | `.env.example` (with a comment), README settings/extras table, `deploy/docker/compose*.yml`, `deploy/helm/felix/values.yaml` |
| New governance control | `deploy/GOVERNANCE.md`, `manifests/governed.yaml` |
| New CLI command or flag | README quick-start, `CONTRIBUTING.md` if it is part of the dev loop |
| New surface/protocol | README protocol table |
| Architecture or workflow change | `CLAUDE.md` |
| User-visible behavior | `CHANGELOG.md` (Unreleased), `docs/ROADMAP.md` status |

## Voice

Dense, factual, present tense. Identifiers, paths, env vars in backticks. Tables for surfaces,
settings, and defaults. Every command shown must be runnable as written from the repo root. No
"simply", "just", or marketing adjectives.
