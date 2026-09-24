# Workspaces that are not the server

**Status: proposal, 2026-09-24.** Nothing below is built yet except the configuration change in
phase 0. This file is the design the workspace tools are to be moved onto; it is updated in place as
each phase lands, like [SELF.md](SELF.md).

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
- **Arbitrary code execution in the workspace.** This proposal isolates file operations. Letting a
  model run commands against its workspace is what the isolation later makes safe to build, not
  something this delivers.

## Design

### One seam: `WorkspaceBackend`

Every workspace tool already starts the same way — `root = workspace_root()`, then
`resolve_under_root(root, args.path)`. That pair becomes one call into a backend chosen by
`FELIX_WORKSPACE_BACKEND`:

```python
class WorkspaceBackend(Protocol):
    async def list_dir(self, scope: WorkspaceScope, path: str) -> ListResult: ...
    async def read_file(self, scope: WorkspaceScope, path: str, offset: int, limit: int) -> bytes: ...
    async def write_file(self, scope: WorkspaceScope, path: str, data: bytes, append: bool) -> WriteResult: ...
    async def edit_file(self, scope: WorkspaceScope, path: str, old: str, new: str, replace_all: bool) -> EditResult: ...
    async def search(self, scope: WorkspaceScope, path: str, query: str, regex: bool, max_hits: int) -> SearchResult: ...
```

The tools keep their argument models, limits and messages; they stop touching the filesystem.
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
| `thread` (default) | the thread id | a conversation's files are its own |
| `tenant` | a fixed `shared` key | agents that should see one tenant-wide workspace |

A tool called outside a thread (no `thread_id`) under `scope: thread` is refused, not quietly given a
shared directory. The `local` backend lays scopes out as `<root>/<tenant>/<key>/`; tenant ids are
already held to `[A-Za-z0-9._-]+`; a thread suffix is only guaranteed free of `:` and `#`, so the
backend holds the key to the same charset before using it as a path segment, and refuses one that is
not.

### Backends

