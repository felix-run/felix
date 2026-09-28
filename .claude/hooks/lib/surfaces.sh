# Which repo paths are documented surfaces, and which public page documents each.
# Source it; it defines functions only.
#
# One copy, because four drifted apart: this map lived in doc-sync-reminder.sh, in the
# regex inside doc-drift-stop.sh, in the docs-sync skill's page-map.md and in the
# docs-syncer agent, and five management routes (artifacts, documents, files, memory,
# skills) had reached none of them. `scripts/validate-toolkit.py` checks that page-map.md
# still names every route module this file routes to management-api.mdx, so the human
# table and this one cannot silently part again.
#
# Paths are repo-relative -- pass `hook_repo_rel` output, never a path stripped of
# `CLAUDE_PROJECT_DIR`, which leaves `.claude/worktrees/<name>/` on the front.

# The page (relative to the felix-web content root) and what to check there.
# Prints nothing and returns 1 when the path is not a documented surface.
surface_page() {
  case "$1" in
    apps/api/src/felix_api/routes/chat.py|apps/api/src/felix_api/routes/openai_compat.py|apps/api/src/felix_api/routes/a2a.py|apps/api/src/felix_api/routes/mcp.py|apps/api/src/felix_api/routes/well_known.py|apps/api/src/felix_api/routes/_sse.py|apps/api/src/felix_api/routes/_streaming.py)
      echo "guide/rest-api.mdx (endpoint tables, request/response shapes, SSE frames) and the README protocol table. A new SSE event also needs the chat-ui StreamEvent union in felix-web (apps/chat-ui/src/types.ts) or clients silently ignore it." ;;
    apps/api/src/felix_api/routes/audit.py|apps/api/src/felix_api/routes/approvals.py|apps/api/src/felix_api/routes/plans.py|apps/api/src/felix_api/routes/jobs.py|apps/api/src/felix_api/routes/manifests.py|apps/api/src/felix_api/routes/eval.py|apps/api/src/felix_api/routes/usage.py|apps/api/src/felix_api/routes/internal.py|apps/api/src/felix_api/routes/artifacts.py|apps/api/src/felix_api/routes/documents.py|apps/api/src/felix_api/routes/files.py|apps/api/src/felix_api/routes/memory.py|apps/api/src/felix_api/routes/skills.py)
      echo "guide/management-api.mdx (scoped surface: document the required mgmt scopes exactly as require_mgmt_scopes() enforces them)." ;;
    packages/harness/src/felix/manifests/schema.py)
      echo "guide/manifest-reference.mdx (field-by-field reference) -- and internals/manifest-pipeline.mdx if the field changes how build_agent compiles." ;;
    packages/harness/src/felix/manifests/builder.py|packages/harness/src/felix/manifests/resolver.py|packages/harness/src/felix/manifests/pin.py)
      echo "internals/manifest-pipeline.mdx (resolve -> pin -> compile -> wrapper order)." ;;
    packages/harness/src/felix/patterns/react.py|packages/harness/src/felix/patterns/registry.py|packages/harness/src/felix/patterns/types.py)
      echo "internals/patterns.mdx (the Agent invoke/stream contract and the ReAct loop)." ;;
    packages/harness/src/felix/patterns/model*.py|packages/harness/src/felix/decisions.py|packages/ai/src/felix_ai/*)
      echo "internals/model-client.mdx (provider routing, wire formats, thinking/caching, fallback, decision models) and the DEFAULT_MODEL_ROUTES table in the README / getting-started if a logical model id changed. The model-layer skill has the procedure." ;;
    packages/harness/src/felix/auth/*|packages/harness/src/felix/manifests/inbound_auth.py)
      echo "internals/auth.mdx (auth modes, scopes, inbound manifest auth) and guide/deploy.mdx for production JWT/api_key requirements." ;;
    packages/harness/src/felix/governance/*|packages/harness/src/felix/manifests/governance.py|packages/harness/src/felix/security/*)
      echo "internals/governance.mdx AND deploy/GOVERNANCE.md in this repo (SOC2 / EU AI Act control mapping, secret refs, screening defaults)." ;;
    packages/harness/src/felix/plugins.py|apps/api/src/felix_api/composition.py)
      echo "internals/plugins.mdx (the plugin registry, entry points and what composition.py may name)." ;;
    packages/harness/src/felix/db/*|packages/harness/src/felix/session/store.py|migrations/versions/*)
      echo "internals/persistence.mdx (tables, tenant RLS, session event log, in-memory test path)." ;;
    packages/harness/src/felix/observability/*|packages/harness/src/felix/audit/*|packages/harness/src/felix/usage/*)
      echo "internals/observability.mdx (audit event catalog, metric/counter names, tracing spans)." ;;
    packages/harness/src/felix/config.py|deploy/*|.env.example|Makefile)
      echo "guide/deploy.mdx and guide/getting-started.mdx (env vars, Compose/Helm/AWS/GCP steps, lean-vs-full matrix)." ;;
    packages/harness/src/felix/sdk.py|clients/cli.py)
      echo "guide/getting-started.mdx (Python client usage: prompt/stream/steer/follow_up/fork/rewind/set_model)." ;;
    packages/harness/src/felix/skills/*|skills/*/SKILL.md)
      echo "guide/concepts.mdx (Agent Skills: progressive disclosure, spec.skills wiring)." ;;
    packages/cli/src/felix_cli/main.py)
      echo "guide/getting-started.mdx + guide/deploy.mdx (the CLI command list must match 'felix --help')." ;;
    *) return 1 ;;
  esac
}

