#!/usr/bin/env bash
# Prove that a resume stream on one replica sees an append made on another.
#
# This is the only property of the harness that a single replica cannot exercise. On
# one process the in-process waiter answers first, so `session/notify.py`'s Redis path
# is never consulted and a completely broken pub/sub fan-out looks identical to a
# working one.
#
# The trick that makes the result mean something is the poll floor. A resume stream
# polls every second by default, so it would pick up a cross-replica append within a
# second whether or not it was ever notified -- a passing test would prove nothing.
# This runs the stack with a 30 s floor, so anything arriving promptly can only have
# arrived by notification.
#
#   ./scripts/smoke-replicas.sh              # boots, proves, tears down
#   KEEP=1 ./scripts/smoke-replicas.sh       # leave the stack up afterwards
#
# Uses its own project name and ports, so it will not collide with `make up`.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-felix-replica-smoke}"
COMPOSE=(docker compose -p "$PROJECT"
  -f deploy/docker/compose.yml -f deploy/docker/compose.replicas.yml
  --project-directory .)

export FELIX_PORT="${FELIX_PORT:-8097}"
export FELIX_PG_PORT="${FELIX_PG_PORT:-55480}"
export FELIX_VALKEY_PORT="${FELIX_VALKEY_PORT:-55481}"
export FELIX_BIND_ADDR=127.0.0.1
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-unused-by-this-smoke}"
# The whole point: slow enough that polling cannot be the explanation.
export FELIX_STREAM_RESUME_POLL_SECONDS="${FELIX_STREAM_RESUME_POLL_SECONDS:-30}"
export FELIX_STREAM_RESUME_POLL_MAX_SECONDS=60

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  POSTGRES_PASSWORD="$(grep -m1 '^POSTGRES_PASSWORD=' .env 2>/dev/null | cut -d= -f2- || true)"
  [[ -n "$POSTGRES_PASSWORD" ]] || { echo "set POSTGRES_PASSWORD (or run scripts/dev-key.sh)" >&2; exit 1; }
  export POSTGRES_PASSWORD
fi
if [[ -z "${FELIX_AUTH_API_KEYS:-}" ]]; then
  FELIX_AUTH_API_KEYS="$(grep -m1 '^FELIX_AUTH_API_KEYS=' .env 2>/dev/null | cut -d= -f2- || true)"
  [[ -n "$FELIX_AUTH_API_KEYS" ]] || { echo "run 'make dev-key' first" >&2; exit 1; }
  export FELIX_AUTH_API_KEYS
fi

cleanup() { [[ -n "${KEEP:-}" ]] || "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1. Booting two replicas behind one origin"
# --build matters: without it Compose reuses whatever `felix:latest` happens to be,
# which may predate the code under test entirely.
"${COMPOSE[@]}" up -d --build

say "2. Migrating (directly, not through any pooler)"
"${COMPOSE[@]}" exec -T -e FELIX_DATABASE_URL="postgresql+psycopg://felix:${POSTGRES_PASSWORD}@postgres:5432/felix" \
  api felix migrate head >/dev/null
echo "   schema at head"

A="$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=api" --format '{{.Names}}' | sort | head -1)"
B="$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=api" --format '{{.Names}}' | sort | tail -1)"
[[ "$A" != "$B" ]] || { echo "only one api replica is running" >&2; exit 1; }
echo "   replica A: $A"
echo "   replica B: $B"

KEY="$(printf '%s' "$FELIX_AUTH_API_KEYS" | python3 -c 'import json,sys; print(next(iter(json.load(sys.stdin))))')"
THREAD="smoke-$$-$(date +%s)"
EFFECTIVE="default:$THREAD"

say "3. Does the origin actually use both replicas?"
for _ in $(seq 1 20); do
  # No credential on purpose: /ready is a probe path and kubelet sends none.
  curl -fsS -o /dev/null --max-time 10 "http://127.0.0.1:${FELIX_PORT}/ready"
done
ORIGIN="$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=origin" --format '{{.Names}}' | head -1)"
docker logs "$ORIGIN" 2>&1 | grep -oE 'upstream=[0-9.]+:[0-9]+' | sort | uniq -c | sed 's/^/   /'
distinct="$(docker logs "$ORIGIN" 2>&1 | grep -oE 'upstream=[0-9.]+:[0-9]+' | sort -u | wc -l | tr -d ' ')"
[[ "$distinct" -ge 2 ]] || { echo "the origin only ever used one replica" >&2; exit 1; }

say "4. Reader on A, writer on B"
docker exec -d "$A" python -c "
import httpx, time
with open('/tmp/smoke-stream.txt','w',buffering=1) as f:
    with httpx.stream('GET','http://127.0.0.1:8080/chat/stream/${THREAD}',
                      headers={'Authorization':'Bearer ${KEY}'}, timeout=180.0) as r:
        f.write(f'{time.time():.3f} status={r.status_code}\n')
        for line in r.iter_lines():
            f.write(f'{time.time():.3f} {line}\n')
"
# Wait for the subscription to exist server-side rather than sleeping a guess: Redis
# pub/sub drops anything published before the subscribe lands.
VALKEY="$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=valkey" --format '{{.Names}}' | head -1)"
for _ in $(seq 1 100); do
  docker exec "$VALKEY" valkey-cli pubsub channels 2>/dev/null | grep -q "$THREAD" && break
  sleep 0.2
done
docker exec "$VALKEY" valkey-cli pubsub channels 2>/dev/null | grep -q "$THREAD" \
  || { echo "replica A never subscribed — nothing after this would mean anything" >&2; exit 1; }
echo "   A is subscribed to felix:thread:default:$EFFECTIVE"

WROTE="$(docker exec "$B" python -c "
import asyncio, time
from felix.config import get_settings
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent
async def main():
    s = get_session_store(get_settings(), tenant_id='default').open('${EFFECTIVE}')
    await s.append_batch([AppendableEvent(kind='message', role='user', content='written on replica B')])
    print(f'{time.time():.3f}')
asyncio.run(main())
" | tail -1)"
echo "   B appended at $WROTE"

say "5. Did it cross?"
for _ in $(seq 1 50); do
  docker exec "$A" grep -q 'written on replica B' /tmp/smoke-stream.txt 2>/dev/null && break
  sleep 0.2
done
docker exec "$A" grep -q 'written on replica B' /tmp/smoke-stream.txt 2>/dev/null || {
  echo "   NOT DELIVERED — the cross-replica wake did not work" >&2
  docker exec "$A" cat /tmp/smoke-stream.txt >&2 || true
  exit 1
}
SAW="$(docker exec "$A" sh -c "grep -m1 'written on replica B' /tmp/smoke-stream.txt" | awk '{print $1}')"
python3 - "$WROTE" "$SAW" "$FELIX_STREAM_RESUME_POLL_SECONDS" <<'PY'
import sys
wrote, saw, poll = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
lag = saw - wrote
print(f"   delivered {lag * 1000:.0f} ms after the append, against a {poll:.0f}s poll floor")
if lag >= poll:
    print("   INCONCLUSIVE: slow enough that a poll could explain it", file=sys.stderr)
    raise SystemExit(1)
print("   Polling cannot explain this. The Redis notification carried it across replicas.")
PY

say "PASS"
