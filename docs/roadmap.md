# Felix roadmap

Living tracker for what to build next. Update status in place; keep items
concrete enough to pick up in a single session.

**Repos:** `felix-run/felix` (harness) · `felix-run/web` (chat-ui + float + docs)  
**Live:** [api.felix.run](https://api.felix.run) · [chat.felix.run](https://chat.felix.run) · [float.felix.run](https://float.felix.run) · [docs.felix.run](https://docs.felix.run)  
**Last reviewed:** 2026-08-22

---

## How to use this file

| Status | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress / partial |
| `[x]` | Done (keep briefly for history, then move to **Shipped**) |
| `[!]` | Blocked / deferred on purpose |

When you finish an item: mark `[x]`, note the commit if useful, then fold it
into **Shipped** on the next tidy pass.

---

## Now (next 1–2 sessions)

Highest-leverage unfinished work after the Pi re-audit wave and governance
landing.

### Ops / reliability

- [ ] **Durable cowork completion on GCE** — idle BRPOP / scheduler fixes
      shipped (`9f79a18`); local durable poll reached `completed`. Prod smoke
      still only asserts cowork `202` accept. Dogfood one Background run on
      float → `completed`, then optionally extend `.github/workflows/smoke.yml`
      to poll to completion (non-blocking).
- [ ] **Docs getting-started (Python)** — Starlight at docs.felix.run still
      has TS/Workers-era prose in places. Operator path should be: Compose →
      migrate → `quick` / `cowork` → chat-ui / float against `api.felix.run`.
- [!] **Rotate Anthropic API key** — deferred until you say go. Then Secret
      Manager `felix-anthropic-api-key` + recreate API/worker.

### Product polish

- [ ] **Session-control UX gaps** — API + chat-ui/float have snapshots,
      abort/continue, thinking, leases, FTS, rewind. Still thin: export JSONL
      from UI, clearer lease-contention copy, reconnect-to-snapshot after
      hard refresh, empty/search states.
- [ ] **Governed demo path (decide)** — `manifests/governed.yaml` +
      `deploy/GOVERNANCE.md` + Helm ESO are in. Either enable on the GCE
      stack (RBAC scopes for chat/float keys, secret refs, command screening)
      or keep demo anonymous and document that choice explicitly.

### Release hygiene

- [ ] **Tag from Unreleased** — fold worker fix, Redis leases, session
      control, governance into `v0.1.1` or `v0.2.0` when ready (`CHANGELOG.md`).

---

## Next (this quarter)

### Harness

- [ ] **Temporal Compose profile** — `FELIX_DURABILITY=temporal` exists;
      optional compose profile + README for long HITL demos. Keep Postgres
      fibers as default.
- [ ] **Live-model eval (optional CI)** — fixture/`--mock` eval is in CI;
      optional nightly workflow against `api.felix.run` that does not block PRs.
- [ ] **Scale-out proof** — Redis leases + steer/waiters are multi-replica
      capable; document and smoke two API replicas behind one origin.
- [ ] **Sandbox ladder extras** — capability-bridge / gVisor as documented
      extras, not default lean image.
- [ ] **OAuth / dynamic provider keys** — secrets backends cover static keys;
      refresh/`getApiKey(provider)` only if a real customer path needs it.

### Web (`felix-run/web`)

- [ ] **Usage → cost view** — inspector Usage tab lists meters; add rollup by
      manifest/day and $ estimate from model catalog metadata.
- [ ] **Float as primary cowork surface** — chat-ui stays general console;
      float owns mount/VFS/approvals/Background. Close remaining session-control
      polish called out under **Now**.
- [ ] **Prune leftover TS-harness skills/copy** in docs sync sources if any
      remain after the getting-started rewrite.

### Deploy

- [ ] **GKE dogfood** — Helm + ESO examples → one known-good install note
      under `deploy/gcp/`.
- [ ] **AWS smoke checklist** — mirror GCP path (Secrets Manager / S3) in
      `deploy/aws/`.

---

## Later / explicit non-goals

- [!] **Commerce / billing plugin** — seam only; no Stripe in-tree.
- [!] **Cloudflare compute in the harness** — Workers/DOs out of `felix`;
      CF hosts web + named tunnel to GCE API only.
- [!] **Merging web into `felix`** — keep repos split.
- [!] **Pi TUI / CBOR / npm packages** — session/loop ideas only; see
      `.cursor/plans/pi_packages_re-audit_*.plan.md`.

---

## Shipped (recent)

Fold older bullets here so **Now** stays short.

### Session / Pi-shaped depth (Aug 2026)

- [x] Snapshots, list/name/label, abort/continue, thinking levels
- [x] Parallel tools, branch summaries, compaction (`retainedTail`, compact
      endpoint), steering modes, compact-after-turn
- [x] FTS search, Redis-backed leases, JSONL export, UI select/confirm/input
- [x] Model catalog metadata, multimodal content blocks, cross-provider
      handoff helpers, prompt-cache key from thread
- [x] Comparative evals; chat-ui + float wired for search / thinking / rewind /
      abort / leases

### Product / cowork

- [x] Client tools bridge + approval interrupt/resume + workspace tools
- [x] `manifests/cowork.yaml` + Float + chat-ui cowork default
- [x] Shared `@felix/cowork-client`

### Ops / trust

- [x] `api.felix.run` tunnel; GCE loopback `:8080`
- [x] GCP Secret Manager hydrate; smoke workflow (health + quick + cowork
      accept + session surfaces)
- [x] Worker idle BRPOP / scheduler `asyncio.run` / Compose healthcheck disable
- [x] Governance: secret refs, compile pins, managed RBAC, Helm ESO,
      `governed.yaml`, `deploy/GOVERNANCE.md`
- [x] `v0.1.0` release

---

## Suggested pick-up order

1. Durable cowork completion smoke on GCE (closes the open reliability gap).
2. Docs getting-started pass for Python + live URLs.
3. Governed mode on the demo stack — or explicitly keep demo loose.
4. Tag `v0.1.1` / `v0.2.0` from Unreleased.
5. Temporal profile or live eval — whichever demos need next.

When in doubt: **dogfood float → fix what breaks → write it down here.**
