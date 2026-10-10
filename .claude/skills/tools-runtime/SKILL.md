---
name: tools-runtime
description: How a Felix tool runs once it is bound — the Tool dataclass and executors, the transport label and the trust allowlist that decides content screening, the ToolProvider and builtins, and each tool family's runtime rules (shell and the shell runner, workspace scopes and the local/hosted backends, http_fetch and web_search egress, browser, sandboxes and containers, queues, client tools, outbound MCP, document search, GitHub publish and per-thread repo checkouts, image tools, artifact spill, attachments, workspace notes). Use when adding or changing a tool or a tool family, touching felix/tools/, felix/mcp/, felix/repos/, felix/documents/, artifacts.py, attachments.py, shell_runner.py or workspace_notes.py, or tracing why a tool refused, timed out, or was or was not screened.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*)
metadata:
  covers: felix/tools/, felix/mcp/, felix/repos/, felix/documents/, felix/artifacts.py, felix/attachments.py, felix/shell_runner.py, felix/workspace_notes.py, felix/ui/
---

# The tool runtime

The governance-pipeline skill covers how a manifest becomes a wrapped tool list. This one covers
what is inside each tool: how it is defined, what its executor reaches, and the rule each family
must not break.

## From spec to executor

- A `Tool` (`tools/types.py:Tool`) is a dataclass: name, schema, `executor`, plus `source`,
  `fatal`, `replay_safe`, `prompt_guidance`, `approval_preview`, `approval_binding` and
  `relays_untrusted`. The executor has a `transport` string and `execute(args, ctx)`.
- `tools/types.py:define_tool` wraps a handler (pydantic `args` validated first; a bad argument
  returns `[invalid args for …]`, not an exception). `define_tool_with_executor` takes a
  hand-written executor class — that is how every outbound family is built.
- `tools/executor.py:wrap_tool` copies a tool with `dataclasses.replace`. Never rebuild a `Tool`
  field by field: a forgotten field silently resets on every wrapped tool (`replay_safe` once
  read `False` everywhere for that reason). Handler arity is decided once by
  `tools/types.py:accepts_positional`; see the plugin-seam skill on `functools.wraps`.
- **Where binding happens.** Names in `spec.tools` resolve through the `ToolProvider`
  (`tools/provider.py:InMemoryToolProvider`), filled by `tools/builtins.py:register_builtin_tools`
  (calculator, the five workspace tools, `ask_user`, skill stubs) and plugin tools. The API builds
  it in `composition.py:compose`; fibers, cron, eval and the CLI use
  `tools/builtins.py:default_tool_provider`, which must stay equivalent. Every other family is
  bound inside `manifests/builder.py:build_agent` from its `spec.*` list via a `tools_from_*`
  factory in this package, lazily imported and wrapped in `try/except` that logs and continues.
  **Shell is the exception**: an operator-disallowed prefix raises `GovernanceError` and fails the
  compile. The governance wrappers are applied afterwards; their order lives in the
  governance-pipeline skill.
- `allow_http` reaching every outbound binder is true only with `FELIX_ENVIRONMENT=development`
  and `FELIX_ALLOW_INSECURE`.
- Failures are values: return `tools/errors.py:tool_error_output` with a `ToolErrorCode`. A plain
  `error: …` string reads as a success to `after_tool`, eval and audit unless it starts with one
  of `tools/types.py:FAILURE_CONTENT_PREFIXES`.

## Transport and trust

`manifests/builder.py:_TRUSTED_TRANSPORTS` is `frozenset({"local"})`. `_is_untrusted_tool`
treats every other transport as untrusted, and also any `local` tool whose `source` starts with
one of `_UNTRUSTED_SOURCE_PREFIXES`. Trust is an allowlist because `transport` is an open string
a plugin can mint: a denylist failed open for `http` and `client` before. Untrusted tools are the
ones content screening covers. So:

- Anything that does not execute in-process gets its own transport label, never `local`.
- Set `source` as well; it is the second line of defence and the spill/reader key off it.
- Do not add to `_TRUSTED_TRANSPORTS` to quiet a screening cost. Pinned by
  `tests/unit/test_tool_trust_boundary.py`.

## Families and the rule each one keeps

