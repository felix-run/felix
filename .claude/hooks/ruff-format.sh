#!/bin/bash
# PostToolUse(Edit|Write|MultiEdit): format + autofix the single Python file that
# changed, then report anything ruff could not fix. Same rules CI enforces.
INPUT=$(cat)
fp=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$fp" ] && exit 0
case "$fp" in *.py|*.md) ;; *) exit 0 ;; esac
case "$fp" in */.venv/*|*/node_modules/*) exit 0 ;; esac
[ -f "$fp" ] || exit 0

root="${CLAUDE_PROJECT_DIR:-.}"
cd "$root" 2>/dev/null || exit 0
[ -d .venv ] || exit 0

# ruff formats Python code blocks inside Markdown too, and CI runs
# `ruff format --check .` over the whole repo — so .md is in scope for format.
uv run --quiet ruff format "$fp" >/dev/null 2>&1
case "$fp" in *.md) exit 0 ;; esac

uv run --quiet ruff check --fix "$fp" >/dev/null 2>&1
left=$(uv run --quiet ruff check "$fp" 2>&1 | head -20)

if [ -n "$left" ] && ! printf '%s' "$left" | grep -q "All checks passed"; then
  jq -cn --arg ctx "ruff (after format + --fix) still reports findings in ${fp#"$root"/}:
$left
Fix these now — 'make lint' and 'ruff format --check' both gate CI. Rules deliberately disabled repo-wide are listed with reasons in pyproject.toml [tool.ruff.lint] ignore; do not add per-line noqa to work around one of those." \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
fi
exit 0
