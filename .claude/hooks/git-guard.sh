#!/bin/bash
# PreToolUse(Bash): deny destructive git, warn (don't block) on committing to main.
#
# Matching is per shell segment with heredoc bodies dropped -- see lib/command.sh. The
# whole-string version blocked `git stash push -q f && … ; rm -f /tmp/x` as a
# force-push (it contains "push" and "-f ") and blocked
# `git commit -m 'do not push --force here'` on the text of its own advice.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$CMD" ] && exit 0

# shellcheck source=lib/command.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/command.sh"

deny() { printf '%s\n' "$1" >&2; exit 2; }
current_branch() { git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null; }

committing=0
while IFS= read -r seg; do
  [ "$(hook_segment_verb "$seg")" = "git" ] || continue

  sub=$(hook_subcommand "$seg" git)

  case "$sub" in
    push)
      # --force-with-lease is the remedy this hook recommends, so it must not be what
      # the hook blocks outright -- the old `*push*--force*` matched it and made the
      # advice unfollowable. It is still refused on main, which is what the advice says.
      if hook_has_flag "$seg" --force-with-lease; then
        [ "$(current_branch)" = "main" ] &&
          deny "Blocked: --force-with-lease on main. Rewriting main is not something to do from here; use a feature branch."
      elif hook_has_flag "$seg" --force -f; then
        deny "Blocked: force-push. If a branch really needs rewriting, ask the user first and use --force-with-lease on a feature branch, never on main."
      fi ;;
    reset)
      hook_has_flag "$seg" --hard &&
        deny "Blocked: destructive working-tree reset. Confirm with the user, then run it yourself if they agree." ;;
    clean)
      # -fdx in any spelling or order, including the split forms.
      if hook_has_flag "$seg" -fdx -fxd -dfx -dxf -xfd -xdf ||
         { hook_has_flag "$seg" -f --force && hook_has_flag "$seg" -d -x; }; then
        deny "Blocked: destructive working-tree clean. Confirm with the user, then run it yourself if they agree."
      fi ;;
    commit) committing=1 ;;
  esac

  hook_has_flag "$seg" --no-verify &&
    deny "Blocked: --no-verify skips the pre-commit hooks (ruff lint/format) that CI re-runs. Fix the findings instead."
done <<EOF
$(hook_segments <<<"$CMD")
EOF

if [ "$committing" = 1 ]; then
  if [ "$(current_branch)" = "main" ]; then
    jq -cn --arg ctx "You are about to commit on main. House rule: land work on a <type>/<slug> branch (feat/, fix/, docs/, chore/, refactor/) and open a PR — see the branch-pr-workflow skill. Only commit directly to main if the user explicitly asked for that." \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:$ctx}}'
  fi
fi
exit 0
