#!/bin/bash
# PreToolUse(Bash): a bare test run reads .env and points at a real Postgres, so
# DB-touching tests fail with a connection error that looks like a code bug. Force the
# supported in-memory path instead.
#
# The trigger is the segment's verb, not the presence of the word -- see lib/command.sh.
# Matching the whole string blocked `grep -rn …`, `git commit -m 'run … via the
# script'`, and this file being read at all, because its own name contains the word.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$CMD" ] && exit 0

# shellcheck source=lib/command.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/command.sh"

[ -n "$FELIX_DATABASE_URL" ] && exit 0

bare=0
while IFS= read -r seg; do
  [ "$(hook_segment_verb "$seg")" = "pytest" ] || continue
  # Entry points that set the in-memory env themselves — let them run.
  case "$seg" in *FELIX_DATABASE_URL=*) continue ;; esac
  bare=1
done <<EOF
$(hook_segments <<<"$CMD")
EOF
[ "$bare" = 0 ] && exit 0

cat >&2 <<'TXT'
Blocked: this pytest run would inherit FELIX_DATABASE_URL from .env and fail against a real Postgres
(sqlalchemy OperationalError: connection refused) — that is an environment failure, not a test failure.

Use the repo's test entry point, which sets the in-memory stores the suite is designed for:
  ./scripts/test.sh                                   # whole suite
  ./scripts/test.sh tests/unit/test_react_loop.py -q
  ./scripts/test.sh -k compact

`make test` and `make check` go through the same script, so both are fine as-is.
TXT
exit 2
