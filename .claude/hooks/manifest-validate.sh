#!/bin/bash
# PostToolUse(Edit|Write|MultiEdit): validate a changed felix/v1 manifest immediately --
# schema + governance -- instead of discovering it in CI's bundle step.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
fp=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$fp" ] && exit 0
[ -f "$fp" ] || exit 0

# shellcheck source=lib/command.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/command.sh"

# The owning repository, not CLAUDE_PROJECT_DIR: under a worktree the old strip left
# `.claude/worktrees/<name>/` on the path, so this never matched -- and had it matched, it
# would have validated the main checkout's copy of the file rather than the edited one.
root=$(hook_repo_root "$fp")
[ -n "$root" ] || exit 0
rel=$(hook_repo_rel "$fp")
case "$rel" in manifests/*.yaml|manifests/*.yml) ;; *) exit 0 ;; esac
cd "$root" 2>/dev/null || exit 0
[ -d .venv ] || exit 0

env=development
grep -q 'frameworks:' "$fp" && env=production
# `--no-resolve-egress`: the default resolves every egress host in DNS, which turned each
# manifest edit into network lookups and, offline, into the hook's full timeout.
out=$(uv run --no-sync --quiet felix validate-manifest "$rel" -e "$env" --no-resolve-egress 2>&1)
status=$?
if [ "$status" -ne 0 ] || printf '%s' "$out" | grep -qiE 'invalid|governance fail'; then
  jq -cn --arg ctx "felix validate-manifest $rel -e $env failed:
$out
Fix the manifest before continuing -- 'felix bundle-manifests' loads every file in manifests/ and CI runs it before pytest. Field reference: packages/harness/src/felix/manifests/schema.py; the manifest-authoring skill maps spec fields to the builder code that consumes them." \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
fi
exit 0
