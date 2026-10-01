#!/usr/bin/env bash
# Prepare /workspace — the clone the agent edits — then exec the Felix process from /app.
#
# Idempotent: a fresh volume is cloned; an existing one is fetched, never reset, so a run's
# uncommitted edits survive a restart and a person can inspect them. The venv is synced with
# the extras CI's test job installs, so `./scripts/test.sh` and `uv run ty` behave as CI does.
set -euo pipefail

# On the builder stack the workspace belongs to the `shell` service, where shell tools exec;
# api and worker set FELIX_SELF_PREPARE_WORKSPACE=0 and skip straight to their process. That is
# not only tidiness: `git fetch` runs hooks (reference-transaction) and reads the checkout's
# `.git/config`, both writable by code the agent runs, so running it here would execute that
# code in a container that holds the GitHub token. Unset, it defaults to preparing, as before.
if [ "${FELIX_SELF_PREPARE_WORKSPACE:-1}" != "1" ]; then
  exec "$@"
fi

repo="${FELIX_SELF_REPO:-https://github.com/felix-run/felix.git}"
branch="${FELIX_SELF_BRANCH:-main}"
ws="${FELIX_WORKSPACE_ROOT:-/workspace}"

# Every container that prepares the workspace mounts this volume and runs this script at the
# same moment — api and worker did, before the shell runner owned it — so without
# a lock they race: two `git fetch`es contend for the same ref locks and one fails with
# "cannot lock ref 'refs/remotes/origin/main': is at … but expected …" on every restart, two
# `uv sync`s write one venv at once, and on a fresh volume both see no `.git` and both clone —
# the second into a directory that is no longer empty, which under `set -e` exits the container.
# The lock is on the directory itself rather than a file in it, because `git clone` needs `$ws`
# empty on first boot. Both containers share one kernel, so `flock` serialises them.
exec 9<"$ws"
flock 9

if [ ! -d "$ws/.git" ]; then
  echo "self-entrypoint: cloning $repo ($branch) into $ws" >&2
  git clone --quiet --branch "$branch" "$repo" "$ws"
else
  git -C "$ws" fetch --quiet origin || echo "self-entrypoint: fetch failed; continuing with the existing clone" >&2
fi

# Repo-local identity for the commits the agent makes inside the checkout. Publishing goes
# through GitHub MCP with the bot's token, never `git push` from here.
git -C "$ws" config user.name "${FELIX_SELF_GIT_NAME:-felix-run-bot}"
git -C "$ws" config user.email "${FELIX_SELF_GIT_EMAIL:-felix-run-bot@users.noreply.github.com}"

if [ "${FELIX_SELF_SYNC:-1}" = "1" ]; then
  echo "self-entrypoint: syncing the workspace venv" >&2
  (cd "$ws" && uv sync --locked --dev --extra temporal --extra warehouse --extra sandbox --extra otel --quiet)
fi

# Released before the exec, not by it: a lock belongs to the open file, and an fd left open
# here is inherited by the Felix process, which would hold the other container at `flock` for
# as long as it runs.
flock -u 9
exec 9<&-

exec "$@"
