#!/bin/bash
# PreToolUse(Bash): a bare pytest run reads .env and points at a real Postgres,
# so DB-touching tests fail with a connection error that looks like a code bug.
# Force the supported in-memory path instead.
INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$CMD" ] && exit 0

case "$CMD" in
  *pytest*|*"make test"*|*"make check"*) ;;
  *) exit 0 ;;
esac

# Already configured (inline or exported) — let it run.
case "$CMD" in *FELIX_DATABASE_URL=*|*felix-test.sh*) exit 0 ;; esac
[ -n "$FELIX_DATABASE_URL" ] && exit 0

cat >&2 <<'TXT'
Blocked: this pytest run would inherit FELIX_DATABASE_URL from .env and fail against a real Postgres
(sqlalchemy OperationalError: connection refused) — that is an environment failure, not a test failure.

Use the wrapper, which sets the in-memory stores the suite is designed for:
  .claude/scripts/felix-test.sh                       # whole suite
  .claude/scripts/felix-test.sh tests/unit/test_react_loop.py -q
  .claude/scripts/felix-test.sh -k compact

Or prefix explicitly:
  FELIX_ALLOW_INSECURE=true FELIX_AUTH_MODE=none FELIX_DATABASE_URL=memory://ci FELIX_OBJECT_STORE=memory uv run pytest -q

For 'make check', run the three gates separately: 'make lint', 'make type', then the wrapper above.
TXT
exit 2
