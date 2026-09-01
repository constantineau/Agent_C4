# C4 telemetry consolidation — progress (updated 2026-09-01)

Goal (Cole, this session): **lose no telemetry**, and **copy all telemetry off the Pi to the VPS**.
Deletion from the boat is allowed only *after* an off-boat copy is sha256-verified.

## Status at a glance

| # | Item | State |
|---|------|-------|
| 1 | Jul 18 race backfill → Postgres | ✅ done 2026-08-30 |
| 2 | NUL-strip fix in ingestion | ✅ deployed + verified 2026-09-01 |
| 3 | Boat cleanup, the verified 2.1 GB | ✅ done 2026-08-30 |
| 4 | Pre-race remainder, 5,481,959 rows | ⬜ not started |
| 5 | Re-pull of the 9.6 GB corrupt archive | ✅ VERIFIED 2026-08-30; salvage still to do |
| 6 | Drain the Pi's live `archive.db` | ⬜ not started — **biggest open problem, 16.2 GB** |
| 7 | Uplink spool | ✅ root cause fixed, drained to 0, reconciles exactly |
| 8 | `ARCHIVE_RETAIN_DAYS=14` prune | ⬜ open — **starts deleting ~2026-09-13** |
| 9 | Jul 18 duplicate rows | ✅ deduped 2026-09-01, reversible |
| 10 | derived-data double-emit | ✅ fixed on the boat 2026-09-01 |
| 11 | Live-archive reads race the archiver | ℹ️ caveat for #6 |

**Uncommitted on branch `dev`** (all verified, none committed — the running containers are
built from this working tree, so `git checkout` would diverge image from source):
`vps/ingestion/app/main.py`, `pi/uplink/uplink.py`, `pi/uplink/test_uplink_queue.py`,
`pi/signalk/patch-windground-path.sh`, `compose.pi.yml`, this file.

## DONE

### 1. Jul 18 race backfill → Postgres ✅
`4,963,504` readings + `51` sail-log entries + `2` session markers, into `telemetry_raw`
on `sr33-dev-timescaledb-1` (this box's dev stack — confirmed it is the live target, its
`max(time)` tracks current boat telemetry).

- Ran `pi/archiver/backfill.py` in **session mode** (the designed path: scopes to engine
  race windows, and also pushes the sail log + `crew.session` markers the debrief needs).
- Ran against a **working copy** at `backups/c4-boat-pull-2026-08-30/work/archive-backfill.db`,
  because `open_db()` opens read-write and would have converted the pristine
  `recovered/archive-recovered.db` to WAL. Pristine artifact untouched.
- Reconciled exactly: `66,692` pre-existing + `4,963,504` backfilled + `28` crew rows in
  window = `5,030,224`. No duplication.
- Window sent: `2026-07-18T17:03:31Z` → `20:40:30Z`.

⚠️ **`telemetry_raw` has no unique constraint or PK.** A re-run silently duplicates.
Rollback handle: `telemetry_raw_prebackfill_20260830` (774,807 rows, Jul 17–18 as it was
before this work). Drop it once you're satisfied.

### 3. Boat cleanup — the verified 2.1 GB ✅
`archive.corrupt-20260830.db` (+ `-shm`/`-wal`) deleted from the Pi. Hash verified
identical on both sides first (`809ad7d…a32f`). Archiver unaffected, still `Up`.

### 5. Re-pull of the 9.6 GB `archive.corrupt-20260718.db` ✅ (finished 2026-08-30T21:51Z)
`repull-9g.sh` completed unattended: rsync exit=0, both sides sha256
`622aec8bf08016487747bff34fffe5ebe02f9422725c5623c29d63b0611a321d`, log says `VERIFIED`.
Local copy is the full `9,613,660,160` bytes at
`backups/c4-boat-pull-2026-08-30/pi/sk_archive/archive.corrupt-20260718.db`.
**It is now safe to delete from the boat** (still present there as of 2026-09-01) — that
would free 9.6 GB of the Pi's 115 G card.

Still to do with it: salvage via `salvage.py` (it is corrupt too — pre-Jul-18 history),
then backfill. **Blocked on local disk, see "Disk" below.**

### 2. NUL-strip fix ✅ **DEPLOYED 2026-09-01**
`docker compose -f compose.dev.yml build ingestion && ... up -d ingestion` — image rebuilt,
`sr33-dev-ingestion-1` recreated, live boat telemetry resumed (lag ~33 s after restart).

