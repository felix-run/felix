# Felix roadmap

Living tracker for what to build next. Update status in place; keep items
concrete enough to pick up in a single session.

**Repos:** `felix-run/felix` (harness) · `felix-run/web` (chat-ui + float + docs)  
**Live:** [api.felix.run](https://api.felix.run) · [chat.felix.run](https://chat.felix.run) · [float.felix.run](https://float.felix.run) · [docs.felix.run](https://docs.felix.run)  
**Last reviewed:** 2026-08-22 (post governance + inbound screening)

---

## How to use this file

| Status | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress / partial |
| `[x]` | Done (keep briefly for history, then move to **Shipped**) |
| `[!]` | Blocked / deferred on purpose |

When you finish an item: mark `[x]`, note the commit if useful, then fold it
into **Shipped** on the next tidy pass. Pick from **Now** unless a demo needs
something from **Next**.

---

## Suggested pick-up order

1. **Durable client poll** (`FelixClient.prompt` + `/chat/stream` behavior).
2. **Cowork completion smoke** on GCE (soft poll in `smoke.yml`).
3. **`spec.a2a` agent card** → then **`spec.anomaly`** wiring.
4. **`inbound.schemes`** enforce-or-drop; same for **`outbound.providers`**.
5. Docs getting-started (Python) + governed demo policy decision.
6. Tag `v0.1.1` / `v0.2.0` from Unreleased (`CHANGELOG.md`).

When in doubt: **dogfood float → fix what breaks → write it down here.**

---

## Now (next 1–2 sessions)

### 1. Close the durable loop

`spec.execution.mode: durable` on `POST /chat` returns **202** +
`resume_token`. Poll is `GET /chat/runs/{resume_token}`. The rest of the
surface still pretends every chat is synchronous.

- [ ] **`FelixClient.prompt`** — on HTTP 202, poll `GET /chat/runs/{resume_token}`
      until `completed` / `failed` / `expired` (honor `expires_at`). Emit
      progress events to subscribers. Add `wait_s` so callers can return the
      202 payload without waiting.
- [ ] **`POST /chat/stream`** — same durable enqueue as `/chat`, then either
      SSE-poll the run (status + final) or return 202 and document that
      streaming stays inline-only. Do not silently ignore `mode: durable`.
- [ ] **Cowork completion on GCE** — idle BRPOP / scheduler fixes shipped
      (`9f79a18`); local durable poll reached `completed`. Prod smoke still
      only asserts cowork `202` accept. Dogfood one Background run on float →
      `completed`, then extend `.github/workflows/smoke.yml` with a
      **soft** completion poll (`continue-on-error: true`, ~3 min).

Files: `packages/harness/src/felix/sdk.py`,
`apps/api/src/felix_api/routes/chat.py`,
`packages/harness/src/felix/durability/runs.py`,
`.github/workflows/smoke.yml`.

### 2. Manifest fields that still do nothing

- [ ] **`spec.a2a` → agent card** — `publish` and `capabilities` are unused.
      `GET /.well-known/agent-card.json` always advertises streaming + MCP and
      never lists manifest skills. Honor `publish` (404 or empty card when
      false), merge `spec.a2a.capabilities`, and emit `spec.skills`.
      File: `packages/harness/src/felix/a2a/card.py`.
- [ ] **`spec.anomaly` → worker scan** — `AnomalySpec` (`enabled`,
      `min_volume`, `min_rate`, `baseline_factor`) is ignored.
      `jobs/anomaly.py` uses hardcoded `MIN_VOLUME=10` / `BASELINE_FACTOR=3.0`.
      Load the tenant manifest (or per-manifest rows) and skip when
      `enabled: false`.
- [ ] **`spec.auth.inbound.schemes`** — `allow_anonymous` and
      `required_scopes` are enforced. `schemes` is only a governance compile
      check. Enforce against the request principal, or drop the field.
- [ ] **`spec.auth.outbound.providers`** — unused. Constrain which secret
      backends / model providers a manifest may call, or remove it.
- [ ] **`spec.observability.metrics`** — tracing is process-global; the
      per-manifest name list does nothing. Allowlist `record_counter` names
      or delete the field.

### 3. Docs + demo policy

- [ ] **Docs getting-started (Python)** — Starlight at docs.felix.run still
      has TS/Workers-era prose. Operator path: Compose → migrate → `quick` /
      `cowork` → chat-ui / float against `api.felix.run`. (Likely
      `felix-run/web` docs sources; keep a short pointer in this README.)
- [ ] **Governed demo path (decide)** — `manifests/governed.yaml` +
      `deploy/GOVERNANCE.md` + Helm ESO are in. Either enable on GCE
      (RBAC scopes for chat/float keys) **or** keep demo anonymous and
      document that choice explicitly in `deploy/GOVERNANCE.md`.
- [!] **Rotate Anthropic API key** — only when you say go. Then Secret
      Manager `felix-anthropic-api-key` + recreate API/worker.

### 4. Release hygiene

- [ ] **Tag from Unreleased** — fold worker fix, Redis leases, session
      control, full governance wave, inbound screening into `v0.1.1` or
      `v0.2.0` (`CHANGELOG.md`). Update Unreleased notes before tagging.

---

## Next (this quarter)

### Harness

- [ ] **Temporal Compose profile** — `FELIX_DURABILITY=temporal` +
      `felix temporal-worker` exist; optional compose profile + README for
      long HITL demos. Keep Postgres fibers as default.
- [ ] **Live-model eval (optional CI)** — fixture/`--mock` is in CI;
      optional nightly against `api.felix.run` that does not block PRs
      (`--llm-judge` opt-in).
- [ ] **Scale-out proof** — Redis leases + steer/waiters are multi-replica
      capable; document and smoke two API replicas behind one origin.
- [ ] **Sandbox ladder extras** — capability-bridge / gVisor as documented
      extras, not default lean image.
- [ ] **OAuth / dynamic provider keys** — secrets backends cover static keys;
      refresh/`getApiKey(provider)` only if a real customer path needs it.
- [ ] **Schema cleanup pass** — delete dead `memory.checkpointer` aliases
      (`agentcore` / `do` / `sqlite`) and any other unused fields after
      enforce-or-drop decisions above.

### Product (`felix-run/web`)

- [ ] **Session-control UX gaps** — export JSONL from UI, clearer
      lease-contention copy, reconnect-to-snapshot after hard refresh,
      empty/search states.
- [ ] **Usage → cost view** — rollup by manifest/day + $ estimate from
      model catalog metadata.
- [ ] **Float as primary cowork surface** — chat-ui stays general console;
      float owns mount/VFS/approvals/Background.
- [ ] **Prune leftover TS-harness skills/copy** in docs sync sources after
      the getting-started rewrite.

### Deploy

- [ ] **GKE dogfood** — Helm + ESO → one known-good install note under
      `deploy/gcp/`.
- [ ] **AWS smoke checklist** — mirror GCP path (Secrets Manager / S3) in
      `deploy/aws/`.
- [ ] **Postgres RLS dogfood** — migration `0006` + `FELIX_DATABASE_RLS=true`
      on a non-prod branch; verify retention bypass + mixed-tenant audit flush.

---

## Later / explicit non-goals

- [!] **`memory.consolidate` LLM merge** — worker already hash-dedupes.
      `enabled` / `model` / `after_facts` stay unused on purpose (v1).
- [!] **`memory.checkpointer` aliases** — dead vendor names; delete on
      schema cleanup, do not implement.
- [!] **`FELIX_POLICY_BUNDLE_PUBKEY` / OPA** — no signed policy-bundle
      runtime in v1 (governance stays manifest compile + wrappers).
- [!] **Commerce / billing plugin** — seam only; no Stripe in-tree.
- [!] **Cloudflare compute in the harness** — Workers/DOs out of `felix`;
      CF hosts web + named tunnel to GCE API only.
- [!] **Merging web into `felix`** — keep repos split.
- [!] **Third-party TUI / CBOR / npm package installer** — session/loop
      ideas only; composition is YAML + `felix.plugins` entry points.

---

## Shipped (recent)

### Governance / secrets wave (Aug 2026)

- [x] Manifest `secret:NAME` refs + plaintext forbid + output masking
- [x] State scrub (session / audit / fiber) + `pin_compile` drift refuse
- [x] Inbound auth (`allow_anonymous` / `required_scopes`) on chat / v1 / A2A
- [x] Inbound `/mcp` through compiled agent wrappers
- [x] `spec.governance` SOC2 / EU AI Act fail-closed + Art. 50 notice + audit emit
- [x] `felix validate-manifest` + `manifests/governed.yaml` + `deploy/GOVERNANCE.md`
- [x] Management-API RBAC scopes; Helm External Secrets template
- [x] `command_screening.include_defaults`
- [x] Presidio (opt-in extra) + regex residual PII
- [x] Opt-in LLM judges (`--llm-judge` / rubric / `JudgeRule.model`)
- [x] Opt-in Postgres RLS (`0006_tenant_rls`, `FELIX_DATABASE_RLS`)
- [x] Inbound user-turn screening (injection + input PII) on chat / v1 / A2A
- [x] CI green (ruff/format, ty scope, compose secrets, OpenAPI URL)

Commits (approx): `34c667f` … `87ae629`.

### Session / Pi-shaped depth (Aug 2026)

- [x] Snapshots, list/name/label, abort/continue, thinking levels
- [x] Parallel tools, branch summaries, compaction, steering modes
- [x] FTS search, Redis-backed leases, JSONL export, UI prompts
- [x] Comparative evals; chat-ui + float wired for search / thinking / rewind

### Product / cowork

- [x] Client tools bridge + approval interrupt/resume + workspace tools
- [x] `manifests/cowork.yaml` + Float + chat-ui cowork default
- [x] Shared `@felix/cowork-client`

### Ops

- [x] `api.felix.run` tunnel; GCE loopback; GCP Secret Manager hydrate
- [x] Smoke workflow (health + quick + cowork accept + session surfaces)
- [x] Worker idle BRPOP / scheduler `asyncio.run` / Compose healthcheck fix
- [x] `v0.1.0` release
