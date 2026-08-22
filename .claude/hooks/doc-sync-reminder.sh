#!/bin/bash
# PostToolUse(Edit|Write): when a documented surface changes, name the exact
# public-docs page that must follow. Public docs live in the SEPARATE felix-web
# repo (apps/docs, Starlight MDX) — override the checkout with FELIX_DOCS_ROOT.
# Reminder only; doc-drift-stop.sh is the enforcement backstop.
fp=$(jq -r '.tool_input.file_path // empty')
[ -z "$fp" ] && exit 0
root="${CLAUDE_PROJECT_DIR:-.}"
rel="${fp#"$root"/}"
case "$rel" in .claude/*) exit 0 ;; esac

docs="${FELIX_DOCS_ROOT:-$HOME/Projects/felix-web/apps/docs}/src/content"

emit() {
  jq -cn --arg ctx "Docs surface touched ($rel) -> $1
Public docs are MDX in the felix-web repo at $docs (guide/ = operators+integrators, internals/ = mechanism). Use the docs-sync skill; it maps every surface and lists the verification commands." \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
  exit 0
}

case "$rel" in
  apps/api/src/felix_api/routes/chat.py|apps/api/src/felix_api/routes/openai_compat.py|apps/api/src/felix_api/routes/a2a.py|apps/api/src/felix_api/routes/mcp.py|apps/api/src/felix_api/routes/well_known.py)
    emit "guide/rest-api.mdx (endpoint tables, request/response shapes, SSE frames) and the README protocol table. A new SSE event also needs the chat-ui StreamEvent union in felix-web (apps/chat-ui/src/types.ts) or clients silently ignore it." ;;
  apps/api/src/felix_api/routes/audit.py|apps/api/src/felix_api/routes/approvals.py|apps/api/src/felix_api/routes/plans.py|apps/api/src/felix_api/routes/jobs.py|apps/api/src/felix_api/routes/manifests.py|apps/api/src/felix_api/routes/eval.py|apps/api/src/felix_api/routes/usage.py|apps/api/src/felix_api/routes/internal.py)
    emit "guide/management-api.mdx (scoped surface: document the required mgmt scopes exactly as require_mgmt_scopes() enforces them)." ;;
  packages/harness/src/felix/manifests/schema.py)
    emit "guide/manifest-reference.mdx (field-by-field reference) — and internals/manifest-pipeline.mdx if the field changes how build_agent compiles." ;;
  packages/harness/src/felix/manifests/builder.py|packages/harness/src/felix/manifests/resolver.py|packages/harness/src/felix/manifests/pin.py)
    emit "internals/manifest-pipeline.mdx (resolve -> pin -> compile -> wrapper order)." ;;
  packages/harness/src/felix/patterns/react.py|packages/harness/src/felix/patterns/registry.py|packages/harness/src/felix/patterns/types.py)
    emit "internals/patterns.mdx (the Agent invoke/stream contract and the ReAct loop)." ;;
  packages/harness/src/felix/patterns/model.py|packages/harness/src/felix/patterns/model_registry.py)
    emit "internals/model-client.mdx (provider routing, thinking/caching, fallback) and the DEFAULT_MODEL_ROUTES table in the README / getting-started if a logical model id changed." ;;
  packages/harness/src/felix/auth/*|packages/harness/src/felix/manifests/inbound_auth.py)
    emit "internals/auth.mdx (auth modes, scopes, inbound manifest auth) and guide/deploy.mdx for production JWT/api_key requirements." ;;
  packages/harness/src/felix/governance/*|packages/harness/src/felix/manifests/governance.py|packages/harness/src/felix/security/*)
    emit "internals/governance.mdx AND deploy/GOVERNANCE.md in this repo (SOC2 / EU AI Act control mapping, secret refs, screening defaults)." ;;
  packages/harness/src/felix/db/*|packages/harness/src/felix/session/store.py|migrations/versions/*)
    emit "internals/persistence.mdx (tables, tenant RLS, session event log, in-memory test path)." ;;
  packages/harness/src/felix/observability/*|packages/harness/src/felix/audit/*|packages/harness/src/felix/usage/*)
    emit "internals/observability.mdx (audit event catalog, metric/counter names, tracing spans)." ;;
  packages/harness/src/felix/config.py|deploy/*|.env.example|Makefile)
    emit "guide/deploy.mdx and guide/getting-started.mdx (env vars, Compose/Helm/AWS/GCP steps, lean-vs-full matrix)." ;;
  packages/harness/src/felix/sdk.py|clients/cli.py)
    emit "guide/getting-started.mdx (Python client usage: prompt/stream/steer/follow_up/fork/rewind/set_model)." ;;
  packages/harness/src/felix/skills/*|skills/*/SKILL.md)
    emit "guide/concepts.mdx (Agent Skills: progressive disclosure, spec.skills wiring)." ;;
  packages/cli/src/felix_cli/main.py)
    emit "guide/getting-started.mdx + guide/deploy.mdx (CLI command list must match: migrate, eval, mint-jwt, bundle-manifests, validate-manifest, doctor, version, temporal-worker)." ;;
  tests/*)
    emit "internals/testing.mdx (test layout and the memory:// no-infrastructure path) — only if the testing story itself changed." ;;
esac
exit 0
