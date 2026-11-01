# pi/archiver — full-resolution onboard archive (Phase 2)

The boat is the source of truth. The live uplink sends 15-s **aggregates** to the cloud;
this service records **every** Signal K delta at full resolution to a durable local SQLite
DB on the Pi. A dropped Starlink link, a crashed uplink, or a full disk queue never costs
archived data — the archive owns its own Signal K subscription and its own crash-safe store.

```
archiver.py    Signal K WS  ->  SQLite (full res), every (time,source,path,value)
backfill.py    SQLite       ->  cloud /ingest/raw  (resumable, post-passage)
```

## Storage

- SQLite at `ARCHIVE_DB` (default `/var/lib/sr33/archive/archive.db`), on the `sk_archive`
  named Docker volume → **survives container restarts, rebuilds, and host reboots.**
- `PRAGMA journal_mode=WAL` (backfill reads never block the live writer) +
  `PRAGMA synchronous=FULL` (durable across power loss — fsync on each ~2 s flush).
- Schema mirrors the cloud `telemetry_raw(time, boat_id, source, path, value, str_value)`,
  so a backfill is a straight copy. Object values (position, attitude) are flattened into
  dotted numeric sub-paths exactly as the uplink does → identical path naming in the cloud.
- Each row keeps the Signal K **source timestamp** (when the data was measured), falling
  back to receive-time only when a delta carries none.

## Run (bench)

The `archiver` service comes up with the rest of the Pi stack:

```bash
docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build
docker logs -f sr33-pi-archiver-1            # "[archive] +N rows (total …)"
```

Inspect the archive:

```bash
docker compose -f compose.pi.yml exec archiver \
  python -c "import sqlite3;c=sqlite3.connect('/var/lib/sr33/archive/archive.db');\
print(c.execute('select count(*),min(time),max(time) from readings').fetchone())"
```

## When things go wrong

- **Disk guard.** Above `ARCHIVE_DISK_HIGH_PCT` of the archive's filesystem the archiver stops
  writing full-res and logs `!!! DISK …% FULL … FULL-RES RECORDING STOPPED` (repeated every
  10 min). It resumes below `ARCHIVE_DISK_LOW_PCT`. For those two marks the archive's own free
  pages count as free, because a prune frees pages inside the file and `df` never sees it. A
  hard floor ignores that credit: real use at `ARCHIVE_DISK_HARD_PCT` or less than
  `ARCHIVE_DISK_MIN_FREE_MB` really free also stops recording, and it can't resume until the
  real numbers are 2 points and twice the minimum clear of it (the WAL and new pages need real
  bytes, and a reader can stop SQLite reusing its free pages). The uplink's 15-s
  aggregates keep going ashore either way.
- **Restart.** Recording starts within seconds, whatever the size of the archive. Startup runs
  only bounded checks (header, schema, and the right-hand edge of every b-tree — where the last
  writes went), never a whole-file scan: on the SD card a `quick_check` of 24 GB took 39 min.
  The full `quick_check` runs afterwards in a background thread at low priority (nice 19, idle
  I/O class, and at most `ARCHIVE_CHECK_DUTY` of the time). If it finds damage in the live
  file, the file is rotated as below.
