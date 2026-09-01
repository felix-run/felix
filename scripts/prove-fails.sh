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
# `--only <names>` shadows just those distributions (comma-separated: ai, harness, cli, api,
# worker) and leaves the rest at your working tree. Reach for it when a whole-tree shadow
# reports BROKEN for a reason that has nothing to do with the test: `tests/conftest.py` comes
# from your working tree and runs against old source, so a base far enough back that conftest
# calls something not yet written errors in fixture setup. Narrowing to the distribution the
# change is in keeps the fixture working and still removes the fix.
#
# Limits, stated because a guard whose blind spot is undocumented is the defect it exists to
# catch: only Python under the five package source roots is reverted. A change to a bundled
# manifest, a JSON schema, a Compose file, or anything else read from disk at runtime is NOT
# reverted, so a test asserting on those will read the new copy and report VACUOUS. For those,
# check out the base ref in a real worktree and run there.
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

summary=$(printf '%s' "$out" | tail -n 20)
verdict() { printf '\n%s\n' "$1"; }

case "$status" in
  0)
    verdict "VACUOUS — passed against the pre-change source, so it does not pin the change.
Break the thing on purpose and watch it go red; if it stays green the guard is decoration.
Common cause: the assertion matches one syntactic form and the real code uses another."
    exit 1
    ;;
  1)
    if printf '%s' "$summary" | grep -qE '[0-9]+ error'; then
      verdict "BROKEN — errored rather than failed. An ERROR means the test is wrong, not the
code: a missing import, a symbol that does not exist at $base, a fixture that does not resolve.
Fix the test so it FAILS against the old source, then re-run this."
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
