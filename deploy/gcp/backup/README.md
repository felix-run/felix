# Nightly backups for a GCE + Compose deployment

[`docs/BACKUP.md`](../../../docs/BACKUP.md) says what holds state and how to copy it. This
directory sets that up as a schedule on a single GCE VM running the Compose stack. The VM is
the [GCE + Compose](../README.md#gce--compose) layout, with Postgres in a container and
`FELIX_OBJECT_STORE=fs` on the `felix-data` volume. It also includes a drill that proves the
copies restore.

Without it, that layout keeps everything on the VM's one disk: the database, the object store,
and the only dumps, which `roll.sh` writes at upgrades. Losing the disk loses all three, and the
recovery point is the last upgrade.

| Layer | What | Recovery point | Survives |
|---|---|---|---|
| 1. Disk snapshots | Daily GCE snapshot of the VM's disk, kept 14 days | ≤ 24h | Lost disk, broken VM, bad upgrade |
| 2. Nightly dump | `pg_dump` + object-store tar → GCS bucket, kept 35 days, undeletable for 7 | ≤ 24h | All of the above, plus a compromised VM (it cannot delete backups) |
| 3. Restore drill | Restores the newest bucket copy into a throwaway Postgres and checks it | — | Turns "we have backups" from a hypothesis into a fact |

| File | Installed at | Does |
|---|---|---|
| `felix-backup` | `/usr/local/sbin/` | One backup: row counts, `pg_dump -Fc`, object-store tar, upload |
| `felix-backup.service`, `.timer` | `/etc/systemd/system/` | Runs it at 03:15 UTC nightly |
| `felix-restore-drill` | `/usr/local/sbin/` | Restores a backup from the bucket into a throwaway container and checks it |
| `lifecycle.json` | the bucket | Deletes objects after 35 days |

The defaults are the reference deployment's, as in `roll.sh`. Everything below runs from your
machine, at the root of a harness checkout. Set these once:

```bash
P=felix-507018 Z=us-central1-a VM=felix-api   # project, zone, VM
B=gs://$P-backups                             # the bucket; the scripts default to this name
```

If the bucket has another name, set `FELIX_BACKUP_BUCKET` in step 4.

## 0. Look first (read-only, ~1 min)

```bash
gcloud compute instances describe $VM --project $P --zone $Z \
  --format='yaml(serviceAccounts,disks[].source,disks[].resourcePolicies)'
gcloud compute resource-policies list --project $P
gcloud compute ssh $VM --project $P --zone $Z --command '
  set -x
  sudo ls -la /opt/felix-backups | tail -5; sudo du -sh /opt/felix-backups
  df -h /; free -m; command -v gcloud; . /etc/os-release; echo $PRETTY_NAME
  sudo crontab -l; systemctl list-timers --all | grep -i felix
  sudo docker ps --format "{{.Names}} {{.Image}}"; sudo docker volume ls | grep felix
  PG=$(sudo docker ps --format "{{.Names}}" | grep -m1 postgres)
  sudo docker exec "$PG" psql -U felix -d felix -Atc \
    "select pg_size_pretty(pg_database_size(current_database())), (select version_num from alembic_version)"
  sudo du -sh "$(sudo docker volume inspect -f "{{.Mountpoint}}" felix_felix-data)"'
```

What to check before going on:

- **`serviceAccounts[].scopes`** must include `cloud-platform` or `devstorage.read_write`. A VM
  that reads Secret Manager almost certainly has `cloud-platform`. Adding a scope means stopping
  the VM for about a minute, so plan that window before step 3.
- **`command -v gcloud`** must print a path. GCE's Debian and Ubuntu images ship it;
  Container-Optimized OS does not, and these scripts assume it.
- **Free disk (`df`)** must hold about 3 × (database + data volume), because the script keeps
  three local nights.
- **`roll.sh`'s `felix-pre-*.dump` files are never pruned.** If they are large, delete the old
  ones by hand.
- **There should be no existing snapshot policy and no `felix` timer or cron.** If either
  exists, reconcile it with this page first.
- **The volume `felix_felix-data`** must be the object store's volume. The Postgres container is
  the one running container whose name contains `postgres`. If either differs, set
  `FELIX_BACKUP_DATA_VOLUME` or `FELIX_BACKUP_PG_CONTAINER` in step 4.

## 1. Daily disk snapshots (~2 min)

```bash
DISK=$(gcloud compute instances describe $VM --project $P --zone $Z \
  --format='value(disks[0].source)' | xargs basename)
gcloud compute resource-policies create snapshot-schedule $VM-daily \
  --project $P --region ${Z%-*} \
  --daily-schedule --start-time 04:00 \
  --max-retention-days 14 --on-source-disk-delete keep-auto-snapshots \
  --storage-location us
gcloud compute disks add-resource-policies "$DISK" --project $P --zone $Z \
  --resource-policies $VM-daily
```

- The snapshot runs at 04:00 UTC, after the 03:15 dump, so each snapshot also holds that night's
  local dump.
- Snapshots are crash-consistent: Postgres replays its WAL on boot, as after a power cut.
- Storage location `us` (multi-region) keeps a copy outside the VM's region. Pick the
  multi-region that contains your zone.

## 2. The bucket (~2 min)

```bash
gcloud storage buckets create $B --project $P \
  --location us --uniform-bucket-level-access --public-access-prevention \
  --retention-period 7d
gcloud storage buckets update $B --lifecycle-file deploy/gcp/backup/lifecycle.json
```

- `--retention-period 7d`: nobody, an owner included, can delete or overwrite an object for 7
  days. The policy is left **unlocked**. Locking it is permanent and can be done later.
- `lifecycle.json` deletes objects after 35 days. Keeping dumps extends the data's lifetime past
  `FELIX_*_RETENTION_DAYS`, so bound the copies too
  ([`docs/BACKUP.md`](../../../docs/BACKUP.md#handling-the-copies)).
- A dump *is* the database: every conversation and tool argument. Grant read access to this
  bucket as narrowly as to the database itself.

## 3. Let the VM write, but not delete (~1 min)

```bash
SA=$(gcloud compute instances describe $VM --project $P --zone $Z --format='value(serviceAccounts[0].email)')
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  gcloud storage buckets add-iam-policy-binding $B --member "serviceAccount:$SA" --role $role
done
```

`objectCreator` + `objectViewer` lets the VM add backups and read them, which the drill needs.
It cannot delete or replace them, so a compromised VM cannot destroy its own backups.

If step 0 showed the scope is missing, add it now. This stops the VM briefly, and the
`--scopes` list must **keep** every scope the VM already has:

```bash
gcloud compute instances stop $VM --project $P --zone $Z
gcloud compute instances set-service-account $VM --project $P --zone $Z --service-account "$SA" \
  --scopes <the existing scopes>,storage-rw
gcloud compute instances start $VM --project $P --zone $Z
```

## 4. Install the nightly backup (~3 min)

```bash
gcloud compute scp --project $P --zone $Z deploy/gcp/backup/felix-backup \
  deploy/gcp/backup/felix-restore-drill deploy/gcp/backup/felix-backup.service \
  deploy/gcp/backup/felix-backup.timer $VM:/tmp/
gcloud compute ssh $VM --project $P --zone $Z --command '
  set -e
  sudo install -m 0755 /tmp/felix-backup /tmp/felix-restore-drill /usr/local/sbin/
  sudo install -m 0644 /tmp/felix-backup.service /tmp/felix-backup.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl start felix-backup.service          # one run now, in the foreground
  sudo journalctl -u felix-backup -n 5 --no-pager
  sudo systemctl enable --now felix-backup.timer
  systemctl list-timers felix-backup.timer --no-pager'
gcloud storage ls -l --project $P "$B/**"
```

**Expected result:** the journal ends with `felix-backup: gs://…/<stamp>/ — dump …, data …`,
and the bucket holds three objects under that stamp: `.meta`, `.dump` and `-data-….tgz`.

**What a run does:**

- records the schema revision, the newest session event, the object store's file count and
  per-table row counts;
- dumps Postgres (`-Fc` is consistent, so the API keeps running) and checks that the dump lists
  `session_events` data;
- tars the object store after the dump, so every row's object is in the tar;
- uploads all three files, then prunes its own local files older than 3 days.

Each file is written to `.part` and renamed on success, so a failed run never leaves something
that looks like a backup. The timer fires at 03:15 UTC ±10 min, and `Persistent=true` catches up
on a night the VM was down.

**Optional, recommended: an alert when a night is missed.** A failed run shows in `journalctl`,
but nobody reads that. A dead-man's switch alerts when no success arrives. Create a check at
healthchecks.io (or any service with the same `/start` and `/fail` URLs) on the schedule
`15 3 * * *` UTC with a 2h grace, then:

```bash
gcloud compute ssh $VM --project $P --zone $Z --command \
  'echo FELIX_BACKUP_PING_URL=https://hc-ping.com/<uuid> | sudo tee /etc/default/felix-backup'
```

`/etc/default/felix-backup` also takes `FELIX_BACKUP_BUCKET`, `FELIX_BACKUP_DIR`,
`FELIX_BACKUP_DATA_VOLUME`, `FELIX_BACKUP_PG_CONTAINER` and `FELIX_BACKUP_KEEP_LOCAL_DAYS`. The
drill is run by hand and reads only the environment, so pass `FELIX_BACKUP_BUCKET`,
`FELIX_BACKUP_DIR` or `FELIX_BACKUP_PG_CONTAINER` to it with `sudo env …` when they are not the
defaults.

## 5. The restore drill (~2 min, then monthly)

```bash
gcloud compute ssh $VM --project $P --zone $Z --command 'sudo felix-restore-drill'
```

It downloads the newest backup **from the bucket**, the copy that survives the VM. It restores
it into a throwaway container running production's Postgres image, with no network and 384 MB
of memory. Then it checks:

- `pg_restore --exit-on-error` finished;
- the schema revision matches what the backup recorded;
- `session_events` is not empty, and its newest event is no older than at backup time;
- every table is present with at least as many rows as recorded (fewer is a warning, because
  the nightly retention sweep can cause it);
- the object-store archive is intact, with at least as many files as recorded.

It prints the download and restore times, the data half of your recovery time, and ends with
`DRILL PASSED` (exit 0) or `DRILL FAILED`. Either way it removes its container and files. To
drill an older backup, pass its stamp: `sudo felix-restore-drill 20261028T031500Z`.

It does not start a harness against the restored database. The fuller drill in
[`docs/BACKUP.md`](../../../docs/BACKUP.md#the-restore-drill) does, and is worth running when
you set the deployment up.

**When to run it:** now, monthly, after any change to the backup, and before an upgrade.

## 6. Keeping an eye on it

- **Weekly:** `gcloud storage ls --project $P $B/ | tail -2`. The newest stamp should be less
  than 24h old.
- **Monthly:** run the drill and note the restore time.
- **`roll.sh` still takes its own pre-upgrade dump.** Keep it: it is the upgrade's rollback, and
  it is newer than last night's.

## Restoring for real

**Take a backup of the current state before restoring over anything**, even a broken one:
`sudo felix-backup`.

### The VM or its disk is gone: from a snapshot (recovery point ≤ 24h, ~15 min)

```bash
SNAP=$(gcloud compute snapshots list --project $P --filter="sourceDisk~$DISK" \
  --sort-by=~creationTimestamp --limit 1 --format='value(name)')
gcloud compute disks create $VM-restored --project $P --zone $Z --source-snapshot "$SNAP"
gcloud compute instances create $VM-2 --project $P --zone $Z \
  --disk name=$VM-restored,boot=yes,auto-delete=no \
  --service-account "$SA" --scopes cloud-platform --machine-type <same as before>
```

- The disk carries the checkout, `.env`, the signing keys and any tunnel credentials, so the
  stack comes back on boot. A hostname served through a tunnel follows the tunnel, not the IP.
- **Stop the old VM if it is still running** before the new one boots, or two copies serve from
  different databases.
- Compare that night's dump with the snapshot's database afterwards: the dump can be up to
  45 min older than the snapshot. A dump restored over it would lose those minutes, not gain
  them.

### Bad data, a bad migration, or a deleted thread: from the dump (~10 min + restore time)

Run these on the VM. `C` must name the compose files the stack runs with: `roll.sh` reads them
from the containers' labels, and the reference deployment uses the three below.

```bash
cd /opt/felix
C="sudo docker compose -f deploy/docker/compose.yml -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml --project-directory ."
PG=$(sudo docker ps --format '{{.Names}}' | grep -m1 postgres)
STAMP=<stamp>; sudo gcloud storage cp "gs://<bucket>/$STAMP/*" /opt/felix-backups/
$C stop api worker scheduler
sudo docker exec "$PG" psql -U felix -d postgres \
  -c 'alter database felix rename to felix_broken' -c 'create database felix owner felix'
sudo docker exec -i "$PG" pg_restore -U felix -d felix --no-owner --exit-on-error \
  < /opt/felix-backups/felix-$STAMP.dump
# The object store: move the current files aside, then unpack the backup's.
D=$(sudo docker volume inspect -f '{{.Mountpoint}}' felix_felix-data)
sudo mv "$D" "$D.broken" && sudo mkdir "$D" && sudo tar xzf /opt/felix-backups/felix-data-$STAMP.tgz -C "$D"
$C up -d
```

- If the dump's schema (the `alembic` line in its `.meta`) is older than the running release,
  either pin `FELIX_IMAGE_TAG` back to the release that wrote it, or let `migrate` bring it
  forward on `up -d`.
- Drop `felix_broken` and `$D.broken` only once the restored service is confirmed working.
- To restore one thread rather than the whole database, `pg_restore` into a scratch database
  and copy the rows across. That is not scripted.

## What this does not cover

- **A recovery point under 24h.** That needs continuous WAL archiving (for example `wal-g` to
  the same bucket).
- **Losing the whole GCP project or account.** The bucket is in the same project. An
  off-provider copy closes that gap, for example a nightly `rclone` to another provider's
  storage.
- **Secrets.** `.env`, the signing keys and tunnel credentials exist only on the disk and in its
  snapshots. Copies in Secret Manager would let you rebuild on a fresh VM without one.
- **The agent workspace** (`FELIX_WORKSPACE_HOST`, the `felix-workspace` volume by default) is
  covered by the snapshots only.