- **Corruption.** A corrupt file is moved aside as `archive.corrupt-<stamp>.db` (never deleted;
  `tools/salvage.py` recovers almost all of it) and a fresh one is started. At startup any
  database error from the open, the schema read or the bounded checks counts (also `string or
  blob too big` and UTF-8 decode errors from a damaged page) — except transient ones (locked,
  busy, disk full, can't open), which are retried instead. The moved file is then checked in
  the background and the verdict logged. Mid-run it fires on any write that says the file is
  corrupt, without asking quick_check again. The batch is kept and written to the fresh file.
  If the move itself fails, the batch is requeued and the move retried.
- **Failing writes.** A failed batch is rolled back and requeued in order. The queue is capped
  at `ARCHIVE_MAX_BUFFER_ROWS`; past it the oldest rows are dropped and counted. The drop
  warning prints at most once per `ARCHIVE_DROP_LOG_EVERY_S` with the count since the last one.
- **Prune.** The hourly retention prune deletes in chunks of `ARCHIVE_PRUNE_CHUNK_ROWS`, taking
  the write lock one chunk at a time, so the flusher keeps writing while it runs.
- **Status file** (`archiver-status.json`, rewritten every `ARCHIVE_STATUS_EVERY_S`):
  `recording`, `reason` (`disk_full` / `rotation_pending` / `no_signalk` / `write_failing`),
  `stop_cause` (`high_mark` / `hard_floor`), `disk_used_pct`, `disk_really_used_pct`,
  `disk_free_bytes`, the marks, `paused_since`, the counts of rows not recorded, dropped from
  the buffer, waiting, written, rotations this run, and the background check's state. The
  engine reads it (`shared/archiver_status.py`) on `/session` and `/health`; a file older than
  90 s reads as "archiver stopped". The console's LOG button and a red banner show
  "Not recording: <reason>".

## Backfill to the cloud (post-passage)

Resumable; a `sync_state` cursor tracks the highest row id the cloud has accepted, so a
re-run only sends what's new.

```bash
docker compose -f compose.pi.yml exec archiver python backfill.py
# ad-hoc re-export of a window (does not touch the cursor):
docker compose -f compose.pi.yml exec archiver \
  python backfill.py --since 2026-06-16T00:00:00Z --until 2026-06-16T23:59:59Z
# re-send everything from the start:
docker compose -f compose.pi.yml exec archiver python backfill.py --reset
```

On the boat, point `VPS_URL`/`INGEST_TOKEN` at the production ingestion API and run this
once a real link is available (dockside wifi after the passage).

## Env

| Var | Default | Meaning |
|-----|---------|---------|
| `SIGNALK_WS` | `ws://localhost:3010/signalk/v1/stream?subscribe=all` | Signal K delta stream |
| `BOAT_ID` | `sr33` | boat id stamped on every row |
| `ARCHIVE_DB` | `/var/lib/sr33/archive/archive.db` | SQLite path (on the named volume) |
| `ARCHIVE_FLUSH_ROWS` / `ARCHIVE_FLUSH_SECONDS` | `1000` / `2` | flush cadence (durability vs. throughput) |
| `VPS_URL` / `INGEST_TOKEN` | `http://localhost:8101` / `dev-ingest-token` | backfill target |
| `BACKFILL_BATCH` | `1000` | readings per cloud POST |
| `ARCHIVE_DISK_HIGH_PCT` / `ARCHIVE_DISK_LOW_PCT` | `85` / `80` | disk guard: stop full-res at the high mark, resume below the low one |
| `ARCHIVE_DISK_HARD_PCT` / `ARCHIVE_DISK_MIN_FREE_MB` | `95` / `2048` | hard floor on REAL filesystem use / free space; no free-page credit, stops recording on its own |
| `ARCHIVE_DISK_CHECK_S` / `ARCHIVE_DISK_NAG_S` | `30` / `600` | how often disk use is measured / how often a stopped archiver repeats its warning |
| `ARCHIVE_MAX_BUFFER_ROWS` | `300000` | cap on rows waiting in RAM while writes fail (~10 min at the dock); the oldest past it are dropped and counted |
| `ARCHIVE_STATUS_FILE` | `archiver-status.json` next to the DB | the archiver's health as JSON (see above) |
| `ARCHIVE_STATUS_EVERY_S` | `10` | how often the status file is rewritten |
| `ARCHIVE_CHECK_DUTY` | `0.5` | share of wall time the background quick_check may spend reading |
| `ARCHIVE_DROP_LOG_EVERY_S` | `30` | at most one buffer-drop / requeue warning per this many seconds |
| `ARCHIVE_PRUNE_CHUNK_ROWS` / `ARCHIVE_PRUNE_PAUSE_S` | `20000` / `0.05` | prune batch size / pause between batches (lock released) |