| Family | Files | Non-obvious rule |
|---|---|---|
| Workspace (`list_dir`, `read_file`, `write_file`, `edit_file`, `search_files`) | `tools/workspace.py`, `tools/workspace_backend.py`, `tools/workspace_local.py`, `tools/workspace_hosted.py`, `tools/workspace_scope.py` | Every path goes through `tools/workspace.py:workspace_root` (or the backend's `scope_root`): a thread's repo checkout if it has one, else the `spec.workspace.scope` directory (`thread` default, `tenant`, or `deployment` only for `FELIX_WORKSPACE_DEPLOYMENT_TENANTS`). The scope is a context var bound by `manifests/builder.py:apply_workspace_scope`; unbound means `thread`. Local walks use descriptors and never follow a symlink. `FELIX_WORKSPACE_BACKEND=hosted` sends the five tools to the gateway in `deploy/cloudflare/workspace-gateway` (`docs/WORKSPACE.md`); there, image `path` and `publish_commits` are refused for any scope but `deployment`. |
| Shell (`spec.shell_tools`) | `tools/shell.py`, `shell_runner.py`, `security/shell_policy.py` | argv is exec'd, never a shell. Allowed = manifest prefix inside `FELIX_SHELL_ALLOWED_COMMANDS`, checked at write, at compile (`assert_shell_commands_allowed`) and per call (`assert_argv_allowed`, against the request's settings). Local exec only where `security/shell_policy.py:local_exec_allowed` (development and `FELIX_AUTH_MODE=none`); otherwise it needs `FELIX_SHELL_RUNNER_URL` and never falls back. Under `hosted` it runs in the scope's sandbox. The runner (`felix-shell-runner`) re-checks with its own settings and shares `tools/shell.py:exec_argv`. A thread checkout cannot use the remote runner. |
| HTTP fetch (`spec.http_tools`) | `tools/http_fetch.py` | The model picks the URL. Needs `path_prefix` or explicit `allow_any_host`. Redirects are walked by hand (`MAX_REDIRECTS`), each hop re-checked against the prefix with `_within_prefix` (origin comparison, not `startswith`); egress goes through `security/egress.py:safe_async_client`. Every refusal is the same `egress_blocked` line so the model cannot probe internal addressing. Transport `http`, not replay-safe. |
| Web search (`spec.search_tools`) | `tools/web_search.py`, `felix/search.py` | Operator endpoint (`FELIX_SEARCH_BACKEND`, `FELIX_SEARCH_URL`); bound even when unset, returning not-configured rather than vanishing. Transport `search`. |
| Browser (`spec.browser_tools`) | `tools/browser.py` | Playwright extra. Every page request, redirects and subresources included, is re-checked by a route guard; hostnames are pattern-checked before they reach Chromium's `--host-resolver-rules`. |
| Sandbox / container (`spec.sandboxes`, `spec.containers`) | `tools/sandboxes.py`, `tools/transports.py` | Images must be in `FELIX_SANDBOX_ALLOWED_IMAGES`. The Docker run is confined (no network, non-root, read-only, caps dropped) and runs via `asyncio.to_thread` so the timeout can fire. Container gateways are checked at bind and dialled through `safe_async_client`. |
| Queues (`spec.queues`) | `tools/queues.py` | Redis list per tenant and binding, in-process deque under `memory://`; tenant comes from the request context. |
| Client tools (`spec.client_tools`) | `tools/client_bridge.py`, `tools/client_requests.py` | The executor records the request (so a durable run's stream can announce it), emits `tool_request`, then waits on a waiter until `POST /chat/tool_result` answers or the timeout (default 120 s) ends it. `tool_call_id` over 256 chars is refused at once. A client-reported `error` stays a tool error. |
| Outbound MCP (`spec.mcp_servers`) | `mcp/client.py`, `mcp/stdio.py` | Bound as `server__tool` (`mcp/client.py:_bind_remote_tool`), source `mcp:<server>`. A server's `tools:` allowlist accepts both bare and prefixed names. Discovery is cached for `DISCOVERY_TTL_S`; failures are not cached. Stdio needs `FELIX_MCP_STDIO_ALLOWED_COMMANDS`, re-checked at spawn in `mcp/stdio.py:stdio_rpc`, with a scrubbed child env. `mcp/server.py` is the inbound surface, not part of this. |
| Document search (`spec.document_tools`) | `tools/document_search.py`, `documents/store.py` | Tenant fixed at bind; hybrid lexical + vector, vector skipped (not faked) without an embedder. Says "empty corpus" apart from "no match". Transport `documents`. |
| GitHub publish (`spec.github_publish`) | `tools/github_publish.py`, `repos/checkouts.py` | `publish_commits` takes a branch and `head_sha`, not file contents; the token lives only in its HTTP client, git runs with an empty environment. `auth: person` binds `tool_from_thread_publish`, resolved per call against the thread's checkout. Checkouts sit outside `FELIX_WORKSPACE_ROOT` (`repos/checkouts.py:checkout_root` refuses nesting), are size-capped and swept by `sweep_expired`. |
| Image tools (`spec.image_tools`) | `tools/image_tools.py`, `tools/tool_images.py` | Only images in this thread can be named; workspace `path` only with `allow_path`. Images a tool returns are stored as attachments by `tools/tool_images.py:store_tool_images` (called from `patterns/tool_runner.py`), inline bytes only, per-call and per-run caps. |

`ask_user` is a builtin whose handler lives in `ui/ask_user.py`: it puts a select, confirm or input
prompt to the person watching the run through `ui/prompts.py:request_ui`, over the stream.

Not tool families despite the names: `tools/retrieval.py` and `tools/decider_retrieval.py` pick
which *tools* the model sees each turn (`spec.tools_retrieval`, optionally ranked by the decider),
called from `patterns/react.py`.

## Data the tools hand around

- **Artifact spill** — `artifacts.py:apply_artifact_spill` replaces an oversized output with a
  preview and a trailing `[artifact:<id> …]` marker clients parse; keep that marker last. The model
  reads it back with `read_artifact`, bound by `manifests/builder.py:_bind_artifact_reader` before
  the governance stack, and limited to artifacts *this conversation* spilled (an owner object beside
  each spill). The reader is exempt from spilling by its `source`. Ledger rows age out after
  `FELIX_ARTIFACT_RETENTION_DAYS`.
- **Attachments** — uploads are stored once and referenced by id (`felix-file://`); the session log
  keeps the reference and `attachments.py:resolve_file_refs` expands it just before the wire, under
  the caller's tenant only. Ids are uuid4 hex and keys are containment-checked, as for artifacts.
- **Workspace notes** — `workspace_notes.py` tells a run in flight that the operator edited a
  workspace file (`POST /chat/workspace/edited`). The client sends structure; the text the model
  reads is rendered server-side so a note cannot carry instructions.

## Security-relevant paths

Read `.claude/skills/security-review/SKILL.md` before changing egress, shell, sandbox, stdio,
the client bridge or checkouts. `.claude/hooks/pr-quality-gate.sh` asks for
`felix-security-reviewer` when a changed path matches its control-path list, which covers
`shell`, `workspace`, `sandbox`, `browser`, `stdio`, `transport`, `http_fetch`, `web_search`,
`mcp/client`, `github`, `repos` and `client_bridge`. It does **not** match `artifacts.py`,
`attachments.py`, `tool_images.py`, `image_tools.py`, `document_search.py`, `queues.py`,
`client_requests.py` or `types.py` — ask for that review yourself when one of them changes a
tenancy or containment check.

## Tests

`tests/unit/` holds one file per family: `test_shell_tool.py`, `test_shell_isolation.py`,
`test_shell_runner.py`, `test_workspace_tools.py`, `test_workspace_scopes.py`,
`test_workspace_symlinks.py`, `test_workspace_hosted.py`, `test_http_fetch_tool.py`,
`test_web_search_tool.py`, `test_egress_and_sandbox.py`, `test_browser_sandbox_procedural.py`,
`test_client_bridge_approvals.py`, `test_client_requests.py`, `test_mcp_peers.py`,
`test_mcp_tool_allowlist.py`, `test_stdio_mcp_policy.py`, `test_document_search_tool.py`,
`test_publish_commits.py`, `test_thread_publish.py`, `test_repo_checkouts.py`,
`test_image_tools.py`, `test_read_artifact_tool.py`, `test_artifact_ledger.py`,
`test_attachment_upload.py`, `test_workspace_notes.py`, plus `test_tool_trust_boundary.py`,
`test_tool_arity_dispatch.py` and `test_tool_errors_marked.py` for the runtime itself.

```bash
./scripts/test.sh tests/unit/test_shell_tool.py -q
./scripts/test.sh -k "workspace or http_fetch" -q
```

## Changing a tool

1. New manifest-bound family: a `*Ref` in `manifests/schema.py`, a `tools_from_*` factory here, a
   guarded lazy binding in `build_agent`, then `make schema` (see the manifest-authoring skill).
2. Give a non-in-process executor its own `transport` and a `source`; never `local`.
3. Decide `replay_safe` deliberately: true only when re-running after a crash has no side effect.
4. Outbound HTTP: check the URL at bind with `security/ssrf.py:assert_safe_outbound_url`
   (`resolve=False`) and dial through `safe_async_client`; never `follow_redirects=True` where a
   prefix confines the tool.
5. Filesystem: go through `workspace_root()` or the workspace backend, never `FELIX_WORKSPACE_ROOT`
   directly, so scope, checkout and the hosted backend all apply.
6. Return failures with `tool_error_output`; keep refusals uninformative where they face the model.
7. Optional dependency: import inside the function and gate the test with
   `tests/optional_deps.py:require_optional`.
8. New settings: `felix/config.py`, `.env.example`, README; a behaviour change on a control path
   updates `deploy/GOVERNANCE.md` too. A new SSE frame needs `make contract`.
