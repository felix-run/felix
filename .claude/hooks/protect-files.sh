#!/bin/bash
# PreToolUse(Edit|Write|MultiEdit): refuse edits to files that must not be
# machine-edited — secrets, lockfiles, generated/runtime dirs, applied migrations.
INPUT=$(cat)
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0
FILE_PATH="${FILE_PATH//\\//}"
rel="${FILE_PATH#"${CLAUDE_PROJECT_DIR:-}"/}"

case "$rel" in
  .env|.env.local|.env.production|secrets/*|*/secrets/*)
    echo "Blocked: $rel holds real credentials. Edit .env.example instead and tell the user which value to set locally." >&2
    exit 2 ;;
  uv.lock)
    echo "Blocked: uv.lock is generated. Change dependencies in the relevant pyproject.toml, then run 'uv lock' (or 'uv sync')." >&2
    exit 2 ;;
  .venv/*|data/*|workspace/*|*.pyc|__pycache__/*|.ruff_cache/*|.pytest_cache/*)
    echo "Blocked: $rel is generated/runtime state, not source." >&2
    exit 2 ;;
esac

# Alembic revisions already merged into main are history: add a new revision instead.
case "$rel" in
  migrations/versions/*.py)
    if git -C "${CLAUDE_PROJECT_DIR:-.}" ls-files --error-unmatch "$rel" >/dev/null 2>&1 &&
       git -C "${CLAUDE_PROJECT_DIR:-.}" diff --quiet origin/main -- "$rel" 2>/dev/null; then
      echo "Blocked: $rel is an already-published migration. Add a NEW revision (next 000N_ prefix, down_revision = current head) instead of editing applied history. See the postgres-migrations skill." >&2
      exit 2
    fi ;;
esac
exit 0