Verified end-to-end, not just by unit test: POSTed a 3-reading batch to
`/ingest/raw` whose middle reading carried the NUL-interleaved `WEDNESDAY`. Response
`{"accepted":3}` and all three rows landed, `str_value` = `WEDNESDAY`. Pre-fix that batch
would have aborted whole. The three `source='nultest'` rows were deleted afterwards.

⚠️ The change is **still uncommitted** on branch `dev` (`vps/ingestion/app/main.py`). The
running container is built from the working tree, so a `git checkout` would silently
diverge image from source. Commit it.

Two corrections to the previously recorded diagnosis:
- The spool files contain **no literal NUL bytes**. They carry the JSON escape `\u0000`,
  which only becomes a real NUL after parsing. 15 files, as recorded.
- The payload is a **UTF-16LE leak from an N2K device**:
  `'W\x00E\x00D\x00N\x00E\x00S\x00D\x00A\x00Y'`. Stripping recovers `WEDNESDAY` rather
  than discarding it.
- The **recovered Jul 18 archive is clean** — 0 NULs in 731,076 str_values. That is why
  the race backfill was safe to run before this fix.

## ITEMS 4–11 (see the table above for state — this section is not ordered by status)

### 4. Pre-race remainder — `5,481,959` rows ⬜
Session mode deliberately excluded these (`Jul 17 22:51Z → Jul 18 17:03:31Z`, the delivery
to the start) per the documented "a day sail or delivery never leaves the boat" rule.
**Cole has since asked for all telemetry on the VPS, so these should go up.** Ad-hoc mode,
which never touches the cursor:
```bash
cd /home/constantineau/Agent_C4/pi/archiver
export $(grep -E '^(INGEST_TOKEN|BOAT_ID)=' ../../.env | xargs)
ENGINE_DB=.../work/engine.db ARCHIVE_DB=.../work/archive-backfill.db \
VPS_URL=http://localhost:8101 BACKFILL_BATCH=2000 \
<venv>/bin/python backfill.py --since 2026-07-17T22:51:08Z --until 2026-07-18T17:03:31Z
```
Careful: ad-hoc mode restarts from id 0 and does not record a cursor, so it is **not**
re-run-safe. Run once.

### 6. Drain the Pi's live `archive.db` — **now the biggest problem**
As of 2026-09-01 14:03 it is `16,191,725,568` bytes. It was 3.6 GB on Aug 30, so the
~6.3 GB/day growth rate held. Never yet drained to the VPS.

**The pull-then-backfill pattern used for the other files no longer works here**: only
~21 G is free locally (see Disk), the file is 16 GB, and at ~3 MB/s over the uplink a pull
takes ~1.5 h during which it grows another ~0.4 GB. This needs a *streaming* drain —
backfill straight from the Pi to the VPS in windows, with a cursor — not a file copy.

### 7. Reconcile the uplink spool ✅ **DONE 2026-09-01 — root cause fixed, spool fully drained**

**The spool was never a "reconcile what's missing" job. The queue was jammed.**
`_flush_queue()` walked the spool oldest-first and `return`ed on *any* exception. On
2026-07-19 07:28 a **zero-byte** spool file was written (interrupted write during the
Starlink/power failure). From that moment the queue head raised `JSONDecodeError` on every
pass, so **nothing behind it was ever sent** — that, not the NUL bug, is why Jul 19–21 is
missing from Postgres. Six weeks of stall from one empty file.

17 zero-byte files exist, all Jul 19–20, identical set on the boat and in the local
snapshot. All are **truly empty — 0 bytes, no telemetry in them**, so clearing them loses
nothing.

**Fix in `pi/uplink/uplink.py` (uncommitted, branch `dev`):**
- `_flush_queue` now separates *transient* (link down, HTTP 5xx → stop, keep everything)
  from *permanent* (unreadable file, HTTP 4xx → move to `quarantine/`, keep draining).
- `_enqueue` writes to a temp name and `os.replace`s it in — atomic, so a power cut can no
  longer create the zero-byte file that caused this.
- Regression suite `pi/uplink/test_uplink_queue.py` — **10 pass on the fix, 4 fail against
  `git show HEAD:` code**, so it genuinely pins the bug.

**Deployed to the boat 2026-09-01.** The Pi runs its own clone at `~/Agent_C4` on branch
**`main`@a1958b7`** — a *different branch and head* from this box's `dev`@31c6b27. Going
through git would have switched the boat's branch and dragged in unrelated changes, so the
single file was copied instead, after confirming the Pi's `uplink.py` was byte-identical to
this box's pre-fix version. Original backed up on the Pi at `/tmp/uplink.py.orig-20260901`.
```bash
scp pi/uplink/uplink.py sr33-pi@100.79.180.102:/tmp/   # then cp into ~/Agent_C4/pi/uplink/
ssh sr33-pi@… 'cd ~/Agent_C4 && docker compose -f compose.pi.yml build uplink && \
                docker compose -f compose.pi.yml up -d uplink'
