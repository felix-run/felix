#!/bin/bash
# PreToolUse(Bash): before opening a PR, make sure the quality reviewers actually ran
# on this exact commit. Blocks `gh pr create` once per HEAD sha; a marker written after
# the review satisfies it. Subagents only exist inside a Claude Code session, so this
# is the only place the review can be enforced locally.
# Deliberately not covered: `gh pr create -R owner/repo` targets a repo by flag rather
# than by cwd and so is never gated. This is a guardrail against the session taking a
# shortcut, not a control against an adversary running bash -- anything that can pass
# `-R` can equally touch the marker file, which the message below spells out. Closing
# it would buy nothing and would break the legitimate cross-repo case.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
cmd=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0
case "$cmd" in *"gh pr create"*) ;; *) exit 0 ;; esac

root="${CLAUDE_PROJECT_DIR:-.}"
command -v git >/dev/null 2>&1 || exit 0

# The rest of this file assumes `-C` decides which repo is being asked about. It does
# not while GIT_DIR or GIT_WORK_TREE are exported -- those win over -C, so an ambient
# GIT_DIR made every query below answer about that repo from anywhere. The visible
# symptom is a PR in an unrelated checkout blocked for unreviewed Python here.
# No `command` builtin in here: /usr/bin/command exists on macOS and not on most
# Linux, so `env ... command git` would break the hook for everyone else. `env` execs
# the git binary directly, so this does not recurse into the function.
git() { env -u GIT_DIR -u GIT_WORK_TREE git "$@"; }

# Which directory will the command actually run in? Not necessarily the one this hook
# was invoked from: a leading `cd` is how a PR gets opened in a sibling checkout from a
# session rooted here. Resolving it from the hook's own cwd made the guard below a
# no-op -- `here` could never differ from `root` -- so a docs PR in another repo was
# judged against this project's Python, and, worse, would have passed the moment this
# project's HEAD had a marker. A gate that reads as satisfied when nothing was reviewed
# is the failure worth spending these lines on.
workdir=$(printf '%s' "$INPUT" | jq -r '.cwd // empty')
[ -n "$workdir" ] || workdir=$(pwd -P)
# First line only. `^` in sed anchors per line, so before this the gate was
# redirected by a `cd` ANYWHERE in the command -- including one inside a PR body
# heredoc, where "cd /tmp/repro" is ordinary reproduction prose. That made the
# control fire or not depending on whether a path mentioned in a PR description
# happened to exist locally. A gate that intermittently disables itself on prose is
# harder to notice than one that is plainly broken.
# Follow every `cd` on the first line in order, keeping the last one that exists --
# which is what bash does for a `&&` or `;` chain. Relative chains fall out for free,
# since `workdir` advances as it goes.
#
# Two earlier attempts were wrong in opposite directions. Taking the first `cd` and
# stopping let `cd <sibling> && cd <project> && …` open a PR here with the gate pointed
# at the sibling. Refusing to resolve when there was more than one `cd` was worse: it
# was described as conservative, and it is not. `cd <project> && cd <project>/sub` is
# ordinary navigation, and blanking the target there falls back to the payload cwd --
# which skips the gate entirely whenever the session cwd is outside the project, as it
# is in any session with additional working directories. A guard that turns a working
# block into a skip on a common shape is not conservative; it is a fail-open with a
# reassuring comment on it.
first=$(printf '%s' "$cmd" | head -n 1)
while IFS= read -r target; do
  [ -n "$target" ] || continue
  target=${target%"${target##*[![:space:]]}"}   # trailing whitespace
  # One matched pair, peeled by hand -- a hook must never eval command text. Peeling
  # both pairs unconditionally resolved `cd "'/path'"` to /path, which is not where
  # bash goes: bash fails that cd and stays put. The hook and the shell disagreeing
  # about which directory a command runs in is the bypass, not the quoting itself.
  case "$target" in
    \"*\") target=${target#\"}; target=${target%\"} ;;
    \'*\') target=${target#\'}; target=${target%\'} ;;
  esac
  case "$target" in "~") target="$HOME" ;; "~/"*) target="$HOME/${target#\~/}" ;; esac
  case "$target" in /*) ;; *) target="$workdir/$target" ;; esac
  [ -d "$target" ] && workdir=$target
done <<TARGETS
$(printf '%s' "$first" | tr ';&|' '\n\n\n' | sed -n 's/^[[:space:]]*cd[[:space:]]\{1,\}\(.*\)$/\1/p')
TARGETS

# Only gate this repo: `gh pr create` in another checkout (or a worktree of another
# project) must not be judged against this project's state.
#
# Identity comes from the common git dir, not the toplevel: a linked worktree of this
# project reports its own toplevel, so `--show-toplevel` never matched `root` and every
# worktree skipped the gate. Worktrees are a normal way to work here, so that was a
# fail-open on a legitimate workflow. The common dir is shared with the main checkout,
# which is exactly the "same project" question being asked.
common=$(git -C "$workdir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
here=$(cd "$(dirname "$common")" 2>/dev/null && pwd -P) || exit 0
[ "$here" = "$(cd "$root" 2>/dev/null && pwd -P)" ] || exit 0

# HEAD and the diff come from `$workdir`, which is the worktree actually being shipped;
# only the marker is shared, keyed by that worktree's sha.
sha=$(git -C "$workdir" rev-parse HEAD 2>/dev/null) || exit 0
marker="$root/.claude/logs/quality-review/$sha"
[ -f "$marker" ] && exit 0

base=origin/main
git -C "$workdir" rev-parse --verify --quiet "$base" >/dev/null 2>&1 || exit 0

# Nothing reviewable? Then nothing to gate — docs- and config-only PRs pass straight through.
changed=$(git -C "$workdir" diff --name-only "$base"...HEAD 2>/dev/null | grep -E '^(apps|packages|tests)/.*\.py$')
[ -z "$changed" ] && exit 0

tests_changed=$(printf '%s\n' "$changed" | grep -c '^tests/')
n=$(printf '%s\n' "$changed" | wc -l | tr -d ' ')
second="felix-test-quality-reviewer is not needed — no tests changed."
[ "$tests_changed" -gt 0 ] && second="Tests changed too, so also delegate to felix-test-quality-reviewer on the changed files under tests/."

cat >&2 <<EOF
Blocked: $n Python file(s) changed against $base, but the quality reviewers have not run on this
commit ($(printf '%s' "$sha" | cut -c1-8)).

Do this first, then re-run the same gh pr create:

  1. Delegate to felix-quality-reviewer on: git diff $base...HEAD
  2. $second
  3. Act on the compounding findings, or say why each one stands.
  4. Record it so this gate passes:
       mkdir -p .claude/logs/quality-review && touch .claude/logs/quality-review/\$(git rev-parse HEAD)

The marker is keyed to the commit sha, so amending or adding a commit asks for a fresh review —
that is deliberate; a review of code that is no longer what you are shipping is worse than none.
Nothing here is graded on finding something: "reviewed, nothing compounding" is a normal result.
EOF
exit 2
