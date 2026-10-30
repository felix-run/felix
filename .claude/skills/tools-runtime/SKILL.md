---
name: tools-runtime
description: How a bound Felix tool runs: executors, transports and the trust allowlist that decides screening, the ToolProvider, and each family's rules (shell, workspaces, fetch/search egress, browser, sandboxes, containers, queues, MCP client, client tools, documents, GitHub publish, artifacts, attachments, connectors). Use when editing felix/tools/, mcp/, repos/, documents/, packages/connectors/ or the tool modules, or tracing why a tool refused, timed out, or was or was not screened.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*)
metadata:
  covers: felix/tools/, felix/mcp/, felix/repos/, felix/documents/, felix/artifacts.py, felix/attachments.py, felix/shell_runner.py, felix/workspace_notes.py, felix/ui/, felix_connectors/
---

# The tool runtime

The governance-pipeline skill covers how a manifest becomes a wrapped tool list. This one covers
what is inside each tool: how it is defined, what its executor reaches, and the rule each family
must not break.

## From spec to executor

- A `Tool` (`tools/types.py:Tool`) is a dataclass: name, schema, `executor`, plus flags such as `source`, `replay_safe` and
  `relays_untrusted` (the dataclass is the list). The executor has a `transport` string and `execute(args, ctx)`.
- `tools/types.py:define_tool` wraps a handler (pydantic `args` validated first; a bad argument
  returns `[invalid args for …]`, not an exception). `define_tool_with_executor` takes a
  hand-written executor class — that is how every outbound family is built.
- `tools/executor.py:wrap_tool` copies a tool with `dataclasses.replace`. Never rebuild a `Tool`
  field by field: a forgotten field silently resets on every wrapped tool (`replay_safe` once
  read `False` everywhere for that reason). Handler arity is decided once by
  `tools/types.py:accepts_positional`; see the plugin-seam skill on `functools.wraps`.
- **Where binding happens.** Names in `spec.tools` resolve through the `ToolProvider`
  (`tools/provider.py:InMemoryToolProvider`), filled by `tools/builtins.py:register_builtin_tools`
  (calculator, the time tools in `tools/time_tools.py`, the five workspace tools, `ask_user`, skill
  stubs) and plugin tools. The API builds
  it in `composition.py:compose`; fibers, cron, eval and the CLI use
  `tools/builtins.py:default_tool_provider`, which must stay equivalent. Every other family is
  bound inside `manifests/builder.py:build_agent` from its `spec.*` list via a `tools_from_*`
  factory in this package, lazily imported and wrapped in `try/except` that logs and continues.
  **Shell is the exception**: an operator-disallowed prefix raises `GovernanceError` and fails the
  compile. The governance wrappers are applied afterwards; their order lives in the
  governance-pipeline skill.
