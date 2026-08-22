#!/bin/bash
# PreToolUse(Bash): a bare pytest run reads .env and points at a real Postgres,
# so DB-touching tests fail with a connection error that looks like a code bug.
# Force the supported in-memory path instead.
INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$CMD" ] && exit 0

case "$CMD" in
  *pytest*) ;;
  *) exit 0 ;;
esac

# Entry points that set the in-memory env themselves — let them run.
# scripts/test.sh is canonical; make test and make check delegate to it.
case "$CMD" in
  *FELIX_DATABASE_URL=*|*scripts/test.sh*|*"make test"*|*"make check"*) exit 0 ;;
esac
[ -n "$FELIX_DATABASE_URL" ] && exit 0

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
