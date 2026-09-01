# C4 telemetry consolidation — progress (updated 2026-09-01)

Goal (Cole, this session): **lose no telemetry**, and **copy all telemetry off the Pi to the VPS**.
Deletion from the boat is allowed only *after* an off-boat copy is sha256-verified.

## ⏸ PICK UP HERE (paused 2026-09-01 ~20:30Z — a drain is RUNNING unattended)

**Two long-running jobs are live right now. Check them before doing anything else.**

**1. The drain, on the boat.** Detached (`PPID 1`), survives disconnects.
```bash
ssh sr33-pi@100.79.180.102 'tail -3 /tmp/drain-archive.log; pgrep -f drain-archive.sh'
ssh sr33-pi@100.79.180.102 'cat /tmp/drain-lost-rowids.log'   # rows lost to corrupt pages
```
Look for `DRAIN COMPLETE`. At the 2026-09-01T20:26Z checkpoint it was at **id 5,733,000 of
~81,000,000**, ~131k rows/min against ~24k/min of new data — **ETA ~08:00Z 2026-09-02**.
If it died, just relaunch; the cursor makes it resume exactly:
```bash
ssh sr33-pi@100.79.180.102 'setsid ~/Agent_C4/pi/archiver/tools/drain-archive.sh </dev/null >/dev/null 2>&1 &'
```
⚠️ `drain-archive.sh` calls `/tmp/step_over_bad.py` **inside the container**. After a Pi
reboot `/tmp` is empty — re-copy it first, or the corrupt-page handling silently no-ops:
```bash
ssh sr33-pi@100.79.180.102 'docker cp ~/Agent_C4/pi/archiver/tools/step_over_bad.py sr33-pi-archiver-1:/tmp/'
```

**2. The guardian, on this box.** A **transient** systemd unit (`systemd-run --collect`): it
outlives the session that started it, but **NOT a reboot of this box** — if the OVH box
restarts, relaunch it before letting the drain continue.
```bash
systemctl status c4-drain-guardian
tail -5 /home/constantineau/backups/drain-guardian.log
```
It compacts every 5 min (the auto compression policy will NOT cover the active chunk until
~Sep 5), stops the drain if free disk drops under 5 G, and exits cleanly on `DRAIN COMPLETE`
after a final compaction pass. **If it is not `active`, restart it before resuming the
drain** — without compaction the drain writes ~20 GB into ~24 G free.
```bash
systemd-run --unit=c4-drain-guardian --collect /home/constantineau/Agent_C4/pi/archiver/tools/drain-guardian.sh
```

