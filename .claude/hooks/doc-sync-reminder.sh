#!/bin/bash
# PostToolUse(Edit|Write|MultiEdit|NotebookEdit): when a documented surface changes, name
# the exact public-docs page that must follow. Public docs live in the SEPARATE felix-web
# repo (apps/docs, Starlight MDX) -- override the checkout with FELIX_DOCS_ROOT.
# Reminder only; doc-drift-stop.sh is the backstop. The map itself is lib/surfaces.sh.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
fp=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')
[ -z "$fp" ] && exit 0

# shellcheck source=lib/command.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/command.sh"
# shellcheck source=lib/surfaces.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/surfaces.sh"

# Relative to the repository that owns the file. Stripping CLAUDE_PROJECT_DIR left
# `.claude/worktrees/<name>/` on the front, which the old `.claude/*) exit 0` then
# matched -- so this hook was silent for every edit made in a worktree.
rel=$(hook_repo_rel "$fp")
page=$(surface_page "$rel") || exit 0

docs="${FELIX_DOCS_ROOT:-$HOME/Projects/felix-web/apps/docs}/src/content"
jq -cn --arg ctx "Docs surface touched ($rel) -> $page
Public docs are MDX in the felix-web repo at $docs (guide/ = operators+integrators, internals/ = mechanism). Use the docs-sync skill; it maps every surface and lists the verification commands." \
  '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
exit 0
