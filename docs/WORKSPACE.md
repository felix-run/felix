# Workspaces that are not the server

**Status: proposal, 2026-09-24; revised the same day to make a hosted sandbox service the production
backend.** Built so far: phase 0 (the default volume), phase 1 (truthful failures) and phase 2a
(scopes on the `local` layout) and phase 2b (the `WorkspaceBackend` seam). This file is the design the workspace tools are to be moved onto;
it is updated in place as each phase lands, like [SELF.md](SELF.md).

The workspace tools — `list_dir`, `read_file`, `write_file`, `edit_file`, `search_files` — are how a
model changes files. Today they are ordinary file I/O inside the API and worker processes, against one
directory. This proposal moves that I/O out of the process that holds the deployment's credentials,
scopes it per tenant and per thread, and keeps the tool surface a manifest sees exactly as it is.

## What production actually does today

Measured on the reference deployment (GCE + Compose, v0.4.0) on 2026-09-24, not inferred:

- **One directory for everyone.** `workspace_root()` reads a single `FELIX_WORKSPACE_ROOT`. The
  context carries `tenant_id` and `thread_id` (`felix/context.py`), and nothing in
  `tools/workspace.py` uses either. Two threads — or two tenants — read and overwrite each other's
  files.
- **The default is the server's own checkout.** `compose.yml` mounts
  `${FELIX_WORKSPACE_HOST:-./workspace}:/workspace`, and `./workspace` is a directory inside the
  deployment's git checkout. An agent's writes landed in `/opt/felix/workspace`, beside the compose
  files and `.env`, until the operator set `FELIX_WORKSPACE_HOST` by hand.
- **In the process with the credentials.** The tools run in the API (streamed runs) and the worker
  (durable runs), the processes holding `FELIX_DATABASE_URL`, provider keys and every
  `FELIX_AUTH_API_KEYS` token. `resolve_under_root` keeps a *path* inside the root; it is the only
  boundary, and it is a boundary on paths, not on the process.
- **The mount's ownership is the host's, not the image's.** The published image runs as uid `10001`;
  the bind-mounted checkout directory was `1001:1002`. Every write failed with `Errno 13` until the
  operator ran `chown`, and would again on any fresh host.
- **The failure was recorded as a success.** Both failed writes appear in the audit log as
  `tool_call` / `ok`: the tool returns `error: [Errno 13] …`, which `is_failure_content` does not
  recognise, because it matches only the bracketed prefixes in `FAILURE_CONTENT_PREFIXES`.

The existing sandbox rung (`spec.sandboxes`, `tools/sandboxes.py`) does not help here. It runs a
Python snippet in a short-lived, well-hardened container — nobody user, read-only root, no network,
128 pids, one CPU, 256 MB — but it is a separate tool, not a home for the workspace, and it starts
containers through the Docker SDK, which needs the daemon's socket.

## Goals

1. **No model-driven file I/O in a process that holds credentials.** The worker asks for a file
   operation; something else performs it.
2. **Scoped.** A workspace belongs to a tenant, and by default to one thread within it. No path
   syntax reaches another scope.
3. **Works unattended.** Durable runs must keep working with no browser connected, which rules out
   the in-browser workspace (client tools) as the answer for them.
4. **The tool surface does not move.** Same five tool names, same argument models, same size limits,
   same approval gating and grant semantics. A manifest written today keeps working unchanged.
5. **Fails closed and says so.** An unavailable backend refuses the call with a failure the audit
   log classifies as a failure.
6. **Operator-chosen.** The isolation level is a deployment setting, with a safe default.

## Non-goals

- **The self-build program's checkout.** `contributor.yaml` and `triage.yaml` bind the workspace
  tools and `spec.shell_tools` against a real git checkout, and their boundary is the builder
  container itself (`compose.self.yml`). That keeps working on the `local` backend below; running
  repository gates inside a sandbox is a separate design.
- **Replacing client tools.** The in-browser workspace stays what it is: the right answer when the
  files belong on the user's machine and someone is watching.
- **Arbitrary code execution in the workspace** — amended 2026-10-07: phase 3b runs a manifest's
  existing `shell_tools` (an allowlisted argv, no shell) inside the scope's sandbox, because a hosted
  workspace whose commands still ran on the host would isolate the files and not the code that
  writes them. Anything beyond `shell_tools` stays out of scope.

## Design

### One seam: `WorkspaceBackend`

Every workspace tool already starts the same way — `root = workspace_root()`, then
`resolve_under_root(root, args.path)`. That pair becomes one call into a backend chosen by
`FELIX_WORKSPACE_BACKEND`:

