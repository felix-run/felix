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

# Where the command will run, which is not where this hook was invoked from. Resolving
# the branch from CLAUDE_PROJECT_DIR meant a session in a worktree was judged against the
# main checkout: every commit on a feature branch was warned about as a commit on main,
# and `--force-with-lease` on that branch was refused. See `hook_workdir`.
WORKDIR=$(hook_workdir "$INPUT" "$CMD")

# The branch the segment will act on, asked the way the segment itself would ask.
#
# The segment's own global options are replayed rather than interpreted -- `-C`,
# `--git-dir`, `--work-tree`, `-c` -- so git applies its own precedence and the hook
# cannot disagree with the shell about which repository that is. Interpreting them was
# wrong in both directions that matter: `git -C a -C b` is cumulative and a first-match
# parser answered `a`, and an exported GIT_DIR outranks `-C` entirely, so scrubbing the
# environment here (as pr-quality-gate.sh rightly does for its *identity* question) made
# this predictive one answer about a repository the command would not touch. Both of
# those failed open, which for the one block below is the direction that costs something.
#
# `-C "$WORKDIR"` goes first so a relative `-C` resolves against the working directory,
# as it would in the shell; a later absolute one simply wins.
#
# The fallback is the project root: an unresolvable path -- a `$VAR` no hook may expand --
# means "cannot tell", and the honest response to that for a *block* is to judge the repo
# the session belongs to rather than wave it through.
current_branch() {
  local seg=$1 branch='' prev='' opt
  local -a globals=()
  while IFS= read -r opt; do
    [ -n "$opt" ] || continue
    # Only the tilde, and only because the shell would have expanded it before git saw
    # it -- a literal `~/x` makes git find no repository, and the fallback then answers
    # about a different one. A relative path is deliberately left alone: `-C` is
    # cumulative, so the `-C "$WORKDIR"` above is what it resolves against, which is the
    # same rule the shell applies. Resolving it here as well would be a second mechanism
    # for one behaviour, and a behaviour with two mechanisms has no single point that can
    # be tested.
    case "$prev" in
      -C | --git-dir | --work-tree) opt=$(hook_expand_tilde "$opt") ;;
    esac
    case "$opt" in
      --git-dir=* | --work-tree=*) opt="${opt%%=*}=$(hook_expand_tilde "${opt#*=}")" ;;
    esac
    globals+=("$opt")
    prev=$opt
  done <<GLOBALS
$(hook_git_globals "$seg" git)
GLOBALS
  branch=$(git -C "$WORKDIR" "${globals[@]}" rev-parse --abbrev-ref HEAD 2>/dev/null)
  [ -n "$branch" ] || branch=$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)
  printf '%s' "$branch"
}

committing=0
commit_seg=
commit_dir=
while IFS= read -r seg; do
  [ "$(hook_segment_verb "$seg")" = "git" ] || continue

  sub=$(hook_subcommand "$seg" git)

  case "$sub" in
    push)
      # --force-with-lease is the remedy this hook recommends, so it must not be what
      # the hook blocks outright -- the old `*push*--force*` matched it and made the
      # advice unfollowable. It is still refused on main, which is what the advice says.
      if hook_has_flag "$seg" --force-with-lease; then
        [ "$(current_branch "$seg")" = "main" ] &&
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
    commit) committing=1; commit_seg=$seg ;;
  esac

  hook_has_flag "$seg" --no-verify &&
    deny "Blocked: --no-verify skips the pre-commit hooks (ruff lint/format) that CI re-runs. Fix the findings instead."
done <<EOF
$(hook_segments <<<"$CMD")
EOF

if [ "$committing" = 1 ]; then
  if [ "$(current_branch "$commit_seg")" = "main" ]; then
    jq -cn --arg ctx "You are about to commit on main. House rule: land work on a <type>/<slug> branch (feat/, fix/, docs/, chore/, refactor/) and open a PR — see the branch-pr-workflow skill. Only commit directly to main if the user explicitly asked for that." \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:$ctx}}'
  fi
fi
exit 0
