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
# Four checks, therefore: the run exited 1, it reported every item failing, it scored rows at
# all (without them the last check has nothing to look at), and none of those rows errored.
#
# Both the `eval` CI job and `make check-ci` run this script. They used to carry their own
# copies of the shell, and the copies drifted: the local one accepted a bare exit 1, so a
# renamed fixture passed before a push and failed in CI.
#
# The run record is JSON on stdout and progress goes to stderr, so these are parsed rather
# than grepped. They were substring matches over a pretty-printed Python dict until the CLI
# stopped rendering it — including a *negative* match for "error" across the whole blob, which
# any score row quoting that word would have tripped.
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

fail() { echo "::error::$*" >&2; echo "eval counter-smoke: $*" >&2; exit 1; }

# stdout and stderr kept apart: stdout is the record to parse, stderr is progress to show.
err_file=$(mktemp)
trap 'rm -f "$err_file"' EXIT
out=$(uv run felix eval --dataset negative --manifest quick --fixture "$FIXTURE" --mock 2>"$err_file")
rc=$?
cat "$err_file" >&2

[ "$rc" -eq 1 ] || fail "expected exit 1 from the negative fixture, got $rc"

printf '%s' "$out" | python3 -c '
import json, sys

raw = sys.stdin.read().strip()
if not raw:
    sys.exit("the run printed no record; it did not get as far as scoring")
try:
    run = json.loads(raw)
except json.JSONDecodeError as exc:
    sys.exit("the run record is not JSON (%s); a parser cannot read this gate" % exc)

passed = run.get("pass_count")
if passed != 0:
    sys.exit("pass_count is %r, want 0 — the scorer accepted an item it must reject" % passed)
rows = run.get("scores") or []
if not rows:
    sys.exit("no score rows, so the error check below would have nothing to look at")
# Key presence, not truthiness. `start_run` writes `str(exc)`, which is "" for any
# exception raised with no arguments — TimeoutError(), a bare `raise SomeError` — so a
# truthiness test reads a crashed item as an honest rejection, which is the one confusion
# this check exists to resolve.
errored = [row.get("item_id") for row in rows if "error" in row]
if errored:
    sys.exit("items errored instead of being scored down: %s" % errored)
print("eval counter-smoke: %d items scored down, exit 1, no errors — as required" % len(rows))
' || fail "the negative run did not reject its fixture the way a working scorer would"
