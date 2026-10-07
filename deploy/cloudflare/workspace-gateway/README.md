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
- **Not yet persistent.** A Container's disk does not survive it stopping (10 idle minutes).
  Backing `/workspace` up to R2 at the end of each run and restoring it on start is the next change.

## Wire contract

`POST /v1/workspaces/{tenant}/{key}/{op}` with `Authorization: Bearer <WORKSPACE_GATEWAY_TOKEN>`,
`op` one of `prepare`, `list`, `read`, `write`, `edit`, `search`. Answers `{"result": {...}}` or
`{"error": CODE, "message": TEXT}`; `src/protocol.ts` has the shapes and codes. `GET /health`
needs no credential.

## Deploy

```bash
cp wrangler.example.jsonc wrangler.jsonc        # gitignored
npm ci
openssl rand -hex 32 | npx wrangler secret put WORKSPACE_GATEWAY_TOKEN
npx wrangler deploy                             # builds the image: needs Docker
```

The same token goes to the harness as `FELIX_WORKSPACE_GATEWAY_TOKEN`, with the Worker's URL.

## Develop

```bash
npm run check-types
npm test                    # the handler against a fake Durable Object namespace
npm run types               # after changing wrangler.example.jsonc: regenerates runtime types
npx wrangler dev            # a real Container locally: needs Docker
```

The helper's tests run with the harness suite: `./scripts/test.sh tests/unit/test_workspace_gateway_helper.py`.
