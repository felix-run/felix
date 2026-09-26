#!/bin/bash
# PostToolUse(Edit|Write|MultiEdit|NotebookEdit): repo-internal companion-file rules.
# Changing one of these surfaces means another file in the SAME repo must change with it.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
fp=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')
[ -z "$fp" ] && exit 0

# shellcheck source=lib/command.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/command.sh"
# Relative to the owning repository, so a worktree edit is judged by its real path rather
# than by `.claude/worktrees/<name>/...`, which matched none of the cases below.
rel=$(hook_repo_rel "$fp")

emit() {
  jq -cn --arg ctx "$1" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
  exit 0
}

case "$rel" in
  packages/harness/src/felix/config.py)
    emit "Settings changed: every new FELIX_ setting needs (1) a line in .env.example with a comment, (2) a row in the README table if it changes the lean/full story, (3) a validate_runtime() guard if an unsafe combination is now possible. Deploy surfaces that also carry env: deploy/docker/compose*.yml, deploy/helm/felix/values.yaml." ;;
  packages/harness/src/felix/manifests/schema.py)
    emit "Manifest schema changed: a new spec field is inert until builder.py consumes it. Wire it in packages/harness/src/felix/manifests/builder.py (binder or apply_* wrapper), extend tests/unit/test_manifest_schema.py, and re-validate the bundled manifests ('uv run felix bundle-manifests'). If it is a governance control, also update deploy/GOVERNANCE.md and manifests/governed.yaml." ;;
  packages/harness/src/felix/manifests/builder.py)
    emit "Builder changed: the governance wrapper order defines precedence (each wrapper clones the tool with a new executor), and test_invariants.py pins it -- the order and how to add a control are in the governance-pipeline skill. Cover the new path in tests/unit/ and, if it changes what a request sees, in tests/e2e/." ;;
  packages/harness/src/felix/plugins.py|apps/api/src/felix_api/composition.py)
    emit "Plugin seam changed: composition.py is the ONLY core file allowed to name plugins, and core must never import felix_commerce / felix_enterprise. tests/unit/test_plugin_boundary.py asserts both — run it after this edit." ;;
  packages/harness/src/felix/patterns/registry.py|packages/harness/src/felix/patterns/react.py)
    emit "Pattern layer changed: patterns register at import time via register_pattern(); nothing in core enumerates them. A new pattern needs a manifests/ example and a spec.pattern value that build_agent can resolve, or it fails with 'Unknown pattern'." ;;
  packages/harness/src/felix/db/models.py)
    emit "ORM models changed: add a matching Alembic revision under migrations/versions/ (next 000N_ prefix, down_revision = current head) — the models are not auto-migrated. If the table is tenant-scoped, mirror the RLS policy pattern from 0006_tenant_rls.py, and give the store a memory:// twin plus a tests/conformance arm. See the postgres-migrations skill." ;;
  packages/ai/src/felix_ai/*|packages/harness/src/felix/decisions.py|packages/harness/src/felix/patterns/model*.py)
    emit "Model layer changed: felix_ai may not import felix (test_invariants.py). A new or changed provider or decider must pass its conformance contract -- tests/conformance/test_model_provider.py or test_decision_provider.py, where a skip is a bug. Pricing lives in felix_ai/catalog.py. The model-layer skill has the procedure." ;;
  apps/worker/src/felix_worker/tasks.py)
    emit "Worker tasks changed: cron schedules are Taskiq labels on the task, so a new periodic job only runs when felix-scheduler is running alongside felix-worker. Check deploy/docker/compose.yml and deploy/helm/felix for the scheduler service before assuming it fires in a deployment." ;;
  pyproject.toml|packages/*/pyproject.toml|apps/*/pyproject.toml)
    emit "Dependency surface changed: keep the DEFAULT install lean — heavy deps (Playwright, sentence-transformers, DuckDB, Presidio, Temporal, cloud SDKs) belong in an optional extra, imported lazily inside the function that needs them. Forward any new extra from the root pyproject [project.optional-dependencies], run 'uv lock', and note it in the README extras table." ;;
esac
exit 0
