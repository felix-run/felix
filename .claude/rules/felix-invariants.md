---
paths:
  - "**/*"
---

# Felix invariants

Rules that hold across the whole repo. Violating one is a blocking review finding, not a style note.

## The defect shape this repo produces

Nearly every real defect here is **a control that looks present and does nothing**, and its most
common cause is that *the branch production takes is the branch nothing covers*. Three rules follow
from it, and they are cheap:

- **Exercise the production call, not a convenient one.** A parameter with a default that every
  test supplies is untested. `create_app()` shipped reading `settings.x` instead of `cfg.x` and died
  at boot, green suite and all, because production is the only caller that passes nothing.
  `tests/unit/test_entrypoint_wiring.py` now calls each entrypoint the way its console script does.
- **A test that cannot fail is worse than no test.** Prove a new one by mutation: introduce a real
  violation of the rule, watch the test go red, revert, confirm the tree is clean. An ERROR is not a
  failure — it means the test is wrong and says nothing either way. An AST invariant here matched
  `timeout=<Constant>` while every literal it hunted lived inside `httpx.Timeout(...)`: green the day
  it was written, unable to fail on any file it named.
- **Absence is the claim that rots fastest.** "Nothing reads this field", "this provider is
  fictional", "that has no caller" — re-derive it against the tree at HEAD before acting, never from
  an earlier note in the same session. `SkillRef.description` was nearly deleted as unread after
  `a2a/card.py` had started reading it. Grep first; the **dead-code-audit** skill lists the
  reachability channels grep alone misses.

A fourth, for changes to controls: **validating a value for one grammar does not validate it for the
next one.** A hostname checked against a DNS-name pattern was interpolated into
`--host-resolver-rules`, which is a comma-separated list, so a comma in the host reached every name
past it. When a validated value crosses into a command line, a header, a URL, a query, or a log
line, re-validate it against *that* grammar's separators. Details: the **security-review** skill.

- **Wrapper order in `manifests/builder.py` is load-bearing.** secret masking → policies → command
  screening → content screening → limits → guardrails → judges → approvals → artifact spill →
  workspace scope. Each wrapper clones the tool with a new executor, so order defines precedence.
  Never reorder to make a test pass. Details: the **governance-pipeline** skill.
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
- **A caller's error answer is written, never forwarded from an exception.** No `str(exc)`,
  `repr(exc)`, `exc.args` or `f"...{exc}"` in an `HTTPException` detail, a JSON body, a stored
  state a route returns, or any helper that builds one. An exception's text is the operator's
  business — an egress proxy's name, a DNS failure, a filesystem path, an upstream's own error — and
  CodeQL reports it as `py/stack-trace-exposure` (#475 shipped six in one file). Reach first for
  `felix_api.errors.client_safe_message(exc)`: it relays only the exception types listed in
  `_relayable()` — each one's message written for a client — and answers anything else with a fixed
  string and the request id. A message a call site wrote for the caller itself passes
  `authored_for_clients=True`, and is caught by its *own* exception type, never a bare `ValueError`
  or `LookupError` that something deeper could raise next. Where there is no exception type to lean
  on, choose the message by the error's code from values the route already validated and log the
  detail: `routes/repos.py` (`GITHUB_UNAVAILABLE`, `_checkout_refusal_message`). `tests/unit/test_route_error_text.py` fails a route that does it; its `KNOWN_OPEN`
  only shrinks. The scan covers route modules only, so a message a route stores and later returns
  (a checkout's `error`) is held to this by review — classify it where it is written.
- **A caller-supplied value reaches a log line only through `felix.logging_setup.loggable()`.** A
  request body field, a path or query parameter, a header, a thread id: wrap it, with a `limit`
  that fits what it should be (`loggable(body.full_name, limit=200)`). The formatter escapes too,
  but CodeQL's `py/log-injection` reads the call site, not the formatter, and #475's fix for the
  rule above introduced one by logging a request field bare. A value already validated by a pattern
  still goes through it: the pattern is invisible to the scanner and to the next person who
  loosens it.
- Commit and push only when the user asks; branch first. Details: the **branch-pr-workflow** skill.

## What the work is for

Two rules on the *balance* of work, added 2026-09-02 after an audit found that roughly forty
consecutive commits were remediation of defects found by reading the tree rather than by running
it. Both are budget rules, not quality rules — the self-audit work was good, and there was too
much of it relative to everything else.

- **A control may not be added for a capability that does not exist.** `manifests/builder.py`
  reached 1,401 lines governing a built-in tool registry of `calculator` plus three skill stubs
  and four file tools, while `support.yaml` — the support agent — shipped with
  `tools: [calculator, list_skills]` and no way to look anything up. Governance is the best-built
  part of this harness and it was guarding almost nothing. Before hardening a wrapper, check that
  something reaches it.
- **One hardening / invariant / audit item per cycle; everything else adds user-visible
  capability.** A defect found in a *real run* is exempt — that is the feedback loop working, and
  it is the loop this rule exists to protect. A defect found by re-reading a file you already
  audited is the thing being budgeted. `docs/ROADMAP.md` carries the current cycle's items.
