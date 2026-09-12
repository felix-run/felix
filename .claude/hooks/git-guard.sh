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

# The branch of the checkout the command will actually run in, not of `CLAUDE_PROJECT_DIR`.
# Those differ under a git worktree: the project root stays on `main` while the worktree is
# on a feature branch, so reading the root made every commit from a worktree warn about
# committing to main and made `--force-with-lease` -- the remedy this hook recommends --
# unusable there. A guard that fires on correct work is noise, and this one fired on every
# commit of a long session. `pr-quality-gate.sh` reads `.cwd` for the same reason, and now
# through the same `hook_workdir`, which also follows a leading `cd`.
WORKDIR=$(hook_workdir "$INPUT" "$CMD")

# The branch the segment will act on, asked the way the segment itself would ask.
#
# The segment's own global options are replayed rather than interpreted -- `-C`,
# `--git-dir`, `--work-tree`, `-c` -- so git applies its own precedence and the hook cannot
# disagree with the shell about which repository that is. Interpreting them was wrong in
# every direction tried: `git -C a -C b` is cumulative and a first-match parser answered
# `a`, and `--git-dir` names a repository as surely as `-C` does.
#
# **GIT_DIR is deliberately NOT scrubbed here**, which reverses the `env -u GIT_DIR
# -u GIT_WORK_TREE` this function carried when it first learned about worktrees. The
# reasoning there was that an exported GIT_DIR "would answer about that repo from
# anywhere" -- true, and that is precisely why it must be honoured: the *command* obeys it
# too, so a commit made with GIT_DIR set lands in that repo whatever directory the session
# is sitting in. Scrubbing it makes the hook describe a checkout the command will not
# touch, and describe it as safe. `pr-quality-gate.sh` scrubs it and is right to: that hook
# asks an identity question -- "is this checkout this project?" -- where ambient state is
# noise. This one asks a predictive one. `tests/unit/test_bash_guard_hooks.py` pins both.
#
# `-C "$WORKDIR"` goes first so a relative `-C` resolves against the working directory, as
# it would in the shell -- by git's own cumulative rule rather than a second copy of it
# here. `--exec-path` is the one global option not replayed: it selects the binaries git
# runs, which is no part of "which repository is this".
#
# The fallback is the project root: an unresolvable path -- a `$VAR` no hook may expand --
# means "cannot tell", and the honest response to that for a *block* is to judge the repo
# the session belongs to rather than wave it through.
current_branch() {
  local seg=$1 branch='' prev='' opt
  local -a globals=()
  while IFS= read -r opt; do
    [ -n "$opt" ] || continue
    # Only the tilde, and only because the shell would have expanded it before git saw it
    # -- a literal `~/x` makes git find no repository, and the fallback then answers about
    # a different one.
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