- Every `allow_http` a binder receives is true only with `FELIX_ENVIRONMENT=development`
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
| Workspace (`list_dir`, `read_file`, `write_file`, `edit_file`, `delete_file`, `rename_file`, `search_files`) | `tools/workspace.py`, `tools/workspace_backend.py`, `tools/workspace_local.py`, `tools/workspace_hosted.py`, `tools/workspace_scope.py` | Every path goes through `tools/workspace.py:workspace_root` (or the backend's `scope_root`): a thread's repo checkout if it has one, else the `spec.workspace.scope` directory (`thread` default, `tenant`, or `deployment` only for `FELIX_WORKSPACE_DEPLOYMENT_TENANTS`). The scope is a context var bound by `manifests/builder.py:apply_workspace_scope`; unbound means `thread`. Local walks use descriptors and never follow a symlink. `FELIX_WORKSPACE_BACKEND=hosted` sends the seven tools — for every scope but `deployment`, which stays local — to the gateway in `deploy/cloudflare/workspace-gateway` (`docs/WORKSPACE.md`); there, image `path` and `publish_commits` are refused for any scope but `deployment`. |
| Shell (`spec.shell_tools`) | `tools/shell.py`, `shell_runner.py`, `security/shell_policy.py` | argv is exec'd, never a shell. Allowed = manifest prefix inside `FELIX_SHELL_ALLOWED_COMMANDS`, checked at write, at compile (`assert_shell_commands_allowed`) and per call (`assert_argv_allowed`, against the request's settings). Local exec only where `security/shell_policy.py:local_exec_allowed` (development and `FELIX_AUTH_MODE=none`); otherwise it needs `FELIX_SHELL_RUNNER_URL` and never falls back. Under `hosted` it runs in the scope's sandbox, except the `deployment` scope, which takes the local or runner path above. The runner (`felix-shell-runner`) re-checks with its own settings and shares `tools/shell.py:exec_argv`. A thread checkout cannot use the remote runner. `tools/shell.py:exec_in_place` is the one exec path for a checked argv at any place; `run_skill_script` (`skills/scripts.py`) uses it with `kind="skill_script"`, which the runner admits by `FELIX_SKILL_SCRIPT_INTERPRETERS` instead of the shell allowlist. |
| HTTP fetch (`spec.http_tools`) | `tools/http_fetch.py` | The model picks the URL. Needs `path_prefix` or explicit `allow_any_host`. Redirects are walked by hand (`MAX_REDIRECTS`), each hop re-checked against the prefix with `_within_prefix` (origin comparison, not `startswith`); egress goes through `security/egress.py:safe_async_client`. Every *egress* refusal is the same `egress_blocked` line so the model cannot probe internal addressing; a URL outside `path_prefix` is refused by name, since the prefix is already in the tool's description. Transport `http`, not replay-safe. |
| Web search (`spec.search_tools`) | `tools/web_search.py`, `felix/search.py` | Operator endpoint (`FELIX_SEARCH_BACKEND`, `FELIX_SEARCH_URL`); bound even when unset, returning not-configured rather than vanishing. Transport `search`. |
| Browser (`spec.browser_tools`) | `tools/browser.py` | Playwright extra. Every page request, redirects and subresources included, is re-checked by a route guard; hostnames are pattern-checked before they reach Chromium's `--host-resolver-rules`. |
| Sandbox / container (`spec.sandboxes`, `spec.containers`) | `tools/sandboxes.py`, `tools/transports.py` | Images must be in `FELIX_SANDBOX_ALLOWED_IMAGES`. The Docker run is confined (no network, non-root, read-only, caps dropped) and runs via `asyncio.to_thread` so the timeout can fire. Container gateways are checked at bind and dialled through `safe_async_client`. |
| Queues (`spec.queues`) | `tools/queues.py` | Redis list per tenant and binding, in-process deque under `memory://`; tenant comes from the request context. |
| Client tools (`spec.client_tools`) | `tools/client_bridge.py`, `tools/client_requests.py` | The executor records the request (so a durable run's stream can announce it), emits `tool_request`, then waits on a waiter until `POST /chat/tool_result` answers or the timeout (default 120 s) ends it. `tool_call_id` over 256 chars is refused at once. A client-reported `error` stays a tool error. |
| Outbound MCP (`spec.mcp_servers`) | `mcp/client.py`, `mcp/stdio.py` | Bound as `server__tool` (`mcp/client.py:_bind_remote_tool`), source `mcp:<server>`. A server's `tools:` allowlist accepts both bare and prefixed names. Discovery is cached for `DISCOVERY_TTL_S`; failures are not cached. Both transports are the official `mcp` SDK's (`mcp/client.py:_session`, one session per discovery or call): HTTP through `security/egress_httpx2.py:safe_httpx2_client` (the egress pin on the SDK's httpx2), stdio through `mcp/stdio.py:stdio_transport`, which needs `FELIX_MCP_STDIO_ALLOWED_COMMANDS`, re-checked at spawn in `stdio_parameters`, which spawns the absolute path the name resolves to on the API's own `PATH` (`stdio_policy.resolve_stdio_command`), with a scrubbed child env whose `PATH` a manifest may not set, git disarmed (`stdio_policy.git_disarmed`, shared with the shell tool), and `env` keys that run code refused by `stdio_policy.env_key_refused` — at spawn and on write (`governance.assert_stdio_env_allowed`), never at compile. `auth: person:<provider>` (`mcp/person_auth.py:open_person_refs`, called by the builder with `BuildDeps.memory_person`) swaps in the compile's person's token -- vault provider, or `github` by the `github:<id>` subject -- or drops the server; discovery is then keyed by `tenant\|person`, not the token, and the tokens join that compile's secret masking. Tests run against real SDK servers (`tests/support/mcp_peer.py`). `mcp/server.py` is the inbound surface, not part of this. |
| Document search (`spec.document_tools`) | `tools/document_search.py`, `documents/store.py` | Tenant fixed at bind; hybrid lexical + vector, vector skipped (not faked) without an embedder. Says "empty corpus" apart from "no match". Transport `documents`. |
| GitHub publish (`spec.github_publish`) | `tools/github_publish.py`, `repos/checkouts.py` | `publish_commits` takes a branch and `head_sha`, not file contents; the token lives only in its HTTP client, git runs with a minimal environment built from nothing (no token, no inherited secrets). `auth: person` binds `tool_from_thread_publish`, resolved per call against the thread's checkout. Checkouts sit outside `FELIX_WORKSPACE_ROOT` (`repos/checkouts.py:checkout_root` refuses nesting), are size-capped and swept by `sweep_expired`. |
| Connectors (`connectors__*` in `spec.tools`, the `felix-connectors` plugin) | `packages/connectors/src/felix_connectors/{tools,http,registry,weather,maps,markets,fx,person,connect,ical,google_common,google_calendar,gmail,google_drive,icloud_dav,icloud,icloud_connect,ics,ics_connect,wikipedia}.py` | Plugin tools, so no binder hands them the agent's egress bound: each declares the hosts its backend reaches in `Tool.egress_hosts` (`connector_tool` refuses none), `manifests/builder.py:_check_tool_egress` refuses the compile when `network.allow_hosts` (or a parent's) does not cover them, and `http.py:get_json` holds every request to the declared hosts, so the declaration cannot drift from the destination. The guard's refusals reach the model as one fixed `permission_denied` line. A family is a module with `TOOL_NAMES` and `build_tools(settings)`, listed in `FAMILIES`; where it has alternatives, its backend comes from a `registry.py:BackendRegistry`, and an unknown name fails the tool's first build by name. Every upstream string is rendered through `tools.py:one_line`. Backends read through `http.Upstream`, whose `hosts` is derived from its `base_url`. A public instance with a rate policy is paced by host (`http.pace_host`, looked up in `get_json`), so every build shares it and an operator's own instance is not paced; a backlog past the wait is `Busy`, reported `rate_limited`. A keyed family (markets) refuses its first build without its key (`FELIX_MARKET_API_KEY`), by name; a key goes in `Upstream.headers` (never in its repr) where the API allows it. A backend's "nothing for that" (`NotFound`, `NoRoute`, `UnknownCurrency`) is part of its Protocol and answered as a finding; a quota spent behind a 200 is `QuotaSpent`, reported `rate_limited`. The Google families act as `ctx.auth.person` through the vault (`google_common.connection`): Google's own client builds each request (`googleapiclient`, the `google` extra, discovery documents it ships) and `http.send` sends it guarded; writers pass `read_only=False` to `connector_tool`, which makes them not replay-safe. A missing grant, scope or account is an `Unavailable` (`person.py`, shared with iCloud) answered in words, not an outage. The iCloud family builds its CalDAV/CardDAV requests itself (`icloud_dav.Dav`, held to `*.icloud.com`) and reads the person's app-specific password through `PersonConnection.opened(connections.secret)`, the one place vault refusals are worded; a repeating event's cost is estimated (`expansion_steps`) before it is expanded, off the event loop; writes go only to the person's own writable calendars; the plugin mounts `POST /connections/icloud` (`icloud_connect.py`) only with the `icloud` extra. The feed family (`ics.py`) reads the published `.ics` links a person connected (`POST /connections/ics`, `ics_connect.py`, the `ics` extra), each link checked against `FELIX_ICS_HOSTS` when connected and on every read; iCalendar expansion (`ical.py`) is shared with iCloud, and both connect routes answer with `connect.ConnectionView`. Transport `connectors`; readers are read-only and replay-safe, writers neither; screened only where the manifest enables content screening. |
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
`felix-security-reviewer` only when a changed path matches its control-path pattern, and that
pattern does not name every file here that holds a tenancy or containment check (artifacts,
attachments, tool images, queues among them). Read the pattern; when your change touches such a
check in a file it misses, ask for the review yourself.

## Tests

One or more files per family under `tests/unit/`, named for it (the first command below lists
them). The runtime itself: `test_tool_trust_boundary.py`, `test_tool_arity_dispatch.py`,
`test_tool_errors_marked.py`.

```bash
ls tests/unit | grep -E 'shell|workspace|http_fetch|web_search|browser|sandbox|client_|mcp|document_search|publish|checkout|image|artifact|attachment'
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
   `tests/support/optional_deps.py:require_optional`.
8. New settings: `felix/config.py`, `.env.example`, README; a behaviour change on a control path
   updates `deploy/GOVERNANCE.md` too. A new SSE frame needs `make contract`.