```python
class WorkspaceBackend(Protocol):
    async def list_dir(self, scope: WorkspaceScope, path: str) -> ListResult: ...
    async def read_file(self, scope: WorkspaceScope, path: str, offset: int, limit: int) -> bytes: ...
    async def write_file(
        self, scope: WorkspaceScope, path: str, data: bytes, append: bool
    ) -> WriteResult: ...
    async def edit_file(
        self, scope: WorkspaceScope, path: str, old: str, new: str, replace_all: bool
    ) -> EditResult: ...
    async def search(
        self, scope: WorkspaceScope, path: str, query: str, regex: bool, max_hits: int
    ) -> SearchResult: ...
```

The tools keep their argument models, limits and messages; they stop touching the filesystem.

**As built (phase 2b, `felix/tools/workspace_backend.py`, `workspace_local.py`).** Two departures
from the sketch above. A `prepare(scope)` call comes first, so a workspace that cannot be served is
still reported before anything wrong with the arguments, the order callers saw before the seam (and
the natural place for a hosted backend to create a sandbox on first use). And the result types are
data, not formatted text: the tools still build every string the model sees. Failures cross the seam
as the exceptions the tools already mapped (`ValueError`, `NotAFileError`, `OSError`), plus
`EditRefused` for an edit the model can correct. The regex screen and the search deadline stay in the
tool; the backend compiles its own copy of the pattern, since a compiled pattern is not something
every backend can be sent. `shell`, the image tools' `path`, `publish_commits` and the context-file
loader still use `workspace_root()` and the local primitives: they need a real directory, and moving
them is phase 3's question.
Containment (`resolve_under_root`), the regex budget in `search_files`, and `edit_file`'s
byte-exact, rename-into-place write move *into* each backend unchanged, so the rules have one
implementation per backend and the same tests run against all of them.

Approvals are untouched by construction: the gate runs in the tool wrapper before `execute`, so a
refused call never reaches a backend, and an approved one reaches it exactly as today.

### Scope

`WorkspaceScope` is `(tenant_id, key)`, taken from the request context the tools already run in.
`spec.workspace.scope` on the manifest picks the key:

| `scope` | Key | For |
|---|---|---|
| `thread` (default) | a hash of the tenant and the thread id | a conversation's files are its own |
| `tenant` | a fixed `shared` key | agents that should see one tenant-wide workspace |
| `deployment` | none: the whole root | the self-build manifests, whose root is a real checkout |

A tool called outside a thread (no `thread_id`) under `scope: thread` is refused, not quietly given a
shared directory. `deployment` holds every other scope, so it is honoured only for the tenants in
`FELIX_WORKSPACE_DEPLOYMENT_TENANTS` (default `default`) and refused for any other.

**As built (phase 2a, `felix/tools/workspace_scope.py`).** The `local` layout is
`<root>/.felix-scopes/<tenant>/<key>/`, not `<root>/<tenant>/<key>/` as first proposed: files written
before scopes sit at the root, and a reserved directory means none of them can be mistaken for a
tenant's. The thread key is a hash rather than the id held to a charset — a hash is a safe path
segment whatever the id holds, so no thread is refused for its name. Each scope directory is created
0700, walked by descriptor with no component a symlink. The scope is bound per call by the builder's
outermost wrapper (`apply_workspace_scope`), so an approval's preview reads the directory the call
will write; a call with no scope bound gets `thread`. `workspace_root()` resolves it, which is how
`shell`, the image tools and `publish_commits` are scoped with no change of their own, and the remote
shell runner is sent the scope's relative path and accepts only that shape. Instruction files
(`AGENTS.md`) are still read from the root, which only `deployment` can now write.

### Backends

