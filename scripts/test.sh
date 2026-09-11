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
# No live vendor credentials, ever. The repo .env carries real keys, and pydantic-settings
# reads it -- so any test that reaches a model called the vendor for real and billed it. That
# is not hypothetical: the e2e harness was written against a route it believed was scripted
# and made live Anthropic calls on its first run, because .env also overrides the default
# model id.
#
# Every credential-bearing setting, not just the two vendors with named fields:
# `model_provider_options` carries a per-provider `api_key` that `resolve_provider_config`
# prefers OVER these, so leaving it set re-arms a vendor with both of them blank, and
# `search_api_key` pays for real outbound search. `test_invariants.py` pins this list against
# `Settings`, so a new credential field fails the suite rather than leaking into it.
#
# This stops the billing, not the egress: a blank key logs a warning and still sends an
# unauthenticated request. Not reaching a vendor at all is the route map's job -- see
# `tests/e2e/conftest.py`.
export FELIX_ANTHROPIC_API_KEY=""
export FELIX_OPENAI_API_KEY=""
export FELIX_SEARCH_API_KEY=""
export FELIX_MODEL_PROVIDER_OPTIONS=""
exec uv run pytest "${@:--q}"
