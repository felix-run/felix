#!/usr/bin/env bash
# Roll a GCE + Compose deployment of Felix to a released version, from your machine.
#
#   deploy/gcp/roll.sh 0.5.0            # preflight, backup, then asks before changing anything
#   deploy/gcp/roll.sh 0.5.0 --check    # preflight only: reads, changes nothing
#   deploy/gcp/roll.sh 0.5.0 --yes      # the same roll, answering its confirmations itself
#
# It is docs/UPGRADING.md "The sequence" as one command: check the release and its `-gcp` image
# exist, report the checkout, pin, schema, durable runs in flight, table sizes and disk; back up
# and prove the dump reads back; move the checkout to the tag and pin FELIX_IMAGE_TAG (after
# backing up .env); pull; `docker compose up -d` with the compose files the running stack already
# uses; then wait for /health to report the version. Every step that changes the host asks first,
# and the script stops at the first failure.
#
# The confirmations read the terminal, so a caller with none — CI, an agent's shell, Claude Code's
# `!` prefix — used to reach the first one only after the backup and die there on
# `/dev/tty: Device not configured`. Without --yes that is now refused before anything runs. With
# --yes every confirmation is answered yes and printed as such, except one: durable runs in flight
# stop the roll, because restarting the worker under them is a decision, not a formality.
#
# Needs gcloud (with ssh access to the VM), gh, docker and curl locally, and sudo on the VM.
# The defaults are the reference deployment's; override any of them:
#   FELIX_GCP_PROJECT, FELIX_VM, FELIX_ZONE, FELIX_REPO_DIR, FELIX_BACKUP_DIR, FELIX_HEALTH_URL, FELIX_IMAGE
# FELIX_SSH_FLAGS adds flags to every `gcloud compute ssh`; a CI caller reaching the VM through IAP
# passes `--tunnel-through-iap`.
#
# Exit status: 0 rolled (or --check passed), 75 deferred because durable runs are in flight under
# --yes (nothing changed; try again later), 2 usage, 1 any other stop.
set -euo pipefail

# Only `confirm` reads the terminal. `gh` pages its output when stdout is a tty and waited at
# `(END)` for a keypress; `gcloud compute ssh` and the other tools read stdin and swallowed
# answers typed ahead of a prompt. Both happened on the first real roll (0.5.0).
export GH_PAGER=cat PAGER=cat

# Passed on every gcloud call rather than read from gcloud's default project. The VM's name is
# not unique across projects: after production moved projects (2026-10-03), the old project
# still held a stopped `felix-api` with the same tunnel credentials, and a machine whose default
# still pointed there would have rolled, or started, that one.
PROJECT="${FELIX_GCP_PROJECT:-felix-507018}"
VM="${FELIX_VM:-felix-api}"
ZONE="${FELIX_ZONE:-us-central1-a}"
REPO_DIR="${FELIX_REPO_DIR:-/opt/felix}"
BACKUP_DIR="${FELIX_BACKUP_DIR:-/opt/felix-backups}"
HEALTH_URL="${FELIX_HEALTH_URL:-https://api.felix.run/health}"
READY_URL="${HEALTH_URL%/health}/ready"
WORKER_URL="${HEALTH_URL}/worker"
IMAGE="${FELIX_IMAGE:-ghcr.io/felix-run/felix}"
read -r -a SSH_FLAGS <<<"${FELIX_SSH_FLAGS:-}"

usage() { echo "usage: $0 <version, e.g. 0.5.0> [--check] [--yes]" >&2; exit 2; }
[ $# -ge 1 ] || usage
VERSION="${1#v}"
CHECK_ONLY=0
ASSUME_YES=0
for arg in "${@:2}"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    *) echo "unknown option: $arg" >&2; usage ;;
  esac