**When the drain completes, in order:**
1. Confirm `FINAL rows=… size=…` in the guardian log; check `drain-lost-rowids.log`.
2. Recreate the archiver container to clear its stale `VPS_URL` (see #6) — **only after** the
   drain, since the drain runs inside it.
3. Set up the **recurring** drain (#6c) — this run is a one-time catch-up; the boat adds
   ~34M rows/day and #8's prune starts ~2026-09-13.

Everything is committed and pushed — working tree clean, nothing left in a scratchpad.
`dev` = `8ea263d`, `main` = `186da58` (merge commits, both on origin). The boat's clone is
on `main` and pulled to `b3c30a1`; it is one merge behind, which only affects the
guardian script that runs on the OVH box, not on the boat.

## Status at a glance

| # | Item | State |
|---|------|-------|
| 1 | Jul 18 race backfill → Postgres | ✅ done 2026-08-30 |
| 2 | NUL-strip fix in ingestion | ✅ deployed + verified 2026-09-01 |
| 3 | Boat cleanup, the verified 2.1 GB | ✅ done 2026-08-30 |
| 4 | Pre-race remainder, 5,481,959 rows | ⬜ not started |
| 5 | Re-pull of the 9.6 GB corrupt archive | ✅ VERIFIED 2026-08-30; salvage still to do |
| 6 | Drain the Pi's live `archive.db` | 🔄 **RUNNING unattended** — ETA ~08:00Z 2026-09-02 |
| 6b | TimescaleDB compression | ✅ enabled, 36.6x — this is what made #6 possible |
| 6c | Recurring drain so it stays drained | ⬜ **next task** — makes the Sep 13 prune safe |
| 7 | Uplink spool | ✅ root cause fixed, drained to 0, reconciles exactly |
| 8 | `ARCHIVE_RETAIN_DAYS=14` prune | ⬜ open — **starts deleting ~2026-09-13** |
| 9 | Jul 18 duplicate rows | ✅ deduped 2026-09-01, reversible |
| 10 | derived-data double-emit | ✅ fixed on the boat 2026-09-01 |
| 11 | Live-archive reads race the archiver | ℹ️ caveat for #6 |

All of this session's work is **committed and pushed** on `dev` and merged to `main`:
the ingestion NUL strip, the uplink queue fix + its regression suite, the Signal K
wind-path split + compose wiring, the drain tooling, the guardian, and this document.

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

Committed 2026-09-01 as `699714e` and merged to `main`. The running container was built from
the working tree, so image and source now agree.

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

### 6. Drain the Pi's live `archive.db` 🔄 **IN FLIGHT since 2026-09-01 — do not re-run blind**
16.2 GB / **79,934,390 rows** (rowid 1 → max), all dated Aug 30 → Sep 1 — the DB was
recreated Aug 30, and the boat produces ~36M rows/day.

**Don't copy the file — stream it.** `backfill.py --all` runs *inside the archiver
container on the Pi*, which already has the DB and the network, so the 16 GB never moves.
That dissolves the local-disk problem entirely.
```bash
# on the Pi — supervised, resumable, survives disconnect:
/tmp/drain-archive.sh          # logs to /tmp/drain-archive.log
```
Measured throughput **~131k rows/min**, so the full 79.9M is roughly a **10-hour** job.

⚠️ **The archiver container carries a stale `VPS_URL`** — `100.67.228.63`, which is
unreachable from the boat (the live uplink correctly uses `100.88.252.115`, and only that
one answers `/health` with 200). A backfill run with default env just fails; the drain
script overrides it explicitly.

**This is not a code bug — `compose.pi.yml` and the Pi's `.env` are both already correct.**
The container was created **2026-07-08** and has been running with an env baked in from
before the VPS address changed. The fix is simply to recreate it:
```bash
cd ~/Agent_C4 && docker compose -f compose.pi.yml up -d --force-recreate archiver
```
**Do NOT do this while the drain is running** — the drain executes inside that container.
Do it once the drain reports COMPLETE. Worth checking the other long-lived containers
(console/engine/n2kout/signalk are all 6+ weeks old) for the same drift.

**Why it is supervised:** transient `database disk image is malformed` (see #11) and link
drops both abort a run of this length. `backfill.py` keeps a `backfill_last_id` cursor in
the archive's `sync_state` and pages by id, so a restart resumes exactly and never
re-sends — which matters because `telemetry_raw` has no PK and a re-send duplicates
silently. **Never use `--since/--until` for this**: ad-hoc mode ignores the cursor.

**The live archive has REAL corrupt pages too, and plain retrying cannot pass them.**
The drain wedged at ids **470500..471000**: `backfill.py` pages with
`id > cursor ORDER BY id LIMIT n`, so a corrupt page anywhere inside that window makes the
whole SELECT raise — the cursor never advances and the supervisor loops forever (it burned
6 attempts in under a minute). This is distinct from the WAL-race transients in #11: those
clear on retry, this never does.

`pi/archiver/tools/step_over_bad.py` handles it with salvage.py's bisect: split the range,
recurse to single rows, POST everything readable, and advance the cursor past only what is
genuinely unreadable. First use recovered **2,954 of 3,000 rows, losing exactly 46**
(rowids 470572–470617, one contiguous page — the July salvage lost 45 the same way).
Losing 46 readings beats losing the 79.5M behind them.

`pi/archiver/tools/drain-archive.sh` now does this automatically: retry once for a
transient, and on a *second* consecutive failure hand off to the stepper, then resume.
Unreadable rowids are appended to `/tmp/drain-lost-rowids.log` on the Pi.

### 6b. Compression is what makes #6 possible at all
Draining 79.9M rows at the measured **263 bytes/row** would have been **~21 GB into 22 G of
free disk** — it would have filled the disk and taken Postgres down. TimescaleDB compression
was available but **not enabled**.

Enabled it (`segmentby = boat_id, source, path`, `orderby = time DESC`) and measured on real
data: **37–42x**. `telemetry_raw` went **3,459 MB → 94 MB** with row counts unchanged
(13.75M total; Jul 18 still exactly 5,296,752). The 79.9M incoming rows should land around
**~500 MB** rather than 21 GB.

Two things learned doing it, both non-obvious:
- **Compress chunk-by-chunk, one statement per transaction.** A single multi-chunk
  `compress_chunk(...)` over `show_chunks(...)` **deadlocks** against live uplink inserts.
- **Compressing an actively-written chunk does not block ingestion.** Verified: the live
  uplink kept landing at 5.5 s lag into the just-compressed current chunk. That is what lets
  the drain be compacted *while running* instead of peaking at 21 GB.

A compression policy (`compress_after => 2 days`, job 1000) handles future chunks, and
`pi/archiver/tools/drain-guardian.sh` recompacts during the drain.

⚠️ **The policy does not protect this drain.** It fires on chunks older than 2 days, but all
79.9M incoming rows are dated Aug 30 – Sep 1 and land in the chunk covering Aug 27 – Sep 3,
which does not age out until ~Sep 5. Compaction has to be driven manually until then —
that is the guardian's whole purpose. Do not assume the policy has it covered.

### 6c. The drain is a one-time catch-up — it does NOT stay done
`backfill.py` exits once it reaches the end, so this run only closes the existing gap. The
boat then keeps producing **~34M rows/day** (~24k/min, ~240 MB/day compressed) and the gap
reopens immediately.

For #8's prune to be safe under "lose nothing", the drain has to run on a schedule so the
prune only ever deletes rows already on the VPS. A systemd timer or cron on the Pi calling
`pi/archiver/tools/drain-archive.sh` is enough. **Not yet set up — this is the next task,
and it is what actually makes the Sep 13 prune deadline safe.**

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

**Fix in `pi/uplink/uplink.py`** (committed `8ca9c21`, merged to `main`):
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

### 11. `database disk image is malformed` on the live archive has TWO causes — tell them apart
Both are real, and they need opposite responses. Diagnosing one as the other wastes hours.

**(a) Transient — a reader racing the archiver's WAL.** The *same* query over the *same*
rowid range fails on one pass and succeeds on the next; a later pass read 100,000 rows clean
across a window that had just failed. **Retrying works.** Contributing factor found the hard
way: a leftover `docker run` probe container held an open SQLite connection on the archive
for ~15 min and made this much worse — kill stray readers (`docker ps` on the Pi).

**(b) Persistent — an actually corrupt page.** Reproducible: every attempt over that exact
range fails, forever. Confirmed at ids 470500..471000 by walking 500-row windows — clean
either side, and bisection pinned the damage to 46 contiguous rows (470572–470617).
**Retrying never works**; only bisect-and-skip gets past it.

**Distinguishing them cheaply:** retry once. Transient clears; persistent fails identically.
That is exactly the rule `drain-archive.sh` implements.

This is the third instance of SQLite corruption on this SD card (`archive.corrupt-20260718`,
`archive.corrupt-20260830`, now live pages). Treat it as expected, not exceptional — **the
root cause is still unaddressed**, and a drain that assumes a clean DB will wedge.

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
