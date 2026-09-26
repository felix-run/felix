#!/bin/bash
# PostToolUse(Edit|Write|MultiEdit|NotebookEdit): format + autofix the single Python file
# that changed, then report anything ruff could not fix. Same rules CI enforces.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
fp=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$fp" ] && exit 0
case "$fp" in *.py|*.md) ;; *) exit 0 ;; esac
case "$fp" in */.venv/*|*/node_modules/*) exit 0 ;; esac
[ -f "$fp" ] || exit 0

# shellcheck source=lib/command.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/command.sh"

# The repository that owns the file -- which may be a worktree, and must be *a* repo. This
# used to cd to CLAUDE_PROJECT_DIR and format whatever path it was handed, so a plan in
# ~/.claude/plans or a memory file got rewritten by ruff's Markdown code-block formatter,
# and a worktree file was formatted by the main checkout's ruff.
root=$(hook_repo_root "$fp")
[ -n "$root" ] || exit 0
cd "$root" 2>/dev/null || exit 0
[ -f pyproject.toml ] && grep -q '^\[tool\.ruff' pyproject.toml 2>/dev/null || exit 0
[ -d .venv ] || exit 0
rel=$(hook_repo_rel "$fp")

# ruff formats Python code blocks inside Markdown too, and CI runs
# `ruff format --check .` over the whole repo -- so .md is in scope for format.
# `--no-sync`: a formatter must not turn an edit into a lockfile resync and a download.
uv run --no-sync --quiet ruff format "$fp" >/dev/null 2>&1
case "$fp" in *.md) exit 0 ;; esac

uv run --no-sync --quiet ruff check --fix "$fp" >/dev/null 2>&1
left=$(uv run --no-sync --quiet ruff check --output-format concise "$fp" 2>&1 | head -20)

if [ -n "$left" ] && ! printf '%s' "$left" | grep -q "All checks passed"; then
  jq -cn --arg ctx "ruff (after format + --fix) still reports findings in $rel:
$left
Fix these now -- 'make lint' and 'ruff format --check' both gate CI. Rules deliberately disabled repo-wide are listed with reasons in pyproject.toml [tool.ruff.lint] ignore; do not add per-line noqa to work around one of those." \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
fi
exit 0