done
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a version: $1" >&2; usage; }
TAG="v$VERSION"

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die() { printf '\n\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }
# Opening /dev/tty is the test, not `-t 0`: stdin is redirected on purpose (see above), and a
# process can have a terminal on stdout with none it can open for reading.
have_tty() { { : </dev/tty; } 2>/dev/null; }
confirm() {
  if [ "$ASSUME_YES" = 1 ]; then
    note "$1 — yes (--yes)"
    return 0
  fi
  local reply
  read -r -p "   $1 [y/N] " reply </dev/tty
  [[ "$reply" =~ ^[Yy]$ ]] || die "not confirmed — nothing further was changed"
}

# Before the preflight, not at the first confirmation: by then the backup has been written.
if [ "$CHECK_ONLY" = 0 ] && [ "$ASSUME_YES" = 0 ] && ! have_tty; then
  echo "no terminal to confirm on. Run this from a terminal, or pass --yes to answer every" >&2
  echo "confirmation yes (durable runs in flight still stop it). --check needs neither." >&2
  exit 2
fi

remote() {
  gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --quiet ${SSH_FLAGS[@]+"${SSH_FLAGS[@]}"} \
    --command "$1" </dev/null
}
psql_q() {
  # One query against the deployment's own database, tab-separated, no headers.
  remote "sudo docker exec \$(sudo docker ps --format '{{.Names}}' | grep -m1 postgres) psql -U felix -d felix -tA -F\$'\\t' -c \"$1\""
}

# --- preflight: reads only ------------------------------------------------------------------

bold "Target: $TAG on $VM ($PROJECT, $ZONE)"

bold "Release and image"
gh release view "$TAG" --repo felix-run/felix --json publishedAt -q '"   release published \(.publishedAt)"' </dev/null \
  || die "no GitHub release $TAG"
docker manifest inspect "$IMAGE:$VERSION-gcp" </dev/null >/dev/null 2>&1 \
  || die "image $IMAGE:$VERSION-gcp is not published"
note "image $IMAGE:$VERSION-gcp exists"

bold "Deployment state"
# As root, not as the checkout's owner: earlier rolls ran `sudo git`, so files in the tree are
# root's and a checkout as the owner fails part-way with "unable to unlink old ...: Permission
# denied", leaving the tree half-switched (0.5.0's first roll). `safe.directory` is what lets
# root operate on a checkout it does not own without git refusing it as dubious.
GIT="sudo git -c safe.directory='$REPO_DIR'"
STATE="$(remote "cd '$REPO_DIR' && $GIT describe --tags --always && $GIT status --porcelain | wc -l && sudo grep -E '^FELIX_IMAGE_TAG=' .env || true")"
CURRENT_CHECKOUT="$(sed -n 1p <<<"$STATE")"
DIRTY="$(sed -n 2p <<<"$STATE" | tr -d ' ')"
CURRENT_PIN="$(sed -n 3p <<<"$STATE")"
note "checkout: $CURRENT_CHECKOUT, uncommitted files: $DIRTY"
note "pin: ${CURRENT_PIN:-<none>}"
[ "$DIRTY" = "0" ] || die "$REPO_DIR has $DIRTY changed files. If a previous roll's checkout failed part-way, they are its
      debris and \`$GIT checkout --force $CURRENT_CHECKOUT\` on the VM restores the tree; otherwise commit or remove them"
[ "$CURRENT_CHECKOUT" != "$TAG" ] || note "already on $TAG"
remote "sudo docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'" | sed 's/^/   /'
note "health: $(curl -sS -m 10 "$HEALTH_URL" </dev/null || echo unreachable)"

bold "Schema"
note "alembic: $(psql_q 'table alembic_version')"

bold "Durable runs in flight"
FIBERS="$(psql_q "select id, kind, status, to_timestamp(updated_at/1000)::timestamp(0) from fibers where status in ('pending','running','sleeping') order by updated_at")"
if [ -n "$FIBERS" ]; then
  while IFS= read -r line; do note "$line"; done <<<"$FIBERS"
  note "A run still going across the upgrade can be refused at its next step as drifted"
  note "(the compile-pin hash changes for bundled manifests). Runs last at most 24h."
else
  note "none"
fi

bold "Table sizes (0020_ordering_indexes blocks writes to each while it builds)"
psql_q "select relname, n_live_tup from pg_stat_user_tables where relname in ('audit_events','usage_events','memory_vectors','job_runs','fibers') order by n_live_tup desc" | sed 's/^/   /'

bold "Disk"
remote "df -h / | tail -1" | sed 's/^/   /'

if [ "$CHECK_ONLY" = 1 ]; then
  bold "--check: preflight done, nothing changed"
  exit 0
fi

# --- backup: writes a new file only ---------------------------------------------------------

bold "Backup"
if [ -n "$FIBERS" ]; then
  if [ "$ASSUME_YES" = 1 ]; then
    printf '\n\033[33mDEFERRED: durable runs are in flight (above); --yes does not roll over them. Nothing\n' >&2
    printf 'was changed. Wait for them, or run interactively.\033[0m\n' >&2
    exit 75
  fi
  confirm "Durable runs are in flight (above). Continue anyway?"
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$BACKUP_DIR/felix-pre-$VERSION-$STAMP.dump"
remote "sudo mkdir -p '$BACKUP_DIR' && sudo sh -c \"docker exec \$(sudo docker ps --format '{{.Names}}' | grep -m1 postgres) pg_dump -U felix -d felix -Fc > '$DUMP'\"" \
  || die "backup failed; nothing else has changed (a partial $DUMP may exist and can be deleted)"
# A dump that pg_restore can list is a dump that finished; a truncated one fails here.
TOC="$(remote "sudo sh -c \"docker exec -i \$(sudo docker ps --format '{{.Names}}' | grep -m1 postgres) pg_restore --list < '$DUMP' | grep -c 'TABLE DATA'\"")"
SIZE="$(remote "sudo du -h '$DUMP' | cut -f1")"
[ "${TOC:-0}" -gt 0 ] || die "backup $DUMP did not read back"
note "$DUMP ($SIZE, $TOC tables with data, reads back with pg_restore --list)"

# --- the change -----------------------------------------------------------------------------

bold "Compose files"
FILES="$(remote "sudo docker inspect \$(sudo docker ps --format '{{.Names}}' | grep -m1 -- '-api-') --format '{{index .Config.Labels \"com.docker.compose.project.config_files\"}}'")"
[ -n "$FILES" ] || die "could not read the running stack's compose files"
FLAGS=""
IFS=',' read -r -a PARTS <<<"$FILES"
for f in "${PARTS[@]}"; do FLAGS="$FLAGS -f $f"; done
note "$FILES"
COMPOSE="cd '$REPO_DIR' && sudo docker compose$FLAGS --project-directory ."

confirm "Move $REPO_DIR to $TAG and pin FELIX_IMAGE_TAG=$VERSION? (.env is backed up first)"
remote "cd '$REPO_DIR' && sudo cp .env '.env.bak-pre-$VERSION-$STAMP' \
  && $GIT fetch --tags --quiet && $GIT checkout --quiet '$TAG' \
  && if sudo grep -q '^FELIX_IMAGE_TAG=' .env; then sudo sed -i 's/^FELIX_IMAGE_TAG=.*/FELIX_IMAGE_TAG=$VERSION/' .env; \
     else echo 'FELIX_IMAGE_TAG=$VERSION' | sudo tee -a .env >/dev/null; fi \
  && echo \"   checkout \$($GIT describe --tags) ; \$(sudo grep '^FELIX_IMAGE_TAG=' .env)\"" \
  || die "checkout or pin failed. Nothing was restarted. The tree may be half-switched: on the VM,
      \`cd $REPO_DIR && $GIT checkout --force $CURRENT_CHECKOUT\` restores it, and
      $REPO_DIR/.env.bak-pre-$VERSION-$STAMP restores .env if it was edited"
note ".env backup: $REPO_DIR/.env.bak-pre-$VERSION-$STAMP"

bold "Pull (before the roll, so the outage is only the restart)"
remote "$COMPOSE pull --quiet" \
  || die "pull failed; nothing was restarted, but the checkout and pin are already on $TAG"

confirm "Roll now? migrate runs first, then api/worker/scheduler restart (expect ~1 min of 502s)"
# The running worker's boot id (empty when it has none to give), so the wait below can tell a
# worker this roll started from the one it replaced.
beat_boot() { grep -o '"boot":"[0-9a-f]*"' <<<"$1" | cut -d'"' -f4 || true; }
PRE_BOOT="$(beat_boot "$(curl -sS -m 5 "$WORKER_URL" </dev/null 2>/dev/null || true)")"
remote "$COMPOSE up -d" \
  || die "\`up -d\` failed part-way; check \`docker compose ps\` and the migrate logs on the VM"
ROLLED_SECONDS=$SECONDS

# --- verify ---------------------------------------------------------------------------------

bold "Waiting for $HEALTH_URL to report $VERSION"
for _ in $(seq 1 60); do
  BODY="$(curl -sS -m 5 "$HEALTH_URL" </dev/null 2>/dev/null || true)"
  if grep -q "\"version\":\"$VERSION\"" <<<"$BODY"; then note "$BODY"; break; fi
  sleep 5
done
grep -q "\"version\":\"$VERSION\"" <<<"${BODY:-}" || die "health did not report $VERSION within 5 minutes (last: ${BODY:-none})"

# /health is liveness and touches nothing; /ready probes the database, cache, object store and
# JWKS. A release whose migration left the API unable to reach its own database reports its
# version on the first and 503 on the second.
bold "Waiting for $READY_URL"
for _ in $(seq 1 24); do
  READY="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "$READY_URL" </dev/null 2>/dev/null || true)"
  [ "$READY" = 200 ] && break
  sleep 5
done
[ "${READY:-}" = 200 ] || die "$READY_URL did not answer 200 within 2 minutes (last: ${READY:-none}); the new version is live but not ready"
note "ready"

# /health and /ready are the API's own answers; 0.19.0 passed both with a worker that died at
# startup. A worker beats only once its startup passed, with its version and a boot id, so wait for
# a beat of this version from a worker that is alive after the restart: either a new boot, or the
# same boot beating since (`up -d` leaves a worker whose config did not change running). The old
# worker's last beat is not enough -- it reads ok, with the version a re-roll names, for a minute
# after the worker is gone; but it is older than the restart, and a live worker's is at most 15 s
# old. Ages are the API's clock and elapsed time is this shell's, so no two clocks are compared;
# the 2 s is whole-second rounding on both sides.
worker_verdict() {
  local body="${1%$'\n'*}" code="${1##*$'\n'}" age
  case "$code" in
    200)
      grep -q "\"version\":\"$VERSION\"" <<<"$body" || return
      [ "$(beat_boot "$body")" != "$PRE_BOOT" ] && { echo fresh; return; }
      age="$(grep -o '"last_beat_age_s":[0-9]*' <<<"$body" | cut -d: -f2 || true)"
      [ -n "$age" ] && [ $((age + 2)) -lt $((SECONDS - ROLLED_SECONDS)) ] && echo alive
      ;;
    # Auth runs before routing, so under jwt a release without the route answers 401. 0.19.1 is the
    # last such release; drop this arm once nothing rolls back that far.
    401 | 404) [ "$(printf '%s\n' "$VERSION" 0.19.1 | sort -V | tail -1)" = 0.19.1 ] && echo legacy ;;
  esac
}
bold "Waiting for $WORKER_URL to report a live $VERSION worker"
for _ in $(seq 1 24); do
  WORKER="$(curl -sS -m 5 -w '\n%{http_code}' "$WORKER_URL" </dev/null 2>/dev/null || true)"
  OUTCOME="$(worker_verdict "$WORKER" || true)"
  [ -n "$OUTCOME" ] && break
  sleep 5
done
case "$OUTCOME" in
  fresh) note "worker $VERSION beating (new boot)" ;;
  alive) note "worker $VERSION beating (not restarted: its config did not change)" ;;
  legacy) note "$VERSION has no /health/worker; check the worker's logs by hand" ;;
  *)
    # The body is a Redis value relayed by the API: printable characters only on this terminal.
    die "no live $VERSION worker beat within 2 minutes (last: $(tr -cd '[:print:]' <<<"${WORKER//$'\n'/ }")); check: sudo docker logs \$(sudo docker ps -a --format '{{.Names}}' | grep -m1 worker)"
    ;;
esac

bold "After"
note "alembic: $(psql_q 'table alembic_version')"
remote "sudo docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'" | sed 's/^/   /'
remote "sudo docker logs \$(sudo docker ps -a --format '{{.Names}}' | grep -m1 migrate) 2>&1 | tail -5" | sed 's/^/   migrate: /'

bold "Done: $TAG"
cat <<EOF
   Backup:   $DUMP
   Rollback (image): on the VM, git checkout $CURRENT_CHECKOUT, restore .env from
             $REPO_DIR/.env.bak-pre-$VERSION-$STAMP, then: $COMPOSE up -d
   Rollback (data):  pg_restore from the backup above (docs/BACKUP.md).
   Next: watch the smoke workflow against api.felix.run, and redeploy docs.felix.run.
EOF
