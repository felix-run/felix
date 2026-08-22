#!/bin/bash
# PostToolUse(Edit|Write): validate a changed felix/v1 manifest immediately —
# schema + governance — instead of discovering it in CI's bundle step.
INPUT=$(cat)
fp=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$fp" ] && exit 0
root="${CLAUDE_PROJECT_DIR:-.}"
rel="${fp#"$root"/}"
case "$rel" in manifests/*.yaml|manifests/*.yml) ;; *) exit 0 ;; esac
[ -f "$fp" ] || exit 0
cd "$root" 2>/dev/null || exit 0
[ -d .venv ] || exit 0

env=development
grep -q 'frameworks:' "$fp" && env=production
out=$(uv run --quiet felix validate-manifest "$rel" -e "$env" 2>&1)
if printf '%s' "$out" | grep -qiE 'invalid|governance fail'; then
  jq -cn --arg ctx "felix validate-manifest $rel -e $env failed:
$out
Fix the manifest before continuing — 'felix bundle-manifests' loads every file in manifests/ and CI runs it before pytest. Field reference: packages/harness/src/felix/manifests/schema.py; the manifest-authoring skill maps spec fields to the builder code that consumes them." \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
fi
exit 0
