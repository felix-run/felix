#!/usr/bin/env bash
# Canonical test entry point.
#
# The suite runs entirely on in-memory stores — no Postgres, Valkey, or object
# store required — which is also how CI runs it. A bare `uv run pytest` reads
# the repo .env, points FELIX_DATABASE_URL at a real Postgres, and fails the
# DB-touching tests with a connection error that looks like a code bug.
#
# Everything after the script name is forwarded to pytest:
#   ./scripts/test.sh
#   ./scripts/test.sh tests/unit/test_react_loop.py -q
#   ./scripts/test.sh -k "compact or fork" -x
set -euo pipefail
cd "$(dirname "$0")/.."
export FELIX_ALLOW_INSECURE=true
export FELIX_AUTH_MODE=none
# auth_mode=none is only permitted on a loopback bind; the repo .env sets 0.0.0.0.
export FELIX_HOST=127.0.0.1
export FELIX_DATABASE_URL=memory://ci
export FELIX_OBJECT_STORE=memory
exec uv run pytest "${@:--q}"
