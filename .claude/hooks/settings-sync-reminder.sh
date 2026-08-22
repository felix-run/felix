#!/bin/bash
# PostToolUse(Edit|Write): repo-internal companion-file rules. Changing one of
# these surfaces means another file in the SAME repo must change with it.
fp=$(jq -r '.tool_input.file_path // empty')
[ -z "$fp" ] && exit 0
root="${CLAUDE_PROJECT_DIR:-.}"
rel="${fp#"$root"/}"
case "$rel" in .claude/*) exit 0 ;; esac

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
    emit "Builder changed: the governance wrapper order (secret masking -> policies -> command screening -> content screening -> limits -> guardrails -> judges -> approvals -> artifact spill) defines precedence — each wrapper clones the tool with a new executor, so reordering changes behavior. Cover the new path in tests/unit/ (test_manifest_governance.py / test_deferred_governance.py)." ;;
  packages/harness/src/felix/plugins.py|apps/api/src/felix_api/composition.py)
    emit "Plugin seam changed: composition.py is the ONLY core file allowed to name plugins, and core must never import felix_commerce / felix_enterprise. tests/unit/test_plugin_boundary.py asserts both — run it after this edit." ;;
  packages/harness/src/felix/patterns/registry.py|packages/harness/src/felix/patterns/react.py)
    emit "Pattern layer changed: patterns register at import time via register_pattern(); nothing in core enumerates them. A new pattern needs a manifests/ example and a spec.pattern value that build_agent can resolve, or it fails with 'Unknown pattern'." ;;
  packages/harness/src/felix/db/models.py)
    emit "ORM models changed: add a matching Alembic revision under migrations/versions/ (next 000N_ prefix, down_revision = current head) — the models are not auto-migrated. If the table is tenant-scoped, mirror the RLS policy pattern from 0006_tenant_rls.py. See the postgres-migrations skill." ;;
  apps/worker/src/felix_worker/tasks.py)
    emit "Worker tasks changed: cron schedules are Taskiq labels on the task, so a new periodic job only runs when felix-scheduler is running alongside felix-worker. Check deploy/docker/compose.yml and deploy/helm/felix for the scheduler service before assuming it fires in a deployment." ;;
  pyproject.toml|packages/*/pyproject.toml|apps/*/pyproject.toml)
    emit "Dependency surface changed: keep the DEFAULT install lean — heavy deps (Playwright, sentence-transformers, DuckDB, Presidio, Temporal, cloud SDKs) belong in an optional extra, imported lazily inside the function that needs them. Forward any new extra from the root pyproject [project.optional-dependencies], run 'uv lock', and note it in the README extras table." ;;
esac
exit 0
