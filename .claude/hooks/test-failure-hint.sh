#!/bin/bash
# PostToolUseFailure(Bash): translate this repo's recurring failure modes into
# the actual fix, so the next attempt is not a guess.
INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
OUT=$(printf '%s' "$INPUT" | jq -r '[.tool_response.stdout?, .tool_response.stderr?, .tool_response.error?, .error?] | map(select(. != null)) | join("\n")' 2>/dev/null)
[ -z "$OUT" ] && exit 0

hint=""
case "$OUT" in
  *"connection to server"*|*OperationalError*|*"Connection refused"*)
    hint="Postgres/Valkey is not reachable. For tests use ./scripts/test.sh (memory:// stores, no services). For the app use 'make up' then 'make migrate'." ;;
  *"Unknown bundled manifest"*)
    hint="load_bundled() resolves manifests/ from the repo root, cwd, then the packaged bundled/ dir — run from the repo root, and check the manifest name matches the file stem in manifests/." ;;
  *"Unknown pattern"*)
    hint="build_agent could not resolve spec.pattern: the pattern module must be imported so register_pattern() runs. Check packages/harness/src/felix/patterns/__init__.py." ;;
  *"FELIX_AUTH_MODE=none requires"*|*validate_runtime*)
    hint="Settings.validate_runtime() rejected the config. For local/dev runs set FELIX_ALLOW_INSECURE=true, or set FELIX_ENVIRONMENT=development." ;;
  *ModuleNotFoundError*|*"No module named"*)
    hint="Missing optional extra or an un-synced venv: 'make install' for the lean set, 'make install-full' for all extras (aws/gcp/mcp/browser/embeddings). Optional deps must be imported lazily inside functions, never at module import time." ;;
  *"ruff"*|*"format"*)
    hint="Formatting gate: 'make fmt' then 'make lint'. CI runs 'ruff format --check .' separately from 'ruff check .'." ;;
esac

case "$CMD" in *"ty check"*) hint="${hint:-'ty check' runs over 'packages apps' in CI, not tests. Unresolved imports are errors; most other findings are warnings by design (see [tool.ty.rules] in pyproject.toml).}" ;; esac

[ -z "$hint" ] && exit 0
jq -cn --arg ctx "Felix hint: $hint" '{hookSpecificOutput:{hookEventName:"PostToolUseFailure",additionalContext:$ctx}}'
exit 0
