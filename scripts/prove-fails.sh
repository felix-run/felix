#!/usr/bin/env bash
# Prove a test can fail — run it against the source as it was *before* the change.
#
#   ./scripts/prove-fails.sh tests/unit/test_entrypoint_wiring.py
#   ./scripts/prove-fails.sh --base origin/main tests/unit/test_x.py::test_y
#   ./scripts/prove-fails.sh --base 9c16791^ --only api tests/unit/test_entrypoint_wiring.py
#
# A test added alongside a fix that passes on the unfixed code is worse than no test: it
# costs the same to run and buys false confidence. This repo has shipped several — an AST
# invariant that matched `timeout=<Constant>` while every literal it was hunting lived
# inside `httpx.Timeout(...)`, so it could not fail on any file it named.
#
# The three outcomes are all informative, and the middle one is the one people miss:
#
#   PROVEN   the test failed against the old source. It is evidence.
#   VACUOUS  the test passed against the old source. It does not test the change.
#   BROKEN   the test errored (import, collection, fixture). That is not the same as
#            failing — an ERROR means the test itself is wrong, and a test that errors
#            against the old tree tells you nothing about whether it would catch the bug.
#
# How it works: a detached worktree at <base> supplies the old package sources on
# PYTHONPATH, which shadows the editable .pth entries in .venv. The tests, conftest, and
# everything outside a package `src/` root still come from your working tree — which is the
# point, since the test being proven is the new one.
#
# **It changes what `import` resolves, and nothing on disk.** That distinction is the whole
# limitation, and an earlier version of this comment blurred it by saying the tool "reverts
# Python under the five package src/ roots", which reads as though those files change. They
# do not. A test that opens a file — `ROOT / "apps/api/src/felix_api/app.py"`, an `rglob` over
# `packages/`, an AST walk of the tree — reads your working tree at every base, and this tool
# can tell it nothing. That is most of the structural invariants in this repo. For those, the
# procedure that works is mutation: introduce the violation on purpose, watch the test go red,
# revert. This script warns when it sees that shape rather than handing you a confident verdict
# it has no basis for.
#
# `--only <names>` shadows just those distributions (comma-separated: ai, harness, cli, api,
# worker) and leaves the rest at your working tree. Reach for it when a whole-tree shadow
# reports BROKEN for a reason that has nothing to do with the test: `tests/conftest.py` comes
# from your working tree and runs against old source, so a base far enough back that conftest
# calls something not yet written errors in fixture setup. Narrowing to the distribution the
# change is in keeps the fixture working and still removes the fix.
#
# So: use this for a test that exercises code through an import — most behavioral tests. Do
# not trust it for a test that reads files. Checking out the base ref in a real worktree and
# running there is the fallback when you need the disk reverted too.
set -euo pipefail
cd "$(dirname "$0")/.."

base=HEAD
only=""
while :; do
  case "${1:-}" in
    --base) base=${2:?--base needs a ref}; shift 2 ;;
    --only) only=${2:?--only needs a comma-separated list of distributions}; shift 2 ;;
    *) break ;;
  esac
done
if [ $# -eq 0 ]; then
  echo "usage: $0 [--base <ref>] <pytest target> [pytest args...]" >&2
  exit 64
fi

git rev-parse --verify --quiet "$base^{commit}" >/dev/null || {
  echo "prove-fails: '$base' is not a commit" >&2
  exit 64
}

worktree=$(mktemp -d "${TMPDIR:-/tmp}/felix-prove-fails.XXXXXX")
cleanup() {
  git worktree remove --force "$worktree" >/dev/null 2>&1 || rm -rf "$worktree"
}
trap cleanup EXIT

# --detach: never move a branch. This must not be able to disturb the working tree it is
# reporting on; the whole value of the answer depends on the tree still being what it was.
git worktree add --detach --quiet "$worktree" "$base"

declare -a names=(ai harness cli api worker)
declare -a paths=(packages/ai/src packages/harness/src packages/cli/src apps/api/src apps/worker/src)

if [ -n "$only" ]; then
  wanted=",${only//[[:space:]]/},"
  for name in $(printf '%s' "$only" | tr ',' ' '); do
    case " ${names[*]} " in *" $name "*) ;; *)
      echo "prove-fails: unknown distribution '$name' (have: ${names[*]})" >&2; exit 64 ;;
    esac
  done
