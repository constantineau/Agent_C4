# C4 telemetry consolidation — progress (updated 2026-09-07)

Goal (Cole): **lose no telemetry**, and **copy all telemetry off the Pi to the VPS**.
Deletion from the boat is allowed only *after* an off-boat copy is sha256-verified.

## ⏸ PICK UP HERE (2026-09-07, second pause — read this block, then "Session 2026-09-07 (later)")

**Everything in this session is committed on `dev` and NOT pushed.** Working tree clean.
All suites green: **24 files + 10 pytest cases** (`test_power`, `test_sensor_health`,
`test_source_priority`, `test_brownout` are the new ones). Nothing is mid-flight; no background
units are running.

**Scope Cole set, in his words:** the full-res archive (`17:03:31 → 20:40:30Z` Jul 18) is what
they *"highly value"*; everything after the turnaround is *"scrap"*. They **retired** — the race
ended ~20:00 local (00:00Z Jul 19). So: do not invest in the post-turnaround spool, and do not
backfill the Jul 15–17 delivery salvage into Postgres (non-racing = scrap; the 9.10 GB salvaged
file on disk already satisfies "lose no telemetry"). Cole's stated goal for the good data:
**analyse it in the C4 Lab debrief, and use all of it to make the system better.**

**Cole approved and these are DONE this session:** watch the bank (`GET /power`), make archiving
brownout-tolerant, make `source_priority` actually bind, and fix the Lab debrief's own-log
track. Full write-up in "Session 2026-09-07 (later)" below, including the correction that the
**GPS kick at 22:58Z, not the battery, is what cost primary navigation**.

**Do these next, in this order:**
1. **The iPad surface.** The engine now returns `/power`, `/health/sensors` and provenance +
   `fell_back` per channel in `/conditions/full`, and **none of it is on the dashboard**. One
   bank tile, provenance on each number, and a single instrument-health chip (unresolvable
   matchers · silent rank-1 sources · attitude out of range · heading-vs-COG bias · AIS or
   synthetic sources present). This is where the value is now: the checks exist, nobody aboard
   can see them.
2. **Two things only a person at the boat can settle** — confirm the bank's chemistry/capacity
   (the `POWER_*` absolute thresholds are unverified 12 V lead-acid guesses; the trend and
   projection are sound), and enable the Orca Core's N2K attitude sharing (heel/pitch/ROT
   rank 1 is fiction until then).
3. **Derive `headingTrue` from `headingMagnetic` + `magneticVariation`.** Heading had *no*
   redundancy on Jul 18 — the 24xd was the only publisher, and when it was kicked there was
   nothing to fail over to.
4. **Record device identity on the boat** (archiver/uplink read `/signalk/v1/api/sources`), so
   `shared/n2k_sources.SR33_DEVICES` becomes a cache rather than the source of truth. N2K
   addresses are claimed at power-up and can move; `drift()` exists to detect that.
5. Then the v2 backlog. Parked on the boat: recreate the archiver container for its stale
   `VPS_URL`, #6c the recurring drain.

**Replay rig state:** `backups/replay-jul18/timeline/` is REBUILT against the current engine
(433 frames, 0 endpoint errors) — use it. `timeline-preprio/` is the pre-fix baseline kept for
before/after diffs; `timeline-prio-partial/` is a half-fixed intermediate, **delete it**.
Rebuilds need the venv in a scratchpad (`freezegun`, `fastapi`, `httpx`, `websockets`,
`pytest`) — it is ephemeral, recreate it.

**Disk: ~9 G free (90%).** The obvious reclaim is
`backups/c4-boat-pull-2026-08-30/work/archive-backfill.db` (3.1 GB, no longer needed — #4 is
done) and `timeline-prio-partial/`.

---

**The boat is OFFLINE and that is accepted** — Cole, 2026-09-07: no race is near, so do not
chase it, and do not treat Pi/Orin work as blocked-and-waiting. Uplink stopped
**2026-09-02T22:13Z** (one 1,476-row reconnect blip at 2026-09-04T20:59:45Z, nothing since).
Tailscale last saw `sr33-pi` 2026-09-05 and `agent-c4` 2026-09-03.

**#6 drain COMPLETE** 2026-09-02T07:24:57Z, attempt 27. Guardian exited clean:
`FINAL rows=109,934,779 size=1033 MB`. Reconciled Postgres-side: 74 hours of data across the
74 hours the fresh archive existed (Aug 30 ~05:00Z → Sep 2 07:24Z), **zero gaps**.

