# Off-box archive — the scripts that emptied 22 G off this VPS (2026-09-09)

`gs://constantineau-c4-archive` — COLDLINE, `northamerica-northeast1`, versioning on, a 1-year
**unlocked** retention policy, and deliberately **no lifecycle rule**.

🛑 **Never put anything irreplaceable in `gs://constantineau-vps-backups`.** That bucket has a
lifecycle rule deleting every object at age 30 days. It is right for rotating DreamCRM dumps and
fatal for an archive whose local copy is being deleted — this was caught mid-upload.

## The rule these scripts encode

**An upload that returned 0 is a claim about a backup, not a backup.** Nothing local is deleted
until the remote copy has been streamed back, decompressed and hashed against the sha256 taken
from the original file. Every script prints what it verified.

| script | what it does |
|---|---|
| `archive_bench_to_gcs.sh` | the bench Signal K volume: hash → gzip → decompress → re-hash → upload |
| `archive_pull_to_gcs.sh` | the 2026-08-30 boat pull, per file, + `MANIFEST-sha256.txt`; the ~6,300 small files go as one tarball verified with `tar --diff` against the live tree |
| `verify_restore.sh` | **restores every object from Coldline** and hashes the stream against the manifest. Run this before deleting anything |
| `delete_archived.sh` | deletes named files only after re-hashing the local copy against the manifest AND confirming the GCS object exists. Skips loudly, never guesses |
| `cut_jul15_race.sh` | lifts the Jul 15 race out of the 8.5 G delivery salvage at full resolution so the salvage could be deleted without losing the race |

## What is deliberately still on disk

- `backups/c4-boat-pull-2026-08-30/work/archive-backfill.db` (2.9 G) — `harness.py`'s default
  `--archive`; the replay rig has no source without it.
- `backups/race-data/archive-jul15-race.db` (459 M) — 2,352,414 rows, `22:30 → 00:20Z`. Also
  backfilled into Postgres, which is where the Lab debrief reads.
- `backups/replay-jul18/` (300 M) — the timelines and the materialised spool.

## Restoring

```bash
gsutil cat gs://constantineau-c4-archive/boat-pull-2026-08-30/<path>.zst | zstd -dc > <path>
gsutil cat gs://constantineau-c4-archive/bench/sk_archive-bench-2026-09-09.db.gz | gzip -dc > archive.db
```

`MANIFEST-sha256.txt` in the bucket carries the hash of every source file, so a restore can be
proven rather than assumed. Coldline has a 90-day minimum billing duration and a retrieval fee of
roughly $0.02/GB — the whole bucket is ~5.2 GiB, about two cents a month.

## Nightly Lab + sr33_dev backup (#180)

`lab_backup.py` backs up the Lab's Docker volumes (ingested, learning, retro, playbooks, shares,
plume, coastline, enc — not gribcache, a refillable download cache) and the dev Postgres
(`pg_dump -Fc`, read-only) into one **set** per run: `<dest>/backup-YYYY-MM-DDTHHMMSSZ/`, with
the same file names as the manual `backup-2026-10-31/` (`vps-lab_<vol>.tar.gz`,
`vps-sr33_dev.pgdump`, `MANIFEST-sha256.txt`) plus `INDEX.json`, written last, which is what
makes a set a backup: per-artifact sha256, the sha256 of every file in each volume, the row
counts of every SQLite table, and the row count of every Postgres table as read back out of the
dump. A set without INDEX.json is an unfinished run and nothing treats it as a backup.

