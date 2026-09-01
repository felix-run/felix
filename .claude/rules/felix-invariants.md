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
- **Extensibility is the product.** Felix must not dictate a workflow: what other harnesses
  bake in should be buildable here as a plugin, a skill, or a third-party package, with core
  staying minimal. Concretely — a list that selects a swappable implementation is an open
  registry (`register_pattern`, `register_model_provider`, `register_object_store`,
  `register_secrets_backend`, `register_warehouse_backend`, `register_embedder_backend`,
  `register_session_strategy`, `register_checkpointer`), and the setting that selects one is an open `str` validated
  against that registry, never a closed `Literal`. A closed list is a decision that needs a
  written reason next to it. Details: the **plugin-seam** skill.
- **A registration seam must have a reader.** Every `PluginRegistry.register_*` method is
  consumed somewhere in core; `tests/unit/test_invariants.py` enforces it. A seam that accepts
  input and silently drops it is worse than no seam.
- **Trust is an allowlist.** `Tool.executor.transport` is open, so governance decides trust by
  what is known-safe (`_TRUSTED_TRANSPORTS`), never by a denylist — a denylist fails open for
  exactly the third-party transports the seam exists to allow.
- **Core never names an optional plugin.** `apps/api/src/felix_api/composition.py` is the only
  place; everything else goes through `felix/plugins.py`. `tests/unit/test_plugin_boundary.py`
  enforces it. Details: the **plugin-seam** skill.
- **The default install and image stay lean.** Heavy dependencies live behind extras and are
  imported lazily inside the function that needs them — never at module top level.
- **Protocols, not vendors.** Storage, secrets, model providers, and the warehouse are swappable
  implementations behind Protocols.
- **`packages/ai` never imports `felix`.** The model layer is a separate workspace member so
  model-agnosticism is structural, not aspirational; `tests/unit/test_invariants.py` walks every
  import node, so a lazy in-function import is not an escape hatch. What the harness needs to
  inject goes through a Protocol (`ToolSchema`, `ModelConfig`) or a sink
  (`felix_ai.observability`, `felix_ai.context`).
- **`memory://` must keep working.** Every store has an in-memory twin; that is the CI test path.
  Run tests with `./scripts/test.sh` (or `make test`), never a bare `pytest`.
- **A model change needs an Alembic revision**, and published revisions are never edited.
- **A new `FELIX_` setting** lands in `felix/config.py` + `.env.example` + the README table, with a
  `validate_runtime()` guard if it enables an unsafe combination.
- **No Cloudflare Workers / Durable Objects / Hyperdrive / R2-binding / Queues compute.** Felix runs
  on infrastructure the operator manages; Cloudflare DNS/CDN/TLS/WAF in front of an origin is fine.
  The line is *compute*, not vendor: `workers_ai` is a registered model provider and `storage/s3.py`
  reaches R2 through its S3 endpoint, because those are outbound HTTPS calls like any other
  provider. What is forbidden is Felix *running on* Workers or Durable Objects, or depending on a
  binding only reachable from inside them.
- **Postgres is the system of record**; the warehouse is optional append-only spill written after
  the Postgres write.
- **`felix-scheduler` runs alongside `felix-worker`**, or no periodic job fires.
- Commit and push only when the user asks; branch first. Details: the **branch-pr-workflow** skill.