```
The fix quarantined all 17 poison files by itself — no manual `mv` was needed.

**Drain result — complete, and it reconciles exactly:**
| | |
|---|---|
| spool files at start | 5,959 → **0** |
| quarantined (all 0-byte) | **17** |
| Jul 19 rows in Postgres | 0 → **344,436** |
| Jul 20 rows in Postgres | 0 → **62,555** |

344,436 / 62,555 match the pre-drain file survey **exactly**, so every readable spool
reading landed and nothing was lost. Took ~14 min at ~370 files/min.

### 9. Jul 18 duplicates — investigated 2026-09-01, **decision still open**
Postgres has **9,835 duplicated (time, source, path) tuples / 9,836 excess rows** on Jul 18,
spanning `17:03:31.963Z → 20:40:30.353Z`.

**They did not come from the spool replay** — that was the first guess and it was wrong.
Traced back to the source artifacts: the recovered archive DB *already contains* **9,655
excess rows** in that same window, i.e. **98% of the duplication was in the boat's own
archive** and arrived with the Aug 30 race backfill. The spool holds nothing at those
millisecond timestamps (it samples one reading per (source,path) per 15 s window, so it
rarely lands on an archive timestamp); it accounts for only the ~181 remainder.

**Cause: a double-emit by the derived producers, not by any ingest path.**
- 9,595 of 9,655 are `derived-data / environment.wind.directionTrue`; the rest are
  `derived-data` wind speed/angle and `course-provider` bearings.
- The two copies sit **3–44 rowids apart** — same or adjacent write batch, not a replay.
- Same timestamp, different value ⇒ the producer recomputed from a different input
  snapshot and stamped both with the same source time. Both values are legitimate.
- Raw N2K sensor sources are essentially unaffected (single-digit dup counts), so the
  measured data is clean; only *derived* quantities are doubled.

**Deduped 2026-09-01.** Kept the first-emitted copy of each (time, boat_id, source, path)
across all of Jul 18: **9,836 rows removed, 0 duplicate tuples left, 5,306,588 → 5,296,752,
all 136 paths preserved.** Every removed row was copied first to
**`telemetry_raw_dupes_removed_20260901`** (9,836 rows), so this is fully reversible and
nothing is actually lost. Keyed the delete on `(tableoid, ctid)` — `ctid` alone is only
unique within a hypertable chunk, so a plain-`ctid` delete would be wrong if Jul 18 ever
spans more than one chunk (it currently spans exactly 1).

### 10. Derived double-emit ✅ **FIXED ON THE BOAT 2026-09-01**
**Two plugin calcs write the same path.** In `signalk-derived-data`, both
`dist/calcs/windDirection.js` (option `directionTrue`) and `dist/calcs/windGround.js`
(option `groundWind`) publish **`environment.wind.directionTrue`**, and
`pi/signalk/derived-data.json` enables **both**. They compute different quantities —
`directionTrue` is water-referenced (`headingTrue + angleTrueWater`), `groundWind` is
ground-referenced — but they land on one path under one `$source`, so nothing downstream
can tell them apart. The difference between them is current + leeway, which is exactly the
0.02–0.05 rad spread seen in the Jul 18 duplicates.

**It is still happening right now**: 78 duplicate `derived-data/environment.wind.directionTrue`
tuples in the last 40,000 rows of the Pi's live `archive.db` (2026-09-01).

**Postgres hides it, and that is the worse problem.** Live data reaches the VPS via the
*uplink*, whose `record()` keeps only the latest value per (source, path) per 15 s window —
so it silently collapses the pair and **arbitrarily publishes either the water- or the
ground-referenced TWD, depending on arrival order**. That is why live Postgres shows zero
duplicates while the archive shows plenty. So `environment.wind.directionTrue` in Postgres
is not a consistent quantity — it is a nondeterministic mix. Backfills from the *archive*
(like Jul 18) expose the pair instead of hiding it.

**Fix applied: the ground calc got its own path** (chosen over disabling `groundWind`, which
would have cost `angleTrueGround` + `speedOverGround`).
`pi/signalk/patch-windground-path.sh` repoints **only** `windGround.js` onto
**`environment.wind.directionTrueGround`**, pairing with the `angleTrueGround` it already
emits, and leaves `environment.wind.directionTrue` to the water-referenced calc.

- Idempotent, keeps `windGround.js.orig`, and **verifies then self-reverts** if the sed
  misses. Tested locally against the real file (2 occurrences changed, deprecated
  `directionGround` block untouched, `node --check` clean, second run is a no-op).
- Wired into the `signalk-derived` service in `compose.pi.yml` so it re-applies on every
  boot — the plugin is `npm install`ed only if absent, so a fresh `sk_config` volume would
  otherwise silently restore the double-emit.
- **Safe for consumers**: `environment.wind.directionTrue` is read as "twd" in 11 places
  (navigator/tactics/trend/fatigue/plangap/summarizer/engine) and now returns a *consistent*
  water-referenced value instead of a coin-flip. `angleTrueGround` and
  `wind.speedOverGround` have **zero** consumers in the repo, so the new path is additive.

**Verified after a `docker restart sr33-pi-signalk-1`:**
- Postgres now shows `environment.wind.directionTrueGround` as its own path alongside
  `directionTrue`.
- Live `archive.db`: **0 duplicate tuples** in the last 8,000 rows (was ~16 per 8,000).

Pi originals kept: `/tmp/uplink.py.orig-20260901`, `/tmp/compose.pi.yml.orig-20260901`,
and `windGround.js.orig` inside the plugin dir.

### 11. Reading the live `archive.db` is unreliable while the archiver writes
Probing the live 16 GB DB from a second connection intermittently raises
`sqlite3.DatabaseError: database disk image is malformed` — the *same* query over the *same*
rowid range fails on one pass and succeeds on the next, and a later pass read 100,000 rows
clean across the window that had failed. It is **not** persistent corruption: the archiver
has `RestartCount 0` and is appending 750–950 rows/batch continuously.

It is a reader racing WAL writes/checkpointing. **Consequence for #6:** a drain must not
treat `DatabaseError` as corruption — it needs retries, or better, should read from a
consistent snapshot (SQLite backup API / `VACUUM INTO`) rather than the live file. Without
that it will look like the July corruption and trigger a pointless salvage.

### 8. ⚠️ `ARCHIVE_RETAIN_DAYS=14` is an active deletion mechanism
The archiver prunes out-of-session readings older than 14 days. No prune has run yet — the
DB was recreated 2026-08-30, so nothing is old enough — but it will start deleting
Aug-30-onward out-of-session data around **2026-09-13**. Under a "lose nothing" policy,
either raise/disable retention or get the drain (#6) working before then.

## Disk — the binding constraint (2026-09-01)
Local `/` is 96 G, **21 G free** (75 G used). Boat card is 115 G, 75 G free.
Consumers already on disk under `backups/c4-boat-pull-2026-08-30/`:
- `pi/sk_archive/archive.corrupt-20260718.db` 9.6 GB — keep, not yet salvaged
- `pi/sk_archive/archive.db` 2.1 GB (the Jul 19 snapshot)
- `recovered/archive-recovered.db` 2.0 GB — pristine Jul 18 artifact, already in Postgres
- `work/archive-backfill.db` 3.1 GB — working copy; **still needed for task #4**, deletable after

Salvaging the 9.6 GB writes a second ~9 GB file → would leave ~12 G free before any
Postgres growth. Sequence disk-hungry work deliberately; `df` before each step.

## Environment notes
- Backfill needs `websockets` (via `archiver` import) which is **not** installed on this
  host. A venv was built in the session scratchpad; that path is ephemeral — recreate with
  `python3 -m venv && pip install websockets pytest`.
- Postgres is `sr33-dev-timescaledb-1`, creds from `.env`:
  `docker exec sr33-dev-timescaledb-1 psql -U sr33 -d sr33_dev`.
- Pi paths are docker volumes, root-owned — reach them with `sudo` and absolute paths
  (`cd` into them fails): `/var/lib/docker/volumes/sr33-pi_sk_queue/_data`,
  `/var/lib/docker/volumes/sr33-pi_sk_archive/_data`.
- **Write tests into the repo, not the scratchpad.** The Aug 30 session recorded the NUL
  validator as "unit-tested and passing" but the test was ephemeral and is gone; it had to
  be re-verified from scratch on 2026-09-01.
- SSH to the boat is `ssh sr33-pi@100.79.180.102` (Tailscale SSH, passwordless sudo).
  Orin is `agent-c4@100.70.110.72`. **Not** `constantineau@`.
- Local disk: 96 G, was down to ~28 G free before the 9.6 GB pull. The pull plus salvage
  plus the Postgres growth from remaining backfills will be tight — **watch `df`**.