| Backend | Where file I/O happens | Isolation | Use |
|---|---|---|---|
| `local` | in-process, under a scoped subdirectory | path containment only (today's, plus scoping) | development, tests, the self-build builder container |
| `broker` | a separate workspace service on the same host, one sandboxed container per active scope | process, filesystem and network isolation; the worker holds no socket | single-VM deployments, including the reference one |
| `remote` | a sandbox service reached over HTTPS | nothing shared with the host | multi-host deployments, or operators who do not want sandboxes on the API host |

**`broker` is the one that answers the production question, and the shape of it is the point.** The
obvious implementation — hand the worker the Docker socket so it can start a container per scope — is
worse than today: that socket is root on the host, so a worker compromised through a prompt would own
the VM. Instead:

- A small **workspace broker** runs as its own service. It alone can create sandboxes, and it exposes
  exactly the five operations above over a local, authenticated RPC (a Unix socket or loopback HTTP
  with a per-deployment token). The API and worker get that endpoint and nothing else.
- Each active scope gets a sandbox with its own volume, a **user-space kernel runtime** (gVisor's
  `runsc`, or rootless Podman where that is unavailable), no network, the existing rung's limits
  (non-root user, pids, CPU, memory), and a byte quota on the volume. Nothing from the harness's
  environment is passed in.
- Idle sandboxes are stopped after a TTL and their volumes kept; a volume is deleted by the retention
  rules below, never by idleness.

This is also where the roadmap's "Sandbox ladder extras — gVisor" item lands: not as an extra on the
snippet rung, but as the runtime the workspace broker requires.

**`remote`** speaks the same five operations to an external sandbox service. It trades latency and a
network dependency for zero host exposure. It is a later phase and a thin client once the broker's RPC
exists, because the contract is the same.

### Failures, and what the audit log sees

Every backend returns failures with a registered prefix — `[workspace unavailable]`,
`[workspace quota]`, `[workspace denied]` — added to `FAILURE_CONTENT_PREFIXES`, so a failed call is a
failed row. Independently of this proposal, the existing `error: …` returns in `tools/workspace.py`
should gain a registered prefix now: today a write that fails with `Errno 13` is audited as `ok`.

`broker` and `remote` fail closed. If the service is down, the call is refused with
`[workspace unavailable]`; the harness never falls back to `local`.

### Lifecycle and retention

- **Create** on first use of a scope. **Stop** an idle sandbox after `FELIX_WORKSPACE_IDLE_SECONDS`.
- **Delete** a thread's workspace when the thread is deleted, and on a retention sweep
  (`FELIX_WORKSPACE_RETENTION_DAYS`, off by default, like approval retention).
- **Quota** per scope (`FELIX_WORKSPACE_MAX_BYTES`), enforced by the backend, not by the tool.
- **Export** a scope as an archive through a management route, so an operator can see what an agent
  wrote without shelling into the host — the same reason `/memory` and `/documents` have routes.

## Phases

| Phase | What | Changes behaviour? | Status |
|---|---|---|---|
| 0 | Stop defaulting the workspace to the checkout: `compose.yml` mounts a named `felix-workspace` volume, initialised to the image's uid, instead of `./workspace` | fresh deployments only | `[ ]` in the repo; done by hand on the reference host 2026-09-24 (`FELIX_WORKSPACE_HOST=/srv/felix/workspace`) |
| 1 | Register a failure prefix for workspace tool errors | audit rows become truthful | `[ ]` |
| 2 | `WorkspaceBackend` seam with the `local` backend, plus `spec.workspace.scope` (default `thread`) | yes — see migration | `[ ]` |
| 3 | `broker` backend with a user-space-kernel runtime, quotas, idle stop | opt-in via `FELIX_WORKSPACE_BACKEND=broker` | `[ ]` |
| 4 | Retention sweep and the export route | opt-in | `[ ]` |
| 5 | `remote` backend | opt-in | `[ ]` |

Phases 0 and 1 are small and independent and should land first. Phase 2 is where the tools stop
touching the filesystem directly and is the one that needs the most review.

## Migration

Phase 2 changes what an existing deployment sees, because files written before it live at the root
and a `scope: thread` workspace starts empty. So:

- The first release with phase 2 treats files at the root as the tenant `default`'s `shared` scope,
  and bundled manifests that relied on a shared directory declare `scope: tenant` explicitly.
- `UPGRADING.md` gets a section saying so, and how to move an existing directory into a scope.
- `broker` is opt-in until it has run on the reference deployment for a release.

## Open questions

1. **Default scope.** `thread` is the safer default and the one that matches how a conversation
   reads; `tenant` matches today's behaviour. This proposes `thread`, with the bundled `cowork`
   manifest staying on `thread`.
2. **Where the broker's runtime comes from.** gVisor needs a kernel-compatible host and a
   daemon configured to offer `runsc`; rootless Podman is easier to install and isolates less. The
   reference GCE VM should be checked for both before phase 3 is scheduled.
3. **Durable-run affinity.** A fiber can be claimed by any worker; with `broker` on one host that is
   fine, but a multi-host deployment needs `remote`, or scope-to-host affinity. `remote` is the
   simpler answer and is why it is in the table.
4. **Reading a workspace from chat-ui.** The web client's "Touched this session" list is derived from
   tool arguments today. The export route in phase 4 is the natural source for a real file list, and
   the client should wait for it rather than invent one.

## Review checklist for each phase

- The worker and API processes have no path to the sandbox runtime beyond the broker endpoint.
- No harness environment variable is visible inside a sandbox.
- A path, a symlink, or a thread id cannot name another scope; tests cover each.
- Approval gating and grant reuse behave identically on every backend (the conformance suite runs
  per backend).
- Every refusal is audited as a failure.