- SQLite files in a volume go in as online `.backup()` snapshots, never the live file + WAL.
- Each artifact is checked at the destination after the upload (local: re-hashed; GCS: the
  object's MD5 against ours, no Coldline retrieval fee).
- **Unchanged artifacts are not stored again.** The tarballs are deterministic and the dump's
  content is hashed as plain SQL (the `-Fc` file itself carries the dump time), so an artifact
  whose content matches the previous set's is recorded in INDEX.json as `stored_in` that older
  set. retro + coastline + enc are ~530 MiB of the ~1.1 GiB and almost never change, so a typical night stores only the ~550 MiB dump.
- **Prune** (`C4_BACKUP_PRUNE=1`, `C4_BACKUP_KEEP=7,4,12`): the newest set of each of the last
  7 days, 4 ISO weeks and 12 months, and always the newest. It never deletes an object a kept set
  points at, never touches a name that is not `backup-<stamp>` under the prefix, leaves an
  unfinished set alone for 24 h (it may be running), and refuses a bucket root as destination.

`lab_restore.py` is the drill: it fetches every artifact of a set (default the newest), checks
its sha256, unpacks each volume in a `--rm` container on a tmpfs and checks every file's hash,
every SQLite file's `integrity_check` and row counts, and restores the dump into a fresh
`--rm` TimescaleDB container (same image, no network) and checks every table's row count.
`--into-volume-prefix P` really restores the volumes into new named volumes `P<volume>`.

```bash
# a set into a local directory, then the drill against it
C4_BACKUP_DEST=/mnt/data/c4-backup/local python3 tools/backup/lab_backup.py backup --no-prune
C4_BACKUP_DEST=/mnt/data/c4-backup/local python3 tools/backup/lab_restore.py
python3 tools/backup/lab_backup.py list  --dest gs://constantineau-c4-nightly/vps-nightly
python3 tools/backup/lab_backup.py prune --dest /mnt/data/c4-backup/local --dry-run
```

Scheduling: `systemd/c4-lab-backup.{service,timer}` (02:15 UTC, daily, database dump
included every night) with `/etc/c4-lab-backup.env` from `systemd/c4-lab-backup.env.example`.

### Where the nightlies go (Cole's picks, 2026-11-02)

- **A separate bucket, `gs://constantineau-c4-nightly`, pruning ON (7 daily / 4 weekly /
  12 monthly).** The archive bucket keeps its 1-year retention lock and versioning and gets no
  nightlies: there a delete is refused for a year and then only becomes a billed non-current
  version, so pruning could never work.
- **Storage class STANDARD.** Nearline bills every object for at least 30 days and Coldline for
  90, but most nightly dumps live 7 days, so those minimums would bill each ~550 MB dump for
  30–90 days. The saving per GB doesn't cover that, and both charge a retrieval fee on every
  restore drill. Standard has no minimum and no retrieval fee. At steady state the bucket holds
  ~21 distinct dumps plus the volumes, ~12 GB: about $0.30 a month.
- **Same region as the archive (`northamerica-northeast1`), uniform bucket-level access, public
  access prevention on, no retention policy, no versioning** (a prune must really free space).
- **The database dump stays daily.**
- **No encryption.** The bucket is private (uniform access, public access prevention, only this
  project's credentials), and the data is race telemetry and Lab state, not personal data. A
  client-side key adds one more thing to lose: if the passphrase went with the VPS, every backup
  would be unreadable exactly when it is needed.

### Enable it (commands for Cole)

```bash
# 1. the nightly bucket (once)
gsutil mb -l northamerica-northeast1 -c STANDARD -b on gs://constantineau-c4-nightly
gsutil pap set enforced gs://constantineau-c4-nightly
gsutil ls -L -b gs://constantineau-c4-nightly | grep -iE 'storage class|location|retention|versioning|bucket policy'

# 2. the units (after #198 merges and the main checkout is pulled)
cd /home/constantineau/Agent_C4 && git pull
sudo install -m 644 tools/backup/systemd/c4-lab-backup.service tools/backup/systemd/c4-lab-backup.timer /etc/systemd/system/
sudo install -m 600 tools/backup/systemd/c4-lab-backup.env.example /etc/c4-lab-backup.env
sudo systemctl daemon-reload

# 3. first real run (~10 min), then a restore drill from the bucket (~30 min)
sudo systemctl start c4-lab-backup.service
journalctl -u c4-lab-backup.service -n 30
python3 tools/backup/lab_backup.py list --dest gs://constantineau-c4-nightly/vps-nightly
sudo C4_BACKUP_DEST=gs://constantineau-c4-nightly/vps-nightly python3 tools/backup/lab_restore.py

# 4. schedule it
sudo systemctl enable --now c4-lab-backup.timer
systemctl list-timers c4-lab-backup.timer
```
