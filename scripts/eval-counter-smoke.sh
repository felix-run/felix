#!/usr/bin/env bash
# The eval gate's counter-smoke, in one place because it has two callers.
#
# `felix eval --fixture fixtures/eval/smoke.json --mock` passes by construction: every item's
# `mock_answer` satisfies its own rubric, so that run proves the pipeline executes and nothing
# about whether the scorer can reject an answer. This is the other half — every item in
# `negative.json` violates its own rubric, so the run must fail — and it has to fail for the
# right reason. A missing fixture, an import error and a bad flag all exit non-zero too, and
# `start_run` counts an item that *raised* as a failure just like one it scored down, so a
# scorer that crashed on everything would read exactly like a scorer that rejected everything.
#
# Four checks, therefore: the run exited 1, it reported every item failing, it printed score
# rows at all (without them the last check is a negative assertion over an empty haystack), and
# none of those rows carries an error.
#
# Both the `eval` CI job and `make check-ci` run this script. They used to carry their own
# copies of the shell, and the copies drifted: the local one accepted a bare exit 1, so a
# renamed fixture passed before a push and failed in CI.
set -uo pipefail

FIXTURE="${1:-fixtures/eval/negative.json}"

# Set, not defaulted. The recipe lines this replaced hard-set these four, and the smoke run
# beside it in `make check-ci` still does — so deferring to the caller would mean a developer
# with FELIX_DATABASE_URL exported to a real Postgres had one half of the pair writing a dataset
# and a run row there while the other half stayed in memory. It cannot produce a false pass, but
# a gate should not touch a database because of where it was run from.
export FELIX_ALLOW_INSECURE=true
export FELIX_AUTH_MODE=none
export FELIX_DATABASE_URL=memory://ci
export FELIX_OBJECT_STORE=memory
# The checks below read the printed run dict, so rich must not wrap it in colour escapes.
# TERM=dumb is the load-bearing one: it turns rich's colour system off outright, where NO_COLOR
# alone still leaves bold escapes around numbers — which would land between `: ` and `0`.
export NO_COLOR=1
export TERM=dumb

fail() { echo "::error::$*" >&2; echo "eval counter-smoke: $*" >&2; exit 1; }

out=$(uv run felix eval --dataset negative --manifest quick --fixture "$FIXTURE" --mock 2>&1)
rc=$?
echo "$out"

[ "$rc" -eq 1 ] || fail "expected exit 1 from the negative fixture, got $rc"

flat=$(printf '%s' "$out" | tr '\n' ' ')
case "$flat" in
  *"'pass_count': 0"*) ;;
  *) fail "the negative run reported no pass_count of 0; it did not score the fixture" ;;
esac
case "$flat" in
  *"'rule'"*) ;;
  *) fail "the run printed no score rows, so the error check below would be vacuous" ;;
esac
case "$flat" in
  *"'error'"*) fail "negative items errored instead of being scored down" ;;
esac

echo "eval counter-smoke: every item scored down, exit 1, no errors — as required"
