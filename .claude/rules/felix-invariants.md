---
paths:
  - "**/*"
---

# Felix invariants

Rules that hold across the whole repo. Violating one is a blocking review finding, not a style note.

- **Wrapper order in `manifests/builder.py` is load-bearing.** secret masking → policies → command
  screening → content screening → limits → guardrails → judges → approvals → artifact spill. Each
  wrapper clones the tool with a new executor, so order defines precedence. Never reorder to make a
  test pass. Details: the **governance-pipeline** skill.
- **Core never names an optional plugin.** `apps/api/src/felix_api/composition.py` is the only
  place; everything else goes through `felix/plugins.py`. `tests/unit/test_plugin_boundary.py`
  enforces it. Details: the **plugin-seam** skill.
- **The default install and image stay lean.** Heavy dependencies live behind extras and are
  imported lazily inside the function that needs them — never at module top level.
- **Protocols, not vendors.** Storage, secrets, model providers, and the warehouse are swappable
  implementations behind Protocols.
- **`memory://` must keep working.** Every store has an in-memory twin; that is the CI test path.
  Run tests with `.claude/scripts/felix-test.sh`, never a bare `pytest`.
- **A model change needs an Alembic revision**, and published revisions are never edited.
- **A new `FELIX_` setting** lands in `felix/config.py` + `.env.example` + the README table, with a
  `validate_runtime()` guard if it enables an unsafe combination.
- **No Cloudflare Workers / Durable Objects / Hyperdrive / R2-binding / Queues compute.** Felix runs
  on infrastructure the operator manages; Cloudflare DNS/CDN/TLS/WAF in front of an origin is fine.
- **Postgres is the system of record**; the warehouse is optional append-only spill written after
  the Postgres write.
- **`felix-scheduler` runs alongside `felix-worker`**, or no periodic job fires.
- Commit and push only when the user asks; branch first. Details: the **branch-pr-workflow** skill.