**#4 pre-race remainder DONE** 2026-09-07: `5,481,959` rows, reconciled exactly
(260,811 pre-existing + 5,481,959 = 5,742,770 in-window). Rollback table
`telemetry_raw_pre4_20260907`. Ad-hoc mode is not re-run-safe — do not run it again.

**Deadline moved in our favour.** Everything through Sep 2 07:24Z is on the VPS, so the Sep 13
prune has nothing undrained to eat. The undrained window is Sep 2 07:24Z → whenever the boat
died, which becomes prune-eligible **~2026-09-16**.

**Next, all boat-independent:** #5 (salvage the re-pulled 9.6 GB archive — local, never
started), then the v2 work in `docs/V2_BACKLOG.md`, which the Race Rewind rig
(`tools/replay/`) has now unblocked. Needs the boat, so parked: recreate the archiver
container for its stale `VPS_URL`, and #6c the recurring drain.

### Added 2026-09-07 (later session) — three facts that change what is worth doing

**1. The whole race IS covered — in two resolutions. Nothing of it is missing.** (Corrected
2026-09-07 after Cole pushed back on an earlier, wronger version of this note.)

Racing ran roughly **17:03Z → 00:00Z Jul 19**, i.e. **13:03 → 20:00 local EDT** — Cole's
"racing ended about 20:00" is local time, and the track confirms it to the hour: the boat made
5.9–7.3 kn northeast up Lake Huron to 43.71 N/−82.12 W at exactly 00:00Z, **turned around**,
sailed back down the same line and was stopped (0.00 kn) at Port Huron 43.0021 N/−82.4136 W by
07:00Z. That is a retirement, not a finish.

| window (UTC) | source | resolution |
|---|---|---|
| 17:03:31 → 20:40:30 | full-res archive (`archive-recovered.db`) | ~18,000 rows/h, 5–28 Hz |
| 20:40:31 → 00:09:35 | uplink **spool**, already in Postgres | 33,014 rows, **74 paths**, ~16 s/path |
| 00:00 → 07:00 Jul 19 | spool | the sail home |

So the archive covers the first ~3 h 37 m of a ~7 h race and the spool covers the last ~3 h
20 m — including the decision to retire. **What is gone is resolution, not the race**: the
archiver crash-looped on the corrupt DB from Jul 19 and wrote nothing until Aug 30, and AIS
stopped in the same hour from the same cause, so the final third exists at 16 s per path rather
than 5–28 Hz. Earlier notes in this file called that "the offshore leg, permanently gone" —
misleading on both counts: there was no long offshore leg, and 74 paths at 16 s is enough for
the engine (its consumers bucket to minutes).

**Follow-up this unlocks:** the replay rig stops at 20:40 only because it reads the SQLite
archive. Materialise the 33,014 spool rows into a `readings`-schema SQLite file and the rig
covers the **whole** race, retirement included — which is exactly where Time-to-Mark and
playbook relevance mattered most. Boat-independent, and the highest-value use of the rig.

**2. #5's 9.6 GB `archive.corrupt-20260718.db` contains no race data at all** — probed
directly: rowids 1..**46,408,949**, spanning **2026-07-15T20:35:32Z → 2026-07-17T21:13:01Z**.
The filename is the date it was set aside, not the data inside it. So #5 is a **Jul 15–17
delivery / tune-up** recovery, ~46.4 M rows at full resolution against the ~1 M low-res spool
rows now in Postgres for that window (**~45×**). Real value for polar/config learning; zero
value for the race debrief. Note the seam: it ends **1 h 38 m before** the recovered archive
begins (Jul 17 22:51:08Z) — a rotation, not corruption. The Jul 16 11:00–14:00Z hole in
Postgres is **not** a data-loss gap: the salvaged archive has nothing there either, so the
boat was simply off.

**#5 SALVAGE DONE 2026-09-07 (later session).** `pi/archiver/tools/salvage.py` (new — the old
`backups/…/salvage.py` had src/dst and the rowid range hardcoded to the 2.1 GB archive and
would have merged this one into that one's output; the new one takes `--src/--dst`, plus
`--resume`, `--min-free-g` and incremental lost-rowid logging). Result, 253 s:

```
salvaged : 46,408,689 rows   lost 260 (99.9994%)   9.10 GB, indexed
span     : 2026-07-15T20:35:32.641Z -> 2026-07-17T21:13:01.138Z   139 paths
-> backups/c4-boat-pull-2026-08-30/recovered/archive-jul1517-recovered.db
   (+ …db.lost-rowids.txt, 3 corrupt page ranges)
```

⚠️ **Not yet in Postgres, and the backfill needs a decision.** The window already holds
5,481,959 spool rows from #4, and these 46.4 M archive rows cover the same readings at full
rate — so an ad-hoc backfill **partially duplicates** it. `backfill.py` session mode is
re-run-safe but would push nothing (Jul 15–17 is not a race session); `--since/--until` mode
is the only route and is **not** re-run-safe. Plan before running: snapshot a rollback table,
then either delete the #4 window rows first or dedupe on `(tableoid, ctid)` after.

⚠️ **Disk: 9.6 G free (90%)** after the salvage. `work/archive-backfill.db` (3.1 GB) is
deletable now that #4 is done — that is the obvious reclaim before the backfill.

**3. Archive cleanup (Cole's item 1) resolves without deleting anything** — measured; see
`docs/V2_BACKLOG.md` → "Onboard hardware / deployment". Short version: `n2k-socketcan.43` is an
em-trak B951 AIS transceiver, all 3,394,173 of its rows are AIS/AtoN with no own-ship data
interleaved, and **vessel identity was never archived** (unique timestamps per position, so
rows regroup into reports but can never be attributed). Retro Fleet replay out of
`telemetry_raw` is therefore impossible; `ais_targets` (902,710 rows / 430 MMSIs, with the
race) is the store to build on. **Recommend keeping the rows** — the read filter hides them,
they are ~1% of a 36×-compressed archive, and `telemetry_raw` has no PK. The forward-looking
half (an `mmsi` column in the archive, the only route to *offshore* Fleet history, since
`ais_targets` is uplink-fed) is a schema change and needs Cole's call.

**Also found, and bigger than the cleanup: `source_priority` has never matched anything.** The
seeded matchers are device names (`orca`, `24xd`, `reactor`, `gnd`) but `$source` labels are N2K
addresses (`n2k-socketcan.15`), so the cloud's `_choose_preferred` always falls through to
"freshest available" — and the onboard path never consulted the table at all. Two of its
premises are also false (the Orca published **no** roll/pitch/ROT during the race; `gwind` is
not a distinct source, the masthead arrives via the GND10). Full measurements, the recovered
address→device map, and the four-part fix are in `docs/V2_BACKLOG.md`. Fifth instance of the
session's pattern: designed, seeded, wired, silently not in force.

---

### Session handoff — 2026-09-07 (paused by Cole)

Everything below is **committed, merged to `main`, and pushed**; working tree clean, all suites
green (16 agent + navigator-progress + navigator-eta + archiver context-filter = 18).

**Five defects found and fixed, each measured against the real Jul 18 race rather than argued:**

| what | before | after |
|---|---|---|
| mark sequencer (`navigator`) | `next_mark` stuck on "Start" the whole race | advances; leg gating live |
| readout flapping (`console`) | 7 flips, "no data" 63% of samples | 0 flips, 0% |
| `/reoptimize` caching | ~97% miss → ~91% engine CPU duty | 82% reuse → ~17% |
| own-ship position (AIS contamination) | max 4,091 kn, 15.9% impossible | max 8.0 kn, 0.0% |
| Time-to-Mark ETA | arrival spread 74.34 h | 10.39 h, worst jump 0.25 h |

**⚠️ Boat-deployment gate is CLEARED but read this first.** The `/strategy` cost was the thing
blocking the sequencer fix from reaching the Pi, and it is fixed — so `main` is deployable when
the boat returns. The boat's clone tracks `main` and is several merges behind; deploy by copying
single files after diffing (a `git pull` there switches the branch — see "Hard-won specifics").

**A pattern worth acting on next session.** Three of the five were things already *designed*
correctly that had quietly stopped working: a cache that never hit, status hysteresis smoothing
the wrong field, and a context filter that existed in `uplink.py` but never reached
`archiver.py`. A fourth recurred three times in one session — **quantising a continuous quantity
reintroduces discontinuities** (cache-key buckets chattering at their edges, `if twa < beat`, and
snapping to the polar grid). Worth spending time on assertions that would have caught these — a
cache-hit-rate check, a plausibility gate on own-ship position, a continuity sweep — rather than
only on new features.

**Open, in the order Cole chose:**
1. **Archive cleanup** — AIS rows are hidden from own-ship reads but still on disk. Related:
   whether AIS should be archived *properly* with a context column, which would make the Fleet
   tile replayable (currently it cannot be).
2. **Log Tier-2 copilot output** — never archived, so every debrief is blind to what it said.
3. **#5, the 9.6 GB salvage** — untouched, purely local, longest-standing item.
4. Follow-up: routed (forecast-aware) ETA into `next_mark` — `/reoptimize` already computes
   per-mark ETAs and is now cheaply cached. **Mind the recursion: `reoptimize` calls
   `get_navigator`.**

**Race Rewind rig** (`tools/replay/`, built this session):
```bash
python3 tools/replay/server.py --timeline /home/constantineau/backups/replay-jul18/timeline
# http://localhost:8110/   — real console left, ground truth right, notes -> /replay/notes.md
```
A final timeline rebuild was launched at pause under `systemd-run --unit=c4-replay-rebuild`
(~35 min) so it survives the session — **check `systemctl status c4-replay-rebuild` and that
`timeline/frames.jsonl` has 433 lines before trusting the rig.** Rebuild it after any engine
change: `tools/replay/harness.py` then `tools/replay/truth.py`, same `--start/--end/--step`, or
the ground-truth pane silently blanks past the shorter of the two.

**Two shell traps that cost time this session** (both bit more than once):
- `pgrep -f X` / `pkill -f X` **match the invoking shell's own command line** — they kill the
  session (exit 144) or loop forever waiting on themselves. Use `pgrep -f 'harn[e]ss.py'`, or
  put the kill in a script file.
- Timings taken *inside* `freeze_time` are meaningless (even a `time.time` captured beforehand
  reads frozen). Measure from the shell, or bisect by running with different inputs.

## Previous resume block (2026-09-01 — drain now finished, kept for context)

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
`dev` is merged into `main` and both are on origin (`git log --oneline -3` for the exact
heads; SHAs are deliberately not pinned here because they drift). The boat's clone tracks
`main` and may sit a merge or two behind — that only affects the guardian script, which runs
on the OVH box, not on the boat, so the boat does not need to be current for the drain.

## Status at a glance

| # | Item | State |
|---|------|-------|
| 1 | Jul 18 race backfill → Postgres | ✅ done 2026-08-30 |
| 2 | NUL-strip fix in ingestion | ✅ deployed + verified 2026-09-01 |
| 3 | Boat cleanup, the verified 2.1 GB | ✅ done 2026-08-30 |
| 4 | Pre-race remainder, 5,481,959 rows | ✅ done 2026-09-07 |
| 5 | Re-pull of the 9.6 GB corrupt archive | ✅ re-pulled 08-30; ✅ **salvaged 09-07** (46,408,689 rows, 99.9994%) — backfill to Postgres still open, needs a dedupe decision |
| 6 | Drain the Pi's live `archive.db` | ✅ COMPLETE 2026-09-02T07:24Z — 109.9M rows, zero gaps |
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

✅ **Salvaged 2026-09-07** — 46,408,689 rows, 260 lost, span Jul 15 20:35:32Z → Jul 17
21:13:01Z. See the "Added 2026-09-07 (later session)" block at the top for the numbers, the
new `pi/archiver/tools/salvage.py`, and why the Postgres backfill is still open.

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

**Updated 2026-09-07 (later session):** the salvage ran and cost 9.10 GB —
`recovered/archive-jul1517-recovered.db`. **`/` is now at 9.6 G free (90%).** Reclaim
`work/archive-backfill.db` (3.1 GB, no longer needed — #4 is done) before the Jul 15–17
backfill. `salvage.py` now takes `--min-free-g` (default 6) and stops mid-run rather than
filling the disk; re-run with `--resume` after freeing space.

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

---

## Session 2026-09-07 (later) — power, provenance, and the debrief path

Everything below is on `dev`, all suites green (22 files + 10 pytest cases), and each number
was measured against the real Jul 18 race rather than argued.

### The finding the rest hangs off: the house bank went flat during the race

| hour (UTC) | mean V | min V |
|---|---|---|
| 11:00 (pre-start) | 12.91 | 12.03 |
| 14:00 | 12.75 | 12.10 |
| 16:00 | 12.11 | 11.41 |
| 18:00 | 11.68 | 11.15 |
| **20:00 — archiver + AIS die at 20:40** | **11.61** | **11.08** |
| 05:00 Jul 19 (motoring home) | 13.30 | 13.03 |

A monotone 1.3 V decline over six hours of racing, bottoming at 11.08 V, recovering only once
the engine came on. At 20:40, at the bottom of that curve, **two things stopped in the same
minute**: the full-res archiver (SQLite corruption, the third on that card) and the em-trak AIS
receiver (67,149 rows in the previous 100 min → **11** in the next 3.5 h). Every other N2K
source kept reporting through the uplink, so the bus lived and the two write-sensitive/
power-hungry devices did not. Correlation, not proof — but it is to the minute.

**`electrical.batteries.0.voltage` was in the archive at ~0.7 Hz the whole time and nothing
read it**: absent from `tools.PRESENT`, from `onboard_conditions.PRESENT`, from `alerts.py`'s
six rules, and from the dashboard (whose "energy" tile is *crew* energy).

### What was built

1. **`vps/agent/app/power.py` + engine `GET /power`** — bank level (median), robust trend
   (median-of-thirds, not OLS), projected hours to an 11.0 V floor, and status
   ok/warn/danger/charging. Stateless so the replay rig gives the same verdicts as the boat.
   `vps/agent/test_power.py` replays the real curve: **warn at 14:25Z, danger from 16:10Z** —
   6 h 15 m and 4 h 30 m of warning before the archiver died — and no alarm during the motor
   home. Two bugs the test caught in the module itself, both this repo's recurring shape: a
   raw-threshold test flapped danger↔warn while the bank sat on 11.6 V for three hours, and an
   OLS slope turned a single winch-load sag into "the bank is dying". Now: Schmitt band on the
   smoothed level, release requires a *sustained* recovery (an unloaded flat bank reads high).
   ⚠️ **The absolute thresholds (12.0 / 11.6 / 11.0 V) assume 12 V lead-acid and are NOT
   confirmed against this boat** — chemistry/capacity are recorded nowhere in the repo. The
   trend and projection need no such assumption; trust those first.

2. **Brownout-tolerant archiving (`pi/archiver/archiver.py`)** — the corruption cost six weeks,
   not because of the corruption but because of the response: `open_db()` raised, the process
   exited, Docker restarted it, 48 times, recording nothing from Jul 19 to Aug 30. Now
   `open_db_resilient()` runs `PRAGMA quick_check`, rotates a bad file aside as
   `archive.corrupt-<ts>.db` (**kept** — salvage recovers ~all of it) and opens a fresh one;
   the flusher survives corruption mid-write, rotates, and re-writes the buffered rows; a
   transient lock requeues instead of rotating; every rotation is counted in `sync_state`
   because a fresh archive otherwise looks exactly like a boat that never sailed. The reader no
   longer writes at all (it signals the flusher), so there is exactly one writer and one
   recovery path. `pi/archiver/test_brownout.py`, 34 assertions — it caught a real bug in the
   new code: two rotations in the same second would have **deleted** the first corrupt file.

3. **`source_priority` now binds** (see `docs/V2_BACKLOG.md` for the full write-up). Matchers
   were device names, `$source` labels are N2K addresses, so nothing ever matched and every
   channel silently used freshest-wins. `shared/n2k_sources.py` resolves addresses to devices
   (map regenerable from Signal K's `sources-cache.json`, with drift detection);
   `shared/source_policy.py` holds the policy the boat uses (no Postgres aboard); the seed SQL
   and `source_notes` are corrected (`gwind` → `gnd`, `gdt` → `intelliducer` — there is no
   GDT 43 on this bus); the cloud, the onboard instrument strip, `latest_value` and the replay
   rig all consult it. Measured on the race: heel came off the "non-racing only" autopilot
   AHRS on **58%** of reads, apparent wind changed on **44%** (0.33 kn median, 4.86 kn max).

4. **The Lab debrief's own-log track was reading other vessels.** `GET /racelog/track` had no
   `boat_id` filter and no AIS filter, and kept whichever source wrote last within each second.
   99.7% of one-second buckets in the race window contained an AIS position:

   | track build | median | p99 | max | legs >15 kn |
   |---|---|---|---|---|
   | as it was | 6.54 kn | 21,290 kn | **50,034 kn** | 9.0% |
   | AIS sources excluded | 6.46 | 18.1 | 25.5 | 1.48% |
   | **+ source priority per second** | 6.48 | **9.3** | **13.6** | **0.00%** |

   This mattered most because it is the one path the race actually gets analysed through.
   `shared/n2k_sources.AIS_MARKER_PATHS` is now the single definition for all three readers
   (onboard, cloud, replay ground-truth) — the cloud one had simply never existed.

### ⚠️ CORRECTION — the battery did NOT cause the navigation failure

Cole relayed from the crew that **someone kicked the GPS/compass** during the race and primary
navigation was lost, and that the battery may not have been to blame. The telemetry agrees with
the crew and dates it to the minute. `n2k-socketcan.3` is the Garmin GPS24xd — a 9-axis
sensor that was the **only own-ship `headingTrue` publisher on the bus**:

```
22:57Z  roll  36.4°  pitch  +2.8°   tracking the autopilot AHRS (35.3° / −3.4°), as all race
22:58Z  roll 133.1°  pitch −49.2°   the autopilot still reads 35.3° / −3.4°   <-- THE KICK
…       roll ≈ ±175° (inverted), pitch −30..−49°, for 23 minutes
23:21Z  roll  38° then a few degrees — the crew put it back upright
00:00Z+ headingTrue − COG = −96, −90, −90, −91, −90, −89, −87, −90°  (spread 2–7°)
```

**Two independent failures, 2 h 18 m apart:**

| time | what | cause |
|---|---|---|
| 20:40:30Z | full-res archiver dies (SQLite corruption) + AIS receiver goes silent | the flat bank (11.08 V minima) |
| **22:58Z** | **primary navigation lost — compass/attitude kicked** | **mechanical, nothing to do with power** |
| 00:00Z | retired, turned for Port Huron | ~1 h after the kick |

The bank was *recovering* by 22:58 (11.83 V and rising), so the earlier framing — which put the
battery at the centre of everything — is wrong about navigation. The battery cost the **record**
(archive resolution + all AIS); the kick cost the **navigation**. Both are real; they are not
the same failure.

**The worst part is the hour after the repair.** Once the sensor was upright again every value
looked ordinary — a few degrees of roll, a few of pitch, a plausible heading number — and the
compass was wrong by a quarter turn for the remaining seven hours. A range check cannot see
that. Nothing aboard cross-checked heading against GPS course, so nobody was told.

Two more things the same data shows:

- **The bias was already growing before the kick**: hourly heading−COG ran −0.1..−5.7° through
  the healthy race, then **+7.0° at 21:00Z and +16.3° at 22:00Z**. Either the sensor was
  already working loose or that leg had unusual leeway/current — either way it was visible an
  hour early and unreported.
- **The 24xd's pitch has never been calibrated.** Over the healthy window the autopilot reads
  mean −0.39° (range −7.5..+7.8, symmetric about zero); the 24xd reads mean **+10.95°** and
  never crosses zero (+4.3..+19.9). It is mounted ~11° nose-up. `pitch` priority now ranks the
  autopilot above it, with the measurement in the seed comment.
- **Heading has no redundancy at all.** Only the 24xd published `headingTrue`; the autopilot
  publishes `headingMagnetic` only. Deriving true from magnetic + `navigation.magneticVariation`
  is the missing fallback — worth doing, and separate work.

**Built for it:** `vps/agent/app/sensor_health.py` + engine `GET /health/sensors` — an attitude
range gate (catches the kick on the *first* sample) and a heading-vs-COG circular-bias check
(catches the quiet quarter-turn the range gate cannot see, and would have warned at 22:00Z).
Stateless, so the replay rig agrees with the boat; it reports rather than substituting, because
silently picking another sensor is how the AIS contamination survived a race.
`vps/agent/test_sensor_health.py` replays the real event, 30 assertions.

### Still open

- **Confirm the bank's chemistry and capacity**, then set `POWER_*` thresholds honestly.
- **Enable the Orca Core's N2K attitude sharing** (dockside): heel/pitch/ROT rank 1 is fiction
  until then, and the boat races on a 1 Hz GPS attitude plus the autopilot.
- **Record device identity on the boat** (archiver/uplink read `/signalk/v1/api/sources`), so
  the committed address→device map becomes a cache rather than the source of truth.
- **Surface all of this on the iPad**: provenance per number (device + `fell_back`), a bank
  tile, and a single instrument-health chip (unresolvable matchers · silent rank-1 sources ·
  AIS/synthetic sources present · channel disagreement). The engine now returns the data; no
  console work has been done.
- Deploy: boat is offline. `main` is deployable; copy single files after diffing.