| Backend | Where file I/O happens | Isolation | Use |
|---|---|---|---|
| `local` | in-process, under a scoped subdirectory | path containment only (today's, plus scoping) | development, tests, the self-build builder container |
| `hosted` | in a sandbox a hosted sandbox service runs, one per active scope | a separate VM per sandbox, on someone else's hardware; nothing shared with the host | **production** |
| `broker` | in a sandboxed container a local service starts on the same host | a user-space kernel per sandbox; the worker holds no socket | a fallback, only for a deployment that cannot use a hosted service |

**`hosted` is the production answer.** A hosted sandbox service runs each workspace in its own
isolated VM on the provider's infrastructure, reached over its API. The harness runs no sandbox
runtime and holds no container socket; it holds one provider credential, from the secrets backend,
and that credential never enters a sandbox. This also answers the question the other two backends
leave open: any worker on any host can reach a thread's sandbox by id, so durable runs keep working
when a fiber is claimed somewhere else.

#### What a provider must offer

The backend is written against a provider protocol, not a vendor. A provider is usable if it offers
all of this:

| Requirement | Why |
|---|---|
| A separate VM, or equivalent hardware-level isolation, per sandbox | a shared-kernel container per scope is the isolation `broker` gives; `hosted` exists to do better |
| File read, write (bytes), list and delete, or a command API to build them | the five tools |
| Command execution with a timeout | `search_files` now; code execution against the workspace later |
| Stop or pause that keeps the filesystem, and resume by id | a thread outlives any one run, and the next run may be on a different worker |
| Egress control, with networking off by default for a workspace | a workspace sandbox has no reason to reach the internet |
| An explicit delete, and listing by metadata | retention has to be enforceable, and orphans findable |
| A choice of where the data lives: region, in the operator's own cloud, or self-hosted | workspace contents leave the deployment, and some operators cannot allow that |
| An HTTP API or a Python SDK, and published limits and per-use pricing | the harness is Python; cost and concurrency have to be predictable |

**Example, checked against the provider's docs on 2026-09-24:** E2B meets every row. It gives each
sandbox its own VM, `files.read` and `files.write`, and `commands.run`. `pause()` keeps the
filesystem and memory with no expiry, and `Sandbox.connect(id)` resumes one. It offers
`allow_internet_access` plus allow and deny lists, an in-your-own-cloud deployment on AWS and Google
Cloud, and open-source infrastructure. It is kept here as a second data point for the requirements,
not as the choice.

#### First provider: Cloudflare Sandboxes (decided 2026-09-24)

The first adapter targets Cloudflare Sandboxes. The web tier (`felix-run/web`) already runs on
Cloudflare Workers, so workspace contents stay in an account the deployment already trusts, under
the same operator, instead of going to a new third party. Checked against the requirements on
2026-09-24, from Cloudflare's developer docs:

| Requirement | Cloudflare Sandboxes | Consequence for the design |
|---|---|---|
| VM isolation per sandbox | met — each sandbox runs in its own VM | none |
| File and command API | met — `writeFile`, `readFile`, `mkdir`, `exec` | `list_dir` and `search_files` are built on `exec` if no listing call fits |
| Keeps files across idle, resume by id | **not by itself** — a sandbox is a Durable Object plus a container, and the container's disk is fresh every time it starts | `/workspace` is persisted with `createBackup` / `restoreBackup` to an R2 bucket; see below |
| Egress control | met — `enableInternet`, and `allowedHosts` as a deny-by-default allowlist, plus `deniedHosts` | the adapter creates workspace sandboxes with the internet off |
| Delete, and list by metadata | **delete: met (rechecked 2026-10-07)** — `destroy()`; no listing | the reconcile sweep may have to work from the R2 backup objects and the mapping table rather than a provider listing; confirm before phase 4 |
| Where the data lives | met — the deployment's own Cloudflare account and R2 bucket | answers most of open question 3 for this provider |
| Callable from the harness | **not directly** — the SDK runs only inside a Worker (`getSandbox(env.Sandbox, id)`); there is no Python SDK or public API for it | a gateway Worker is required; see below |
| Status and price | **SDK 1.0 released 2026-09-30** (rechecked 2026-10-07; 0.x fixes end 2026-12-31); Workers Paid plan | `hosted` stays opt-in until the adapter has run on the reference deployment for a release |

**The gateway Worker.** A small Worker — in this repository, `deploy/cloudflare/workspace-gateway`,
as decided on 2026-10-07 (see "Phase 3 as decided") — holds the
Sandbox Durable Object binding and the backup bucket and exposes exactly the `SandboxProvider`
operations over HTTPS. The harness's adapter is an HTTP client to it, authenticated by a dedicated
secret (not the chat-ui key, not a harness API key). The gateway takes a scope key and never a raw
sandbox id from the caller, derives the sandbox id from `(tenant_id, scope_key)` itself, and refuses
anything else, so a leaked gateway secret still cannot name a sandbox outside the scope scheme.

**Persistence, concretely.** Because the disk does not survive sleep, the adapter treats every
sandbox as disposable and `/workspace` as the thing that persists:

- On a scope's first use in a sandbox that has just started, restore the latest backup, if any.
- After every successful write or edit, and before the sandbox is allowed to sleep, create a
  backup and store its handle in the mapping row. A write is reported to the model only once its
  backup exists, so an acknowledged write cannot be lost to a sleep.
- Backups expire after three days by default, and an expired object stays in R2 until something
  deletes it. The adapter sets the TTL from `FELIX_WORKSPACE_RETENTION_DAYS`, refreshes it on use,
  and the bucket gets a lifecycle rule, so the retention sweep and R2 agree on what is kept.
- Backing up after each write costs a round trip per write. Measure it against a typical cowork
  turn (open question 5) and batch to one backup per turn if it is too slow — the lease guarantees
  one writer per scope, so a turn is a safe unit.

#### Provider adapters

`hosted` is one backend over a small `SandboxProvider` protocol — create (with scope metadata, an
image or template, and network off), connect or resume by id, pause, delete, list by metadata, the
file operations, and run a command. Each provider is an adapter module, chosen by
`FELIX_WORKSPACE_PROVIDER`. A shared conformance suite runs against every adapter: in CI against an
in-memory fake, and against the real provider in an opt-in job, the same split the database
conformance suite uses.

#### Mapping scopes to sandboxes

A table `workspace_sandboxes(tenant_id, scope_key, provider, sandbox_id, state, backup_ref,
created_at, last_used_at)` — `backup_ref` holding the latest backup handle for providers, like the
first one, whose sandboxes do not keep their disk — unique on `(tenant_id, scope_key)`, tenant-first
indexed and row-level-secured like every other tenant table, records which sandbox holds which
scope:

- **First use** of a scope creates the sandbox and inserts the row.
- **Later calls**, from any worker, connect to or resume it by id.
- **Idle** past `FELIX_WORKSPACE_IDLE_SECONDS`, the provider pauses or stops it, keeping the files.
- **Deleted** with its thread, or by the retention sweep: the provider deletes it, then the row goes.

Some providers keep a paused sandbox indefinitely and bill for its storage, so deletion is the
harness's job and never assumed. A reconcile sweep lists the provider's sandboxes by the
deployment's metadata and deletes any with no row — an orphan is a cost leak and a copy of a
tenant's files nobody is tracking.

#### Operations inside a sandbox

- **Containment is enforced twice.** The harness normalises the path against the sandbox's
  workspace directory before calling, so a traversal is refused with the same message as today, and
  the sandbox boundary holds regardless.
- **`edit_file` stays byte-exact.** The harness reads the bytes, applies the exact-match replacement
  itself, and writes the bytes back. It relies on one writer per scope at a time, which the session
  lease already gives a thread.
- **`search_files` runs a bounded `grep` inside the sandbox**, with the existing pattern, line and
  wall-clock limits applied as command arguments and a command timeout.
- **Nothing from the harness's environment is passed in.** A sandbox starts from a minimal image with
  an empty environment.

#### `broker`, if hosted is ruled out

For a deployment that cannot send workspace contents to any third party and cannot run a provider in
its own cloud. The shape is the point: the obvious version — giving the worker the Docker socket to
start a container per scope — is worse than today, because that socket is root on the host. Instead a
small broker service alone can create sandboxes, and exposes only the file operations over a local,
authenticated RPC. Each scope gets a container on a user-space kernel runtime (gVisor's `runsc`, or
rootless Podman), with no network, the existing snippet rung's limits, and a byte quota. This is
where the roadmap's gVisor item would land.

### Failures, and what the audit log sees

Every backend returns failures with a registered prefix — `[workspace unavailable]`,
`[workspace quota]`, `[workspace denied]` — added to `FAILURE_CONTENT_PREFIXES`, so a failed call is a
failed row. Provider errors — rate limits, a sandbox that cannot be resumed, a timeout — map onto
those prefixes, and the provider's own message goes into the audit payload, not the model's context.
Independently of this proposal, the existing `error: …` returns in `tools/workspace.py` should gain a
registered prefix now: today a write that fails with `Errno 13` is audited as `ok`.

`hosted` and `broker` fail closed. If the provider or the broker is down, the call is refused with
`[workspace unavailable]`; the harness never falls back to `local`.

### Lifecycle and retention

- **Create** on first use of a scope. **Pause** an idle sandbox after `FELIX_WORKSPACE_IDLE_SECONDS`.
- **Delete** a thread's workspace when the thread is deleted, and on a retention sweep
  (`FELIX_WORKSPACE_RETENTION_DAYS`, off by default, like approval retention). Deletion goes through
  the provider's delete call, then the mapping row.
- **Reconcile** on the same sweep: provider sandboxes with no mapping row are deleted.
- **Quota** per scope (`FELIX_WORKSPACE_MAX_BYTES`), enforced by the backend, not by the tool.
- **Export** a scope as an archive through a management route, so an operator can see what an agent
  wrote without going to the provider's console — the same reason `/memory` and `/documents` have
  routes.

## Phase 3 as decided (2026-10-07)

Shaped after a recheck of the provider against **Sandbox SDK 1.0**, which was released on
2026-09-30 and changes the API the sections above were written against. Where this section and
those disagree, this one is current.

**What changed in the provider.**
- **No more `getSandbox`.** In 1.0 the gateway writes its own Durable Object, which drives
  `this.ctx.container` (`start`, `exec(argv)`, `destroy`), and the Worker reaches it with
  `getByName`. `createBackup`/`restoreBackup` give way to `DirectoryBackup`: one directory, kept as
  one R2 object, restored as plain files, with no R2 credential inside the container.
- **`destroy()` exists.** Snapshots, though, cannot be deleted at all, so this design does not use
  them.
- **Each instance is a Firecracker microVM** with its own kernel. Disk does not survive a stop. Idle
  instances are not billed. Cold starts are typically 1–3 s.
- **The file API follows symlinks and has no ranged read.**

**Decisions.**
- **Files, then shell.**
  - **3a** puts the five file tools on the hosted backend.
  - **3b** runs `shell_tools` inside the same sandbox through `exec`. The harness still applies the
    allowlist and command screening first, and a hosted scope never falls back to the host.
- **Parity through a helper.** The sandbox image carries a small `felix-fs` helper, run through
  `exec`. It walks with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)` and gives ranged reads,
  the size caps, edit by rename and the bounded search. One conformance suite then runs against
  `local` and `hosted` alike.
- **Backups at the end of each run, not each write.**
  - **When:** `DirectoryBackup` to R2 when a run that wrote files ends, and again before the idle
    timeout stops the sandbox.
  - **Restore:** on wake.
  - **The cost:** a VM lost mid-run loses that run's writes. This replaces "a backup after every
    write" above.
- **SDK 1.0, the gateway's own Durable Object.**
  - The Durable Object is named from `(tenant_id, scope_key)`, which the Worker derives and
    validates.
  - Containers start with `enableInternet: false` on the smallest instance type.
  - There is a 10-minute idle timeout and a `/health` route.
- **No `workspace_sandboxes` table in phase 3.** The Durable Object name is deterministic, which
  replaces the mapping table above. The R2 prefix is what phase 4's sweeps reconcile against.
- **One sandbox per `tenant` scope**, shared by that tenant's threads.
- **Local files move once.** On a scope's first hosted use, its local files are uploaded once and
  the upload is recorded. *Not built yet* (2026-10-07): until it is, a scope starts empty on
  `hosted`, which `UPGRADING.md` says.
- **Some tools still need a local directory:** an image tool's `path`, `publish_commits`, AGENTS.md
  loading and a thread's repository checkout. Under `hosted`, these are refused for any scope but
  `deployment`, never served from the host.

- **The gateway lives in this repository** (`deploy/cloudflare/workspace-gateway`), not in
  `felix-run/web`. It is harness infrastructure, like the database: an operator running Felix
  headless, with no chat-ui, deploys it from here. This also puts the `felix-fs` helper beside the
  code it is ported from. A test compares the two as syntax trees, so they cannot drift.

**Pull requests, in order, all in this repository.**
1. The gateway Worker (`deploy/cloudflare/workspace-gateway`), its image and the `felix-fs` helper,
   the five file operations, its handler contract tests, the helper's tests in the harness suite,
   and `wrangler.example.jsonc`.
2. Persistence. `DirectoryBackup`, restore on start, a checkpoint route, a
   backup-then-stop alarm on idle, and `destroy`.
3. `HostedBackend` over the gateway, a fake gateway for tests, the backend conformance
   suite run against both backends, the `FELIX_WORKSPACE_BACKEND` opt-in, and the end-of-run
   checkpoint.
4. 3b, `shell_tools` exec through the hosted backend. Also UPGRADING notes and an opt-in
   live conformance job.
5. Latency and cost measured on the reference deployment, and this file brought in line.

## Phases

| Phase | What | Changes behaviour? | Status |
|---|---|---|---|
| 0 | Stop defaulting the workspace to the checkout: `compose.yml` mounts a named `felix-workspace` volume, initialised to the image's uid, instead of `./workspace` | fresh deployments; existing ones on the old default see an empty workspace (`UPGRADING.md`) | `[x]` fix/workspace-default-volume; the reference host set `FELIX_WORKSPACE_HOST=/srv/felix/workspace` by hand first |
| 1 | Register a failure prefix for workspace tool errors | audit rows become truthful | `[x]` #308 — every failure goes through `tool_error_output` |
| 2a | `spec.workspace.scope` (default `thread`) through `workspace_root()`, the `deployment` scope gated to the operator's tenants, `felix workspace migrate` | yes — see migration | `[x]` feat/workspace-scopes |
| 2b | `WorkspaceBackend` seam with the `local` backend: the tools stop touching the filesystem directly | no | `[x]` refactor/workspace-backend |
| 3a | `hosted` backend for the five file tools: the gateway Worker in `deploy/cloudflare/` (SDK 1.0, `felix-fs` helper, R2 `DirectoryBackup`), `HostedBackend`, the conformance suite over both backends | opt-in via `FELIX_WORKSPACE_BACKEND=hosted` | `[x]` #508 gateway, #509 persistence, feat/hosted-workspace-backend; not yet run against a deployed gateway |
| 3b | `shell_tools` exec inside the scope's sandbox | opt-in, with 3a | `[x]` feat/hosted-shell: the shell tool's own exec path (`exec_argv`, ported and held to it) runs in the sandbox |
| 4 | Retention and reconcile sweeps, and the export route | opt-in | `[ ]` |
| 5 | `broker` backend, only if a deployment needs one | opt-in | `[ ]` |

Phases 0 and 1 are small and independent and should land first. Phase 2 was split: 2a delivers the
isolation, through the one function every workspace consumer already calls, and 2b is where the
tools stop touching the filesystem directly — a refactor with no change in behaviour, ahead of the
backend that needs it. Phase 3 is the production change; its provider is chosen
(Cloudflare Sandboxes), and all of it is in this repository: the adapter and the gateway Worker,
under `deploy/cloudflare/`.

## Migration

Phase 2 changes what an existing deployment sees, because files written before it live at the root
and a `scope: thread` workspace starts empty. So:

- The first release with phase 2a leaves files at the root where they are, visible only to the
  `deployment` scope, and `felix workspace migrate` moves them into the tenant `default`'s `shared`
  scope once (`UPGRADING.md`). The bundled manifests that rely on the root — `contributor` and
  `triage` — declare `scope: deployment`; `cowork` stays on `thread`.
- Moving to `hosted` uploads an existing scope's files into its new sandbox on first use, once, and
  records that it did.
- `UPGRADING.md` gets a section for each, and `hosted` stays opt-in until it has run on the reference
  deployment for a release.

## Open questions

1. **Default scope.** `thread` is the safer default and the one that matches how a conversation
   reads; `tenant` matches today's behaviour. This proposes `thread`, with the bundled `cowork`
   manifest staying on `thread`.
2. **Which provider first — decided: Cloudflare Sandboxes.** Recorded 2026-09-24; see
   "First provider" above. What is still open about it: a documented delete call, the cost of a
   backup per write, and when the SDK leaves preview.
3. **Workspace contents leave the deployment.** With a hosted provider, files an agent writes are
   stored by a third party. That is a data-handling decision for each operator, which is why the
   requirements include running the provider in the operator's own cloud, and why `broker` remains.
   With Cloudflare Sandboxes the files stay in the deployment's own Cloudflare account and R2 bucket.
4. **Cost.** One sandbox per thread, paused when idle, adds up with the number of threads. The
   retention default, the idle timeout and whether `scope: tenant` suits some manifests better all
   follow from the chosen provider's pricing.
5. **Latency.** Every file operation becomes a network round trip. Measure a typical cowork turn
   before and after, and batch writes where the provider supports it.
6. **Reading a workspace from chat-ui.** The web client's "Touched this session" list is derived from
   tool arguments today. The export route in phase 4 is the natural source for a real file list, and
   the client should wait for it rather than invent one.

## Review checklist for each phase

- The worker and API processes hold no sandbox runtime and no container socket.
- The provider credential is held only by the harness and never enters a sandbox; no harness
  environment variable is visible inside one.
- A path, a symlink, or a thread id cannot name another scope; tests cover each.
- Approval gating and grant reuse behave identically on every backend (the conformance suite runs
  per backend and per adapter).
- Every refusal is audited as a failure, and every sandbox the provider holds has a mapping row.
