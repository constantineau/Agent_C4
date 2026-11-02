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
python3 tools/backup/lab_backup.py list  --dest gs://constantineau-c4-archive/vps-nightly
python3 tools/backup/lab_backup.py prune --dest /mnt/data/c4-backup/local --dry-run
```

Scheduling: `systemd/c4-lab-backup.{service,timer}` (02:15 UTC) with `/etc/c4-lab-backup.env`
from `systemd/c4-lab-backup.env.example`.

### Retention in the bucket

`constantineau-c4-archive` has a 1-year retention policy and versioning. A delete of an object
younger than a year is refused, and after that a delete only turns it into a non-current version,
which is still billed until a lifecycle rule removes it. So the shipped env file sets
`C4_BACKUP_PRUNE=0`, and the bucket keeps every stored set for at least a year. Pruning there
only saves money once Cole adds a lifecycle rule for non-current versions (or picks another
bucket for the nightlies).
