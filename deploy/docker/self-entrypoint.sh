#!/usr/bin/env bash
# Prepare /workspace — the clone the agent edits — then exec the Felix process from /app.
#
# Idempotent: a fresh volume is cloned; an existing one is fetched, never reset, so a run's
# uncommitted edits survive a restart and a person can inspect them. The venv is synced with
# the extras CI's test job installs, so `./scripts/test.sh` and `uv run ty` behave as CI does.
set -euo pipefail

repo="${FELIX_SELF_REPO:-https://github.com/felix-run/felix.git}"
branch="${FELIX_SELF_BRANCH:-main}"
ws="${FELIX_WORKSPACE_ROOT:-/workspace}"

if [ ! -d "$ws/.git" ]; then
  echo "self-entrypoint: cloning $repo ($branch) into $ws" >&2
  git clone --quiet --branch "$branch" "$repo" "$ws"
else
  git -C "$ws" fetch --quiet origin || echo "self-entrypoint: fetch failed; continuing with the existing clone" >&2
fi

# Repo-local identity for the commits the agent makes inside the checkout. Publishing goes
# through GitHub MCP with the bot's token, never `git push` from here.
git -C "$ws" config user.name "${FELIX_SELF_GIT_NAME:-felix-bot}"
git -C "$ws" config user.email "${FELIX_SELF_GIT_EMAIL:-felix-bot@users.noreply.github.com}"

if [ "${FELIX_SELF_SYNC:-1}" = "1" ]; then
  echo "self-entrypoint: syncing the workspace venv" >&2
  (cd "$ws" && uv sync --locked --dev --extra temporal --extra warehouse --extra sandbox --extra otel --quiet)
fi

exec "$@"
