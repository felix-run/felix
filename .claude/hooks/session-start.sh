#!/bin/bash
# SessionStart: inject the few facts that make the difference between a working
# first command and a confusing failure. Stdout is added to Claude's context.
INPUT=$(cat)
root="${CLAUDE_PROJECT_DIR:-.}"
cd "$root" 2>/dev/null || exit 0

# Baseline for doc-drift-stop.sh: what the tree already carried before this session did
# anything, so the Stop gate asks about this session's changes and nobody else's. Taken
# once per session and tree -- SessionStart fires again on resume and compact, and a
# later snapshot would silently absorb the session's own work.
if command -v jq >/dev/null 2>&1; then
  # shellcheck source=lib/surfaces.sh
  . "$(dirname "${BASH_SOURCE[0]}")/lib/surfaces.sh"
  sid=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
  top=$(drift_tree "$INPUT")
  if [ -n "$sid" ] && [ -n "$top" ]; then
    base=$(drift_baseline_file "$sid" "$top")
    [ -f "$base" ] || drift_snapshot "$top" > "$base" 2>/dev/null
  fi
fi

echo "Felix harness (Python 3.14, uv workspace). Tests need the in-memory env:"
echo "  ./scripts/test.sh [pytest args]   # sets FELIX_DATABASE_URL=memory://ci etc."
echo "A bare 'uv run pytest' picks up .env and fails against a real Postgres (pytest-env-guard hook blocks it)."

# A fixture identity in this repo's own config. Test fixtures set `user.name t` /
# `user.email t@example.com`; one wrote them into the real `.git/config` once, and thirty
# commits went out authored by `t` before anyone asked why. The test suite now fails the
# test that does it; this catches one that got in some other way.
local_email=$(git config --local user.email 2>/dev/null)
case "$local_email" in
  *@example.com|*@example.org|*@example.net)
    echo "WARNING: this repo's .git/config sets user.email=$local_email (a test-fixture identity) — commits here are not authored as you. Fix: git config --local --unset user.name; git config --local --unset user.email" ;;
esac

[ -d .venv ] || echo "WARNING: .venv missing — run 'make install' (uv sync --dev) before lint/type/test."

if [ -f .env ]; then
  grep -qE '^FELIX_DATABASE_URL=' .env || echo "NOTE: .env has no FELIX_DATABASE_URL; Settings defaults to localhost Postgres."
else
  echo "NOTE: .env missing — 'cp .env.example .env' before 'make dev' / 'make up'."
fi

if command -v docker >/dev/null 2>&1; then
  up=$(docker compose -f deploy/docker/compose.yml --project-directory . ps --status running -q 2>/dev/null | wc -l | tr -d ' ')
  [ "${up:-0}" != "0" ] && echo "Compose: $up service(s) running (api :8080)."
fi

docs="${FELIX_DOCS_ROOT:-$HOME/Projects/felix-web/apps/docs}"
[ -d "$docs/src/content" ] && echo "Public docs checkout present: $docs/src/content (guide/ + internals/ MDX) — docs-sync skill maps surfaces to pages."
exit 0
