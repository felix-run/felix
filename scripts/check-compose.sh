#!/usr/bin/env bash
# Parse every Compose overlay combination `make up-*` runs, then assert what only the
# rendered config shows (scripts/check-compose-render.py). CI's docker job and
# `make compose-check` both run this, so the two cannot drift.
#
# The values below only have to be non-empty: this parses and renders, it never starts or
# pulls anything. A value already in the environment wins; Compose reads .env for the rest.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${POSTGRES_PASSWORD:=compose-check-not-a-secret}"
: "${MINIO_ROOT_PASSWORD:=compose-check-not-a-secret}"
# Assigned rather than defaulted with ${…:=…}: the JSON's braces would end that expansion early.
# The quotes are meant literally — the value is JSON, not shell words.
# shellcheck disable=SC2089,SC2090
if [ -z "${FELIX_AUTH_API_KEYS:-}" ]; then
  FELIX_AUTH_API_KEYS='{"sk-compose-check-not-a-secret":{"tenant_id":"default","sub":"ci","scopes":["admin"]}}'
fi
# The gcp overlay pins secrets_backend=gcp, which needs a project, and deploys a published
# image, which needs a tag. The builder overlay refuses to start its shell runner unauthenticated.
: "${FELIX_GCP_PROJECT:=compose-check}"
: "${FELIX_IMAGE_TAG:=compose-check}"
: "${FELIX_SHELL_RUNNER_TOKEN:=compose-check-not-a-secret}"
# shellcheck disable=SC2090  # FELIX_AUTH_API_KEYS is JSON; its quotes are data
export POSTGRES_PASSWORD MINIO_ROOT_PASSWORD FELIX_AUTH_API_KEYS FELIX_GCP_PROJECT FELIX_IMAGE_TAG \
  FELIX_SHELL_RUNNER_TOKEN

base=(-f deploy/docker/compose.yml)

# Each overlay passed as separate arguments; the combinations mirror the COMPOSE_* variables
# in the Makefile, so what this validates is what `make up-*` actually runs.
check() {
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::group::compose.yml $*"
  echo "compose config: compose.yml $*"
  docker compose "${base[@]}" "$@" --project-directory . config --quiet
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::endgroup::"
  return 0
}
check
check -f deploy/docker/compose.lite.yml
check -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml
check -f deploy/docker/compose.pgbouncer.yml
check -f deploy/docker/compose.replicas.yml
check -f deploy/docker/compose.observability.yml
check -f deploy/docker/compose.self.yml

# Rendered-config assertions a parse cannot make: no Felix service still builds under the
# published-image overlay, and every one waits on the migration.
render() { docker compose "${base[@]}" "$@" --project-directory . config --format json; }
render | python3 scripts/check-compose-render.py
render -f deploy/docker/compose.self.yml | python3 scripts/check-compose-render.py
render -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml \
  | python3 scripts/check-compose-render.py --published
echo "compose: every overlay parses and renders as required"
