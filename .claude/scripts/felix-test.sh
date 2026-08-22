#!/usr/bin/env bash
# Run the Felix suite the way CI does: in-memory stores, no Postgres/Valkey/MinIO.
# The repo .env points FELIX_DATABASE_URL at a real Postgres, and pydantic-settings
# reads it, so a bare `uv run pytest` fails on DB-touching tests. Everything after
# the script name is forwarded to pytest.
#
#   .claude/scripts/felix-test.sh
#   .claude/scripts/felix-test.sh tests/unit/test_react_loop.py -q
#   .claude/scripts/felix-test.sh -k compact -x
set -euo pipefail
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
export FELIX_ALLOW_INSECURE=true
export FELIX_AUTH_MODE=none
export FELIX_DATABASE_URL=memory://ci
export FELIX_OBJECT_STORE=memory
exec uv run pytest "${@:--q}"