fi

present=()
for i in "${!names[@]}"; do
  [ -n "$only" ] && case "$wanted" in *",${names[$i]},"*) ;; *) continue ;; esac
  root="$worktree/${paths[$i]}"
  [ -d "$root" ] && present+=("$root")
done
[ ${#present[@]} -gt 0 ] || {
  echo "prove-fails: no package source roots at $base — is it a Felix commit?" >&2
  exit 64
}
shadow=$(IFS=:; echo "${present[*]}")

echo "prove-fails: running against source at $(git rev-parse --short "$base") ($base)"
echo "prove-fails: shadowing ${only:-all packages}"

# Does the target read the tree from disk? If so this tool cannot revert what it sees, and
# every verdict below is about the wrong thing. Saying so up front is the difference between
# a limitation and a trap: the first target aimed at this was a scanning test, and it got a
# confident PROVEN on a comparison that never happened.
target=""
for arg in "$@"; do
  case "$arg" in -*) continue ;; *) target=${arg%%::*}; break ;; esac
done
if [ -n "$target" ] && [ -f "$target" ] &&
   grep -qE '(Path\(__file__\)|\.rglob\(|\.glob\(|os\.walk\(|read_text\(|open\()' "$target"; then
  cat <<WARN

  !! $target reads files from the tree.
     This shadows PYTHONPATH only — nothing on disk changes — so any assertion about file
     *contents* sees your working tree at every base, and the verdict below does not mean
     what it says. Use mutation instead: introduce the violation on purpose, run the test,
     watch it go red, revert. Verdict follows for the import-driven parts only.
WARN
fi
echo

set +e
# -p no:cacheprovider: a run against old source must not write .pytest_cache entries that
# the next real run would read as last-failed state.
out=$(PYTHONPATH="$shadow${PYTHONPATH:+:$PYTHONPATH}" \
  ./scripts/test.sh "$@" -p no:cacheprovider --tb=short -q 2>&1)
status=$?
set -e

printf '%s\n' "$out"
echo

# The last non-empty line is pytest's summary ("1 failed, 2 passed in 0.4s"). Anchoring
# there rather than grepping the last 20 lines keeps an assertion message like "found 2
# errors in the manifest" from being counted as a collection error.
summary=$(printf '%s\n' "$out" | grep -vE '^\s*$' | tail -n 1)
verdict() { printf '\n%s\n' "$1"; }

case "$status" in
  0)
    verdict "VACUOUS — passed against the pre-change source, so it does not pin the change.
Break the thing on purpose and watch it go red; if it stays green the guard is decoration.
Common cause: the assertion matches one syntactic form and the real code uses another."
    exit 1
    ;;
  1)
    if printf '%s' "$summary" | grep -qE '(^|[, ])[0-9]+ errors?([, ]|$)'; then
      verdict "BROKEN — errored rather than failed. An ERROR means the test is wrong, not the
code: a missing import, a symbol that does not exist at $base, a fixture that does not resolve.
Fix the test so it FAILS against the old source, then re-run this.

If the errors are in fixture setup rather than in the test body, this is probably not your
test: tests/conftest.py comes from your working tree and is running against ${base} source, so
it can call something that does not exist yet there. Retry with --only <dist> to shadow just
the distribution you changed."
      exit 1
    fi
    verdict "PROVEN — failed against the pre-change source. This test is evidence."
    exit 0
    ;;
  5)
    verdict "BROKEN — no tests were collected. Check the target path and any -k filter."
    exit 1
    ;;
  *)
    verdict "BROKEN — pytest exited $status (collection or usage error), which is not a test
failure. Fix the test so it FAILS against the old source, then re-run this."
    exit 1
    ;;
esac
