# Workspace gateway

The Worker the harness's **hosted** workspace backend reaches each workspace scope's sandbox
through (`docs/WORKSPACE.md`, phase 3). Off unless the harness sets `FELIX_WORKSPACE_BACKEND=hosted`;
a deployment that never does needs none of this.

```
harness ──HTTPS, bearer──▶ gateway Worker ──getByName(tenant/key)──▶ Durable Object ──exec──▶ Container
                                                                                            (felix-fs helper)
```

- **One sandbox per scope.** A scope is `tenant/key`: `key` is `shared` (a tenant's shared scope)
  or the 40-hex hash of a thread. The Worker derives the Durable Object's name from the two and
  takes no sandbox id from the caller, so a leaked token reaches nothing outside the scheme.
- **The sandbox holds nothing of ours.** Each Container is a Firecracker microVM started with the
  internet off and an empty environment; this Worker's secrets never enter it.
- **The file rules are the harness's.** `helper/felix_fs.py` runs inside the sandbox once per
  operation and walks every path with the local backend's code — no symlink followed, ranged
  reads, edit by rename. `tests/unit/test_workspace_gateway_helper.py` compares that code with its
  source in `felix/tools/` as syntax trees, so the two cannot drift silently.
- **`/workspace` lives in R2 between Containers.** A Container's disk does not survive it stopping
  (10 idle minutes), so:
  - a started Container gets the scope's latest backup restored before any operation reaches it,
    and a restore that fails refuses the call rather than serving the scope empty;
  - `checkpoint` backs it up (the harness calls it when a run that wrote files ends), and an alarm
    backs up a scope written since its last backup shortly before the idle stop;
  - `destroy` stops the Container and deletes the backup.

  Backups use `DirectoryBackup` from `@cloudflare/sandbox`: one object per backup, written by the
  Container through a grant for that object alone, so it holds no R2 credential.

## Wire contract

`POST /v1/workspaces/{tenant}/{key}/{op}` with `Authorization: Bearer <WORKSPACE_GATEWAY_TOKEN>`,
`op` one of `prepare`, `list`, `read`, `write`, `edit`, `search` (the file operations), `delete` and
`rename` (the operator's file pane: each compares the file's digest when sent `expected_sha256` and
refuses `409 workspace_changed` with the file's `sha256` and `bytes` now; a rename never replaces
anything, `409 target_exists`), `delete_folder` and `rename_folder` (the pane's folders: the tree is
walked first with no link followed, and refused before anything changes when it holds a reserved
name, `422 reserved_path`, or more than 2,000 entries, `409 too_many_entries` with `count`; a delete
sent `expected_count` refuses `409 workspace_changed` with the folder's file `count` now, and removes
a symlink inside as the link, never its target; a rename never replaces anything and never moves a
folder into itself), `exec` (a
`shell_tools` command, run by the shell tool's own exec path in the sandbox), `clone` (a thread's
repository, into its empty `/workspace`, through the `github.com` intercept in `src/github.ts`,
which adds the person's token outside the container and allows only that one repository's fetch),
`git` and `lstat` (read-only, for the harness's repository listing and `publish_commits`: git run
by the harness's own `_git_exec`, ported into the helper), `checkpoint` and `destroy`. Answers `{"result": {...}}` or
`{"error": CODE, "message": TEXT}`; `src/protocol.ts` has the shapes and codes. `GET /health`
needs no credential.

## Deploy

```bash
cp wrangler.example.jsonc wrangler.jsonc        # gitignored
npm ci
npx wrangler r2 bucket create felix-workspaces
openssl rand -hex 32 | npx wrangler secret put WORKSPACE_GATEWAY_TOKEN
npx wrangler deploy                             # builds the image: needs Docker
```

The same token goes to the harness as `FELIX_WORKSPACE_GATEWAY_TOKEN`, with the Worker's URL.

**Upgrading: the gateway first, then the harness.** A new harness operation is a new `op` here, and
the helper that runs it ships in the image. A harness ahead of its gateway gets `404` for the new
op, which it reports as the workspace being unavailable (`503 workspace_unavailable` on the file
pane's routes) and nothing else; a gateway ahead of its harness only has an op nobody calls. The
pane's `delete_folder` and `rename_folder` (2026-10-10) are such ops: redeploy this Worker
(`npx wrangler deploy`, which rebuilds the image with the new helper) before the harness that
calls them.

`WORKSPACE_INSTANCE` (a plain var) sizes each sandbox: `standard-1` by default (1/2 vCPU, 4 GiB,
8 GB disk), or `lite`, `standard-2`, `standard-3`, `standard-4`. Anything else and the gateway
serves nobody (`503 misconfigured`). `lite` (1/16 vCPU, 256 MiB, 2 GB) is about a tenth of the
price while awake, but every operation starts a process, and at 1/16 vCPU that takes seconds: a
repository listing took 15 s on it against under 1 s on `standard-1`, and git or a package install
can run out of memory. CPU is billed on use; memory and disk as provisioned while a sandbox is awake.

## Develop

```bash
npm run check-types
npm test                    # the handler against a fake Durable Object namespace
npm run types               # after changing wrangler.example.jsonc: regenerates runtime types
npx wrangler dev            # a real Container locally: needs Docker
```

The helper's tests run with the harness suite: `./scripts/test.sh tests/unit/test_workspace_gateway_helper.py`.
