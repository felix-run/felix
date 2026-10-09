#!/usr/bin/env bash
# Score an agent against a real model: `felix eval` with no `--mock`, in-process on this checkout.
#
#   scripts/eval-live.sh <fixture.json> <manifest> [extra felix eval flags]
#
# The model key comes from the environment (FELIX_ANTHROPIC_API_KEY, or whichever provider the
# manifest's model routes to) — this is the one eval path that spends money, which is why it
# runs from `.github/workflows/eval-live.yml` on a schedule and never on a pull request.
#
# Stores are in-memory and the workspace is this checkout, so `contributor` reads the code it is
# being scored on. Its shell tool and GitHub MCP do not bind here (no allowlist, no token); they
# log a warning and the agent runs with its file and skill tools, which is all its items need.
#
# Writes the run record to eval-live-<dataset>.json, appends a table to $GITHUB_STEP_SUMMARY
# when set, and exits with `felix eval`'s code: 0 all passed, 1 an item failed, 2 a bad dataset.
set -uo pipefail

FIXTURE="${1:?usage: scripts/eval-live.sh <fixture.json> <manifest> [flags]}"
MANIFEST="${2:?usage: scripts/eval-live.sh <fixture.json> <manifest> [flags]}"
shift 2
DATASET="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$FIXTURE")"
OUT="eval-live-${DATASET}.json"

if [ -z "${FELIX_ANTHROPIC_API_KEY:-}${FELIX_OPENAI_API_KEY:-}${FELIX_MODEL_PROVIDER_OPTIONS:-}" ]; then
  echo "eval-live: no model credentials in the environment; this run would score nothing but 401s" >&2
  exit 2
fi

export FELIX_DATABASE_URL=memory://eval-live
export FELIX_OBJECT_STORE=memory
export FELIX_AUTH_MODE=none
export FELIX_HOST=127.0.0.1
export FELIX_ALLOW_INSECURE=true
export FELIX_REDIS_URL="${FELIX_REDIS_URL:-redis://127.0.0.1:9/0}"
export FELIX_MEMORY_EMBEDDER="${FELIX_MEMORY_EMBEDDER:-none}"
export FELIX_WORKSPACE_ROOT="${FELIX_WORKSPACE_ROOT:-$PWD}"
# `contributor` and `triage` are not bundled; they live in manifests/self (README).
export FELIX_MANIFESTS_DIR="${FELIX_MANIFESTS_DIR:-$PWD/manifests/self}"

uv run --no-sync felix eval --dataset "$DATASET" --manifest "$MANIFEST" --fixture "$FIXTURE" "$@" >"$OUT"
code=$?

python3 - "$OUT" "$MANIFEST" "$code" <<'PY'
import json, os, sys

path, manifest, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    run = json.load(open(path))
except (OSError, ValueError):
    print(f"eval-live: no run record in {path} (exit {code})", file=sys.stderr)
    sys.exit(0)
stats = run.get("stats") or {}
total = run.get("pass_count", 0) + run.get("fail_count", 0)
lines = [
    f"### `{run.get('dataset_name')}` vs `{manifest}`: {run.get('pass_count', 0)}/{total} passed",
    "",
    f"Cost ${stats.get('cost_usd', 0):.4f} · {stats.get('tokens_input', 0) + stats.get('tokens_output', 0)} tokens"
    f" · {stats.get('tool_calls', 0)} tool calls · judge fallbacks {stats.get('judge_fallbacks', 0)}",
    "",
    "| item | verdict | rule | why |",
    "|---|---|---|---|",
]
for s in run.get("scores", []):
    verdict = "error" if s.get("error") else ("pass" if s.get("pass") else "**fail**")
    why = (s.get("error") or s.get("reason") or s.get("answer") or "").replace("|", "\\|").replace("\n", " ")[:160]
    lines.append(f"| `{s.get('item_id')}` | {verdict} | {s.get('rule', '')} | {why} |")
text = "\n".join(lines) + "\n"
print(text)
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a") as fh:
        fh.write(text + "\n")
PY
exit "$code"
