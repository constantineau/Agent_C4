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
