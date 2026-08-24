#!/bin/bash
# PreToolUse(Bash): before opening a PR, make sure the quality reviewers actually ran
# on this exact commit. Blocks `gh pr create` once per HEAD sha; a marker written after
# the review satisfies it. Subagents only exist inside a Claude Code session, so this
# is the only place the review can be enforced locally.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
cmd=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0
case "$cmd" in *"gh pr create"*) ;; *) exit 0 ;; esac

root="${CLAUDE_PROJECT_DIR:-.}"
command -v git >/dev/null 2>&1 || exit 0

# Which directory will the command actually run in? Not necessarily the one this hook
# was invoked from: a leading `cd` is how a PR gets opened in a sibling checkout from a
# session rooted here. Resolving it from the hook's own cwd made the guard below a
# no-op -- `here` could never differ from `root` -- so a docs PR in another repo was
# judged against this project's Python, and, worse, would have passed the moment this
# project's HEAD had a marker. A gate that reads as satisfied when nothing was reviewed
# is the failure worth spending these lines on.
workdir=$(printf '%s' "$INPUT" | jq -r '.cwd // empty')
[ -n "$workdir" ] || workdir=$(pwd -P)
target=$(printf '%s' "$cmd" | sed -n 's/^[[:space:]]*cd[[:space:]]\{1,\}\([^;&|]*\).*/\1/p')
if [ -n "$target" ]; then
  target=${target%"${target##*[![:space:]]}"}   # trailing whitespace
  target=${target#\"}; target=${target%\"}      # one layer of quoting, peeled by hand:
  target=${target#\'}; target=${target%\'}      # never eval attacker-shaped command text
  case "$target" in "~") target="$HOME" ;; "~/"*) target="$HOME/${target#\~/}" ;; esac
  case "$target" in /*) ;; *) target="$workdir/$target" ;; esac
  [ -d "$target" ] && workdir=$target
fi

# Only gate this repo: `gh pr create` in another checkout (or a worktree of another
# project) must not be judged against this project's state.
here=$(git -C "$workdir" rev-parse --show-toplevel 2>/dev/null) || exit 0
here=$(cd "$here" 2>/dev/null && pwd -P) || exit 0
[ "$here" = "$(cd "$root" 2>/dev/null && pwd -P)" ] || exit 0

sha=$(git -C "$here" rev-parse HEAD 2>/dev/null) || exit 0
marker="$root/.claude/logs/quality-review/$sha"
[ -f "$marker" ] && exit 0

base=origin/main
git -C "$here" rev-parse --verify --quiet "$base" >/dev/null 2>&1 || exit 0

# Nothing reviewable? Then nothing to gate — docs- and config-only PRs pass straight through.
changed=$(git -C "$here" diff --name-only "$base"...HEAD 2>/dev/null | grep -E '^(apps|packages|tests)/.*\.py$')
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
