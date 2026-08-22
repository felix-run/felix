#!/bin/bash
# PreToolUse(Bash): deny destructive git, warn (don't block) on committing to main.
INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$CMD" ] && exit 0
case "$CMD" in git*|*"&& git "*|*"; git "*) ;; *) exit 0 ;; esac

case "$CMD" in
  *"push"*--force*|*"push"*-f\ *)
    echo "Blocked: force-push. If a branch really needs rewriting, ask the user first and use --force-with-lease on a feature branch, never on main." >&2
    exit 2 ;;
  *--no-verify*)
    echo "Blocked: --no-verify skips the pre-commit hooks (ruff lint/format) that CI re-runs. Fix the findings instead." >&2
    exit 2 ;;
  *"reset --hard"*|*"clean -fdx"*)
    echo "Blocked: destructive working-tree reset. Confirm with the user, then run it yourself if they agree." >&2
    exit 2 ;;
esac

case "$CMD" in
  *"commit"*)
    branch=$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)
    if [ "$branch" = "main" ]; then
      jq -cn --arg ctx "You are about to commit on main. House rule: land work on a <type>/<slug> branch (feat/, fix/, docs/, chore/, refactor/) and open a PR — see the branch-pr-workflow skill. Only commit directly to main if the user explicitly asked for that." \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:$ctx}}'
    fi ;;
esac
exit 0