# The subset the Stop gate holds a turn for. Narrower than `surface_page` on purpose:
# the reminder is free to be broad because it only advises, but a block that fires on a
# test file or a Makefile tweak teaches people to wave it through.
surface_blocks() {
  # A subset of `surface_page` by construction, not by care: a blocking path with no page
  # would stop a turn while the edit-time reminder stayed silent about where the docs go.
  surface_page "$1" >/dev/null || return 1
  case "$1" in
    apps/api/src/felix_api/routes/_*) return 1 ;;
    apps/api/src/felix_api/routes/*.py) return 0 ;;
    packages/harness/src/felix/manifests/schema.py|packages/harness/src/felix/manifests/builder.py) return 0 ;;
    packages/harness/src/felix/config.py|packages/harness/src/felix/sdk.py) return 0 ;;
    packages/harness/src/felix/auth/*|packages/harness/src/felix/governance/*) return 0 ;;
    packages/harness/src/felix/patterns/react.py|packages/harness/src/felix/patterns/registry.py) return 0 ;;
    packages/cli/src/felix_cli/main.py|migrations/versions/*) return 0 ;;
    deploy/docker/compose*.yml|deploy/helm/felix/values.yaml) return 0 ;;
    *) return 1 ;;
  esac
}

# A change that counts as "documentation was considered". `.env.example` is on purpose: a
# new setting's documentation *is* its commented line there.
SURFACE_DOCS_TEXT="README.md / CLAUDE.md / CHANGELOG.md / .env.example / docs/ / deploy/GOVERNANCE.md / deploy/*/README.md"
surface_is_doc() {
  case "$1" in
    README.md|CLAUDE.md|CHANGELOG.md|.env.example|deploy/GOVERNANCE.md|deploy/*/README.md|docs/*) return 0 ;;
    *) return 1 ;;
  esac
}

# The working tree's uncommitted changes, one `<path>\t<blob hash>` line per file, sorted.
#
# The Stop gate compares this against the copy session-start.sh saved, so it can tell a
# change this session made from one the tree already carried. Comparing the tree against
# HEAD alone blocked a read-only planning turn over another branch's uncommitted edits --
# asking the session to document work it had never touched.
drift_snapshot() {
  local dir=$1 path hash
  { git -C "$dir" diff --name-only HEAD 2>/dev/null
    git -C "$dir" ls-files --others --exclude-standard 2>/dev/null; } | sort -u |
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    if [ -f "$dir/$path" ]; then
      hash=$(git -C "$dir" hash-object -- "$path" 2>/dev/null)
    else
      hash=deleted
    fi
    printf '%s\t%s\n' "$path" "${hash:-unknown}"
  done
}

# The working tree a hook payload's session is in: its `cwd`, else the project root. One
# spelling for both ends of the drift gate -- if session-start.sh and doc-drift-stop.sh
# ever disagreed, their baseline file names would too, and the gate would quietly fall
# back to measuring against HEAD.
drift_tree() {
  local cwd
  cwd=$(printf '%s' "$1" | jq -r '.cwd // empty' 2>/dev/null)
  git -C "${cwd:-${CLAUDE_PROJECT_DIR:-.}}" rev-parse --show-toplevel 2>/dev/null
}

# Where the snapshot for one session and one working tree lives. Keyed by the tree as well
# as the session, because a session moves between the main checkout and its worktrees.
drift_baseline_file() {
  local sid=$1 top=$2
  printf '%s/felix-docdrift-%s-%s.base' "${TMPDIR:-/tmp}" "$sid" "$(printf '%s' "$top" | shasum | cut -c1-10)"
}
