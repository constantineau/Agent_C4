# C4 telemetry consolidation — progress (updated 2026-09-08)

Goal (Cole): **lose no telemetry**, and **copy all telemetry off the Pi to the VPS**.
Deletion from the boat is allowed only *after* an off-boat copy is sha256-verified.

## ⏸ PICK UP HERE — REFINEMENT PHASE (set 2026-09-09, horizon: a couple of weeks)

**Cole approved the Polar tab as a first pass and set the mode: "generally refine the entire
system over the next couple weeks."** Not new surface area — make what exists trustworthy,
smooth and correct. The standing dev stack (agent + lab) runs today's code; the bench console
stack too; `dev` and `main` are level and pushed.

**The refinement queue, in rough order of value:**
1. **Close the polar → optimizer loop.** The Polar tab's "Propose refinement" honestly reports
   no measured bins archived — bins reach the archive only through a debrief RUN, which needs a
   race definition + oracle. Run a real debrief over the Jul 15 boat-log track (it's the clean
   race), archive the measured bins, propose, and walk Cole through his first apply. This is the
   one remaining link in the chain he cares most about.
2. **Exercise the Polar tab against Cole's actual use** — he's reviewing it now; expect
   refinement asks (sorting, a per-config table view across all TWS, maybe both races overlaid).
   Don't guess ahead; fix what he names.
3. **B — the decision timeline** (the big build): the rig server-side, scrub the race with the
   trust strip beside the track. The graphical trust timeline folded in here.
4. **The known small rough edges, all measured already:**
   - the heading `warn` flicker across the 15° line (22:07–23:06 on Jul 18, ok↔warn churn in the
     trust sweep too) — wants the bank tile's dwell-median treatment in `sensor_health`;
   - the deviation tile's absolute gates (`act` for 88% of Jul 18; 5 min behind plan trips it an
     hour into a multi-day race) — thresholds should scale with the race, numbers are Cole's;
   - `selector` flip churn (40 flips on Jul 18) — same stability sweep, uninvestigated;
   - the channel-diff script (source-filtered) — four hand queries found real things, make it
     repeatable per race.
5. **When the boat is back** (unchanged): rebuild archiver + engine + console aboard, close
   session 3 from the iPad, enable Orca attitude sharing.

**Refinement-phase habits that today validated:** score for stability, not moments; verify before
deleting; one implementation per analysis; measured beats forecast beats theory; every gate must
say what it refused. The stability scorer (`tools/replay/score_stability.py`) and the two-race
corpus are the instruments for the whole phase — anything that flaps on Jul 15 is a bug by
construction.

## (2026-09-09, later — the POLAR TAB shipped)

**https://lab.racertracer.net/#polar** (password `CAN100`). The debrief interface Cole asked
for: the observed polar as a half-polar diagram (TWS chips + config filter, per-config curves
with hover tooltips, the ORC cert as a dashed reference only), the cell table, per-race
provenance (trust line, refused counts, skipped bench sessions), and the Decisions card wired to
the existing propose/apply flow (measured bins only; apply stays in Debrief). Engine:
`vps/lab/app/obspolar.py` (single implementation — the CLI in `tools/analysis/` is a thin
wrapper), cached on the `lab_learning` volume, "Refresh from the record" rebuilds in ~20 s.
`monitor.agent_json` grew a per-call `timeout` (a 7-hour full-res track fetch is ~20 s; the 8 s
default strangled it). Palette CVD-validated on the Lab surface (7 config slots); chart geometry
eyeballed via rendered SVG both modes.

**The standing dev stack is REBUILT (agent + lab, in place)** — everything from September is live
in the browser now, not just the polar tab: derived windows, the trust sweep, measured-wind
bins, the settle. The bench console stack was already rebuilt this morning.

## The earlier queue (2026-09-09, superseded where struck)

The two-race corpus is built and already paying (see the blocks below for everything shipped
today: derived window in the debrief AND the prune, session auto-close, GCS Coldline archive +
22 G reclaimed, the Jul 15 full-res backfill + control timeline, per-config polars, the bank
retune, the sail-log dedupe, `sr33-bench`). What is NEXT:

1. ~~**A — the trust layer**~~ ✅ **SHIPPED 2026-09-09** (Cole ruled: refuse, per-channel,
   printed). `trust_window.py` + `GET /racelog/trust` on the agent; the Lab stores the sweep with
   the track and `score_track` refuses danger-window samples from the learning inputs only.
   Verified e2e on the live DB: Jul 18 refuses the −98° compass windows, the Jul 15 control
   refuses nothing. Also fixed en route: duplicate session ids across engine-store generations
   (Jul 8 + Jul 15 both id=1) resolve by `start_ts` now; sail log settles at **30 s** (Cole:
   "it takes at least 30 seconds to change sails") — Jul 15 = 7 configurations, Jul 18 = 4.
   Graphical trust timeline folded into B. Details: `docs/V2_BACKLOG.md` → "Debrief" → A.
2. ~~**C — measured wind into `_performance_bins`.**~~ ✅ **SHIPPED 2026-09-09** with
   3. ~~**`config_at()` stale-config fix**~~ ✅ — fixes carry TWS/TWA/STW, `_fix_wind` prefers
   measured over GRIB (`wind_source` labels which), kite gate at 55° TWA bins implausible windows
   unattributed. E2e on live Jul 15: 6,660/6,660 measured fixes, 57 bins, phantom gone.
   Second half of C still open: tack/gybe cost, heel vs target, rudder work (the record has the
   series). Details: `docs/V2_BACKLOG.md` → "Debrief" → C.
4. **The channel-diff script** (per race, source-filtered — the bench contamination is exactly
   why it must filter). Four hand queries found real things; make it repeatable.
5. **B — the decision timeline** (the big build; needs the rig server-side).
6. Small: the heading `warn` 15°-threshold flicker (wants the bank tile's dwell median).
7. **When the boat is back:** rebuild archiver + engine + console images (`shared/` in the
   archiver, auto-close in the engine, September tiles in the console); close session 3 from the
   iPad; enable Orca attitude sharing; bank chemistry now LOW priority (Cole: never saw it go too
   low; the tile no longer assumes otherwise).

## Session 2026-09-09 (what shipped, in order)

**Done today: 0c — the Lab debrief reads the DERIVED race window.** One commit on `dev`, working
tree clean, and it is the change the rest of the debrief plan sat on. The route stopped taking
its bounds from the caller: `POST /api/debrief/track/from-log` now takes a **session**, fetches
`/racelog/sessions` and resolves the window server-side (`main.resolve_log_window`), so the Lab
UI and any other caller get the same race. Measured end to end against the live database —
**1.81 h / 2,000 fixes / 4 sail changes → 7.30 h / 8,000 fixes / 51 sail changes.**

Three things worth carrying forward:
- **The window travels with the track now** — `save_track` persists it (kind, marker span,
  `provenance`, motion device), `/api/debrief/track` serves it, the card prints it, and
  `judge._score_actual_track` stamps it into `actual_track`. **A (the trust layer) should read
  it from there** rather than re-deriving.
- **Density had to scale with the window.** `/racelog/track` thins to `max_points` (2000), so a
  4× longer window arrives 4× coarser unless you ask — the eighth instance of the shape, in a new
  costume: not "not in force" but "in force at a quarter of the resolution". Now one point per
  3 s, capped 8000; median gap 2 s. **Watch for this wherever a limit is a constant and the range
  is not.**
- **`use_marker: true`** (a checkbox in the card) still loads exactly what the button recorded.

⚠️ **The running dev stack cannot show this yet.** `sr33-dev-agent-1` / `sr33-dev-lab-1` are
2026-07-30 images: the live `/racelog/sessions` has **no `window` key**, and the resolver falls
back to the marker (tested path). Today's numbers came from current code run against the live DB
in throwaway containers. `docker compose -f compose.dev.yml up -d --build agent lab` to see it in
the browser. Note the lab image **bakes `vps/lab/web/`**, same as the console.

**Also done today: 0d — the retention prune keeps the derived window** (`session_windows()` takes
the archive connection and returns marker **and** derived, so the kept set can only grow;
`pi/archiver/Dockerfile` now copies `shared/`; `backfill.py` session mode passes its connection
too). Two findings, both from running it against the real 2026-08-30 pull:
- 🔎 **The 18:52:20 tap was a START, not a stop.** The boat's `engine.db` has a **third** session,
  `Race 2026-07-18 18:52Z`, open to this day — starting one auto-closes the last. So Jul 18's
  hours were never on the deletion path (the old open question is settled), and **retention has
  been effectively disabled on the boat since Jul 18**: one open session protects everything.
  Close it from the iPad when the boat is back. Only *closed* markers are uplinked, which is why
  the cloud thinks that race lasted 1 h 49 m.
- ⚠️ **A bucketed window's edge is not the edge.** Motion is read in 5-minute buckets labelled
  with their START, so an unpadded keep-window deletes up to five minutes off the tail of every
  race — measured, 4 rows of a five-hour sail, every prune. Padded by one bucket each side.

**Also done today: sessions auto-close** (Cole's call, 2026-09-09). `racelog.autoclose()` runs off
the REC poll in `status()` — no new loop — and closes an open session at **the last moment the
boat was under way**, once the record shows ≥ `RACELOG_AUTOCLOSE_IDLE_H` (12) hours stopped. Three
refusals matter more than the feature, and all three are tested: **a quiet bus is not a parked
boat** (needs ≥ 30 motion samples in the idle window, so a dead archiver or a powered-down boat
never ends a race); **no under-way moment inside the session ⇒ refuse**, because picking an end
for a weeks-old session would put those weeks on the deletion path; and an under-way moment that
**predates** the session cannot end it (REC tapped at the dock after a sail would otherwise write
`end_ts < start_ts`, a negative window the prune reads). `status()` reports `auto_closed` so the
crew learns the boat ended their session. **This does not fix session 3** — the boat has been
stopped for weeks, so it hits the second refusal; close that one from the iPad.

**The local backups are gone, and the disk is no longer the binding constraint: 43% used, 55 G
free** (2026-09-09). Deleted after every object was restored from Coldline, decompressed and
hashed against the sha256 taken from the original: `archive.corrupt-20260718.db` (9.0 G, already
salvaged), `archive-jul1517-recovered.db` (8.5 G), `pi/sk_archive/archive.db` (2.0 G, malformed)
and `recovered/archive-recovered.db` (2.0 G, fully in Postgres). **Still on disk on purpose:**
`work/archive-backfill.db` (2.9 G — the replay rig dies without it), `work/engine.db` and the
small files, `backups/replay-jul18/` (300 M).

**Both races are prototype data now, and neither depends on those files.**
- **Jul 18** — full-res in Postgres (10.8 M rows) and in the rig's archive.
- **Jul 15** — was full-res ONLY inside the deleted 8.5 G delivery salvage. Cut out first to
  **`backups/race-data/archive-jul15-race.db`** (459 M, 2,352,414 rows, `22:30 → 00:20Z`,
  132 paths, row-count matched against the source) and backfilled into Postgres, which is where
  the debrief reads. That window went from **47,128 aggregate rows to 2,399,542 full-res**; the
  debrief's own-log track now returns **5,291 fixes at a 1-second median gap** with 48 sail
  changes. The delivery hours stay out of Postgres per Cole's earlier call — only the race went in.
  ⚠️ `backfill.py --since/--until` compares ISO strings **lexicographically**, and
  `'…22:30:00.020Z' < '…22:30:00Z'` because `.` sorts below `Z` — the first run silently dropped
  the 409 rows in the first second. Pass sub-second bounds (`…:00.000Z`) or lose the first second.

**Off-box backups now exist** (2026-09-09, Cole's call): `gs://constantineau-c4-archive`,
COLDLINE, `northamerica-northeast1`, **versioning on + a 1-year (unlocked) retention policy, no
lifecycle rule**. Holds the whole 2026-08-30 boat pull (26 G → 3.06 GiB) with a
`MANIFEST-sha256.txt` of every source hash, and the wiped bench volume (15.6 G → 2.25 G). Each
file was hashed, compressed, decompressed and re-hashed **before** upload. ⚠️ Do NOT use
`gs://constantineau-vps-backups` for anything you cannot lose — it has a lifecycle rule deleting
every object at 30 days.

**Do these next, in this order:**
- 0e. **A — the trust layer** on the debrief. ⚠️ The gating rule (refuse vs down-weight `danger`
  bins) is Cole's call; do not pick it unilaterally.
- **When the boat is back:** rebuild the **archiver** image (it needs `shared/` now, or it prunes
  on markers alone and says so every hour) and the **engine** (auto-close lives there), then close
  session 3 from the iPad — auto-close deliberately will not.
- Small follow-up still queued: the heading `warn` flickers across the 15° threshold (80 `warn` /
  19 `ok` between 22:08Z and the kick) — wants the same dwell median the bank tile got.
- Tests: `vps/agent/test_racelog.py` gained the auto-close section (8 assertions).

**Branches: `dev` and `main` are both pushed and level** (the debrief change merged as `81aa3c7`;
the prune change follows it). The public **c4.racertracer.net dashboard is back up** — it had been
502 since the bench stack was taken down; rebuilt from `dev`, so it serves September's tiles.
⚠️ Its engine's `/health/sensors` and `/conditions/full` return **500** until the bench
`archiver` runs (the engine mounts the archive read-only and there was no writer to open it); the
bench archive is 14.6 GB of the replayed 2014 sample loop and wants wiping first.

Tests: **27 files + 10 pytest cases.** New: `vps/lab/test_debrief_window.py` (17 assertions) and
`pi/archiver/test_prune_window.py` (12). In both, the assertions that matter test the **read
path** — what interval the route asks the agent for, what rows are still on disk after a prune —
not what the resolver returns. Archiver tests need `websockets`, lab tests need fastapi; run each
in a throwaway container off its own image with the repo mounted:
`docker run --rm -v $PWD/pi/archiver:/app/pi-archiver:ro -v $PWD/shared:/app/shared:ro -w /app
sr33-pi-archiver python pi-archiver/test_prune_window.py` — cleaner than `docker cp` into a
running container, which leaves it serving a mix of two builds.

## Previous resume block (2026-09-08 — read this next, then "Session 2026-09-08" below)

**Two batches, and they are at different points.** The heading cross-check work (11 commits) is
on `dev`, merged to `main` and **pushed** — `main` is at `c525e66`. The race-window work after it
is committed and pushed to `origin/dev`, but **NOT merged to `main`**; that is your call next
session. Working tree clean.

All suites green: **25 files + 10 pytest cases** (`test_sensor_health` gained 24 assertions and
now drives `assess()` through a fake source instead of handing `heading_bias()` pre-paired
samples; `test_race_window.py` is new — 30 assertions, driven by the real Jul 18 record).
`test_racelog.py` fails on this box and always has: `pi/archiver/archiver.py` imports
`websockets`, which is not in the system python.

**Nothing is mid-flight; no background units are running.** `timeline-fullrace/` was rebuilt and
swapped in at the end of this session: 1,091 frames, 17:03:31Z → 02:09:00Z, **0 endpoint
errors**, truth aligned over the same window with the same `--spool`.

**Two things the rebuilt timeline showed that nothing had measured before:**
- **435 of 435 frames `ok` across the full-resolution healthy race (17:03–20:40Z)** — the
  zero-false-alarm claim for the 10-minute window, now end to end through the real engine rather
  than from an offline sweep.
- **The check would have warned at 22:08:31Z — fifty minutes before anyone kicked anything.**
  Bias +15.3° with 7.7° of spread, and it holds. That is the pre-kick drift the fixture noted
  (+7.0° at 21:00Z, +16.3° at 22:00Z) finally arriving as something on a screen. ⚠️ It **flickers
  across the 15° threshold** — 80 `warn` to 19 `ok` between 22:08Z and the kick — because the
  bias sits right on it. Not fixed: it is the same "right every other poll" shape the bank tile
  needed a dwell median for, and it is queued as a small follow-up rather than tuned blind.

**`main` is deployable and the boat's clone is several merges behind it.** When the boat is back:
rebuild the console + engine images (`docker compose -f compose.pi.yml up -d --build console
engine`) — the console image BAKES `pi/console/dashboard/`, so the new tiles do not appear until
it is rebuilt — and deploy by copying single files after diffing, because a `git pull` on the Pi
switches branches.

**Done this session — the iPad surface (the previous handoff's item #1).** Everything the
2026-09-07 session built was reachable by HTTP and on **no screen aboard**. Now: a **HOUSE BANK**
tile, **DATA turned into the instrument-health tile** (one chip over five cross-checks), and
**provenance on every number** (⚑ on any tile running off a backup sensor, the device named in
every BASED ON line, a full channel→device→rank→age table in the DATA detail). Verified against
the real Jul 18 race through the replay rig, not just the demo scenarios. Full write-up in
`docs/V2_BACKLOG.md` → "In-race UX".

**Four bugs found by looking at the numbers on a screen, all fixed:**

1. 🔴 **The replay rig's frozen clock only reached ONE endpoint.** freezegun's default ignore
   list contains `'threading'`, and `TestClient` runs each endpoint on an AnyIO worker thread, so
   `time.time()` returned the **real** wall clock — 51 days after the race — everywhere except
   `/conditions` (whose extra stack frame happened to hide the `threading` frame). `/sources`
   reported ages of **4,394,415 s** in every frame ever built, and `/conditions/full` showed
   **18 of 18** channels as `fell_back`. **Every timeline built before today is wrong in this
   way.** Fixed in `harness.build()`; rebuilt (see "Replay rig state").
1b. 🔴 **…and the rig could still phone the live internet.** The rebuild for that fix stalled at
   frame 282 on an ESTABLISHED TLS socket to Open-Meteo — no response, no timeout, would have
   hung forever. `NEEDS_NETWORK` misses it because **`/strategy` is replayable and chains into
   the re-route path when the verdict goes off-book**, which it does at 19:23Z. The rig is now
   hermetic (non-loopback `connect()` raises; 20 s default socket timeout;
   `REPLAY_ALLOW_NET=true` opts out and says the frames stop being reproducible).
2. 🔴 **The bank status flapped `warn`↔`danger` seventeen times in 40 minutes** — visible the
   moment the tile existed. `min()` over a sliding window is discontinuous in `now`. Fixed to a
   dwell median; `POWER_CLEAR_MARGIN_V` removed; `test_power.py` now scores **stability**, which
   nothing did before. **This supersedes the 14:25Z-warn / 16:10Z-danger figures in the
   backlog** — those came from the decimated spool. Archive-measured: first warn 13:04:30Z (7.6 h
   before the failure), and `danger` never fires, because the bank plateaued just *above* an
   unconfirmed 11.60 V line. Numbers in `docs/V2_BACKLOG.md` → "Onboard hardware".
3. **`display:flex` beats the `hidden` attribute**, so the RACE CHECKLIST bar — "appears only
   when something is due" — was on screen permanently as an empty red strip. Seventh instance of
   designed-wired-and-silently-not-in-force.

**⚠️ CORRECTION to the previous handoff: do NOT delete
`backups/c4-boat-pull-2026-08-30/work/archive-backfill.db`.** It was listed there as the obvious
3.1 GB disk reclaim. It is the **replay rig's archive** — `harness.py`'s default `--archive` and
the source of every timeline built so far. Deleting it breaks Race Rewind. (Disk is still ~11 G
free / 89%.)

**THE SECOND HALF OF THIS SESSION — the Lab debrief, and a race four times longer than the
record said.** Cole: *"I'd like to build out the 'debrief' portion of the lab"*, then *"stitch
together proximal windows into a complete race, regardless of button presses."*

- **The Lab could only see 1 h 49 m of the 7 h race.** Session marker id 2 says
  `17:03:31Z → 18:52:20Z`, and **2,534,717 rows of telemetry sit after it**. Not a bug — the
  ⏺ LOG button was caught during a kite hoist (`end_ts` written 4.2 s before the sail bar
  registered A3 up / J1 down / staysail up). Two of the three sessions ever recorded look
  accidental; Jul 8 lasted **21 seconds**.
- **✅ `shared/race_window.py`** treats the marker as a hint: seed → stitch (< 1 h apart) →
  extend across continuous underway telemetry → bound at the turnaround, with `provenance` in
  words and the raw marker always served alongside. Verified through the live database:
  **1.81 h → 7.36 h (×4.1)** for Jul 18, +18 min for Jul 15. `/racelog/sessions` now returns a
  `window` per session. Four judgement calls are argued in the module docstring and the commit —
  the start is deliberately NOT extended, the turnaround needs a sustain rule, the bound is
  `min()` not assignment, and the motion series comes from ONE device-resolved source.
- **❌ Item 3 below is DEAD, measured not assumed.** Deriving `headingTrue` from
  `headingMagnetic` + `magneticVariation` is not redundancy: the Reactor 40's magnetic heading
  tracks the 24xd to within a degree (median −0.0°, n=12,982) through the healthy race **and**
  through the quarter-turn fault, where it reads 91.0° off COG against the 24xd's 90.9°. It would
  have been a failover source that agrees with the broken sensor. Numbers in
  `docs/V2_BACKLOG.md` → "Learning loop".
- **The debrief plan (A → C → B) is in `docs/V2_BACKLOG.md` → "Debrief".** Read that section
  before starting. It carries the two findings that shape it: ~2 h of the race is
  instrument-corrupt and nothing marks it (derived TWD moves 137° across the compass step while
  TWS holds at ~27 kn), and the debrief has no concept of a retirement.

**Do these next, in this order:**
0c. ~~**Point the Lab's debrief at `window` instead of `start_ts`/`end_ts`.**~~ ✅ **DONE
   2026-09-09** — see the block at the top and `docs/V2_BACKLOG.md` → "Debrief".
0d. 🔴 **Then wire `race_window` into the onboard retention prune.** `archiver.prune()` deletes
   out-of-session readings older than 14 days (`pi/archiver/archiver.py:357`), so an accidental
   stop puts the rest of a race on the **deletion** path — the one place a wrong window destroys
   data instead of hiding it, and squarely against "lose no telemetry". (What happened to
   Jul 18's out-of-session hours on the Pi is not established — see the backlog note.)
0e. **Then A from the debrief plan — the trust layer.** ⚠️ The gating RULE (refuse vs down-weight
   bins from `danger` windows) is Cole's call; do not pick it unilaterally.
0. ~~🔴 **the heading cross-check is silent on the data the boat actually sent**~~ ✅ **DONE, and
   the headline was WRONG — the check was working.** Read `docs/V2_BACKLOG.md` → the P0 block for
   the full measurement; the short version is that this morning's P0 was raised from 36 frames of
   a 7-hour recording, and the recording disagreed. The compass was *fine* for half an hour after
   the crew re-seated it (+3 to +24° at 23:50Z) and stepped out at **23:56:10Z** — heading 43.6 →
   125.8 → 285.7° in 70 s while COG held ~25-33°. From there the bias is a rock-steady −90 to
   −105°, and the check reports **`danger` on 110 of 110 minutes** at a median −90.2° with 5.7° of
   spread against a 25° gate. The 35-of-36 `unknown` window was 23:25 → 00:09Z: a 20-minute
   sliding window straddling two step changes and containing nothing else, which is the only
   thing a sliding window can do there. **The rig only ever saw that stretch because the timeline
   stopped at 00:09:35Z, thirteen minutes after the fault began.** Same shape as `power._tripped`
   flapping the bank tile, one level up: a sliding-window statistic tells you about the window —
   and a replay window is one of those windows. Fixed anyway, because two of the three findings
   were real: the reference is now a single named publisher on a different device from the
   compass (`choose_source()`, and the verdict says "vs Orca Core"), AIS-bearing sources are
   refused outright, `HEADING_WINDOW_MIN` is **10 not 20** (measured: the window IS the detection
   latency, and 20 bought no false-alarm protection that 10 does not), and `_circular()` no
   longer raises `ValueError` on perfectly-agreeing samples — which took `/health/sensors` down
   entirely, on the cleanest input there is. The rate-aware spread gate is **explicitly dropped**.
0b. ~~**`source_priority` ranks the AIS transceiver as an own-ship fallback**~~ ✅ **DONE
   2026-09-08, Cole's call.** `b951` was rank 4 for `sog`, `cog`, `lat` and `lon`. Nothing was
   broken — the read paths filter AIS out first — but it is the one ranking whose only protection
   lives in another module, and "use the AIS box for lat/lon" is the bug that cost a race written
   down as an intention. Removed from `shared/source_policy.py`, `vps/db/seed/source_priority.sql`
   **and the running `sr33_dev` table** (`DELETE 4`), so the cloud read path matches the boat's.
   The 24xd, Orca and 943 keep three real GPS sources. Two assertions in `test_source_priority.py`
   aimed at whoever re-adds it "for redundancy" later.
1. **The two things only a person at the boat can settle**, now with a sharper reason than
   yesterday: **confirm the bank** (chemistry, capacity, charging budget) — the race data cannot
   resolve `danger` vs `warn` on its own, see #2 above — and **enable the Orca Core's N2K
   attitude sharing**, because the policy ranks the Orca first for heel/pitch/rate-of-turn/
   heading and it published **none** of them during the race. The health chip reports that as a
   standing note rather than an alarm, and it will keep doing so until someone flips that
   setting.
2. ~~**Materialise the 33,014 spool rows into a `readings`-schema SQLite file**~~ ✅ **DONE
   2026-09-08**, and then **re-cut later the same day**: 50,288 rows for `20:40:30Z -> 02:09:50Z`
   in `backups/replay-jul18/spool-jul18-to0210.db`, verified continuous across the seam. The
   first cut stopped at `00:09:36Z` — the end of racing, and defensible — and that is exactly
   what produced a P0 against working code. **Cut a replay window where the evidence ends, not
   where the race does.** (Racing *tactics* past 00:09Z stay out of scope per Cole; this is
   coverage for the health checks, and it is 3.4 MB.)
3. ~~**Derive `headingTrue` from `headingMagnetic` + `magneticVariation`.**~~ ❌ **DROPPED
   2026-09-08 — measured, and it is not redundancy.** See the block at the top and
   `docs/V2_BACKLOG.md` → "Learning loop". Heading still has no redundancy on this boat; it has
   to come from a second compass or from COG above a speed gate, and that needs a decision about
   what the engine should DO when heading is untrusted (`sensor_health` deliberately reports
   rather than substitutes). Not queued.
4. **Record device identity on the boat** (archiver/uplink read `/signalk/v1/api/sources`), so
   `shared/n2k_sources.SR33_DEVICES` becomes a cache rather than the source of truth.
5. Then the v2 backlog. Parked on the boat: recreate the archiver container for its stale
   `VPS_URL`, and #6c the recurring drain.

**Replay rig state.** Four timelines, and it matters which one you open.

| dir | window | frames | what it is for |
|---|---|---|---|
| `timeline-fullrace/` | 17:03Z → 02:09Z | **1,091 + truth** | **the standing artifact — use this.** Rebuilt 2026-09-08 after the heading fix, 0 endpoint errors |
| `timeline-heading/` | 23:00Z → 02:09Z | 378 + truth | the compass fault on its own, if you do not want to scrub nine hours |
| `timeline-fullrace-preheadingfix/` | 17:03Z → 00:09Z | 851 + truth | the previous standing one, kept one session as the before/after baseline. **It stops 13 min after the compass fault begins** — that is how a P0 got raised against working code. Delete it when the disk gets tight |
| `timeline/` | 17:03Z → 20:40Z | 433 + truth | the full-resolution (5–28 Hz) view of the racing half |

All are rebuilt with the clock fix and capture `/power`, `/health/sensors` and
`/conditions/full`. **Pass `--spool` to `truth.py` as well** or the ground-truth pane blanks where
the archive stops. Truth carries roll/pitch/rate-of-turn and the house bank, so the kick reads per
source: at 22:59:01Z the 24xd reads 133.1° while the Reactor still reads 35.3°.

```bash
python3 tools/replay/server.py --timeline /home/constantineau/backups/replay-jul18/timeline-fullrace
# http://localhost:8110/ — the whole race. 22:08:31Z is where the heading row first says WATCH;
# 22:58Z is the kick (attitude goes ACT); 23:56:10Z is where the compass steps out for good, and
# from 00:20Z the row reads "-90° off GPS course (vs Orca Core)" to the end of the recording.
```
`timeline-preprio/` is the pre-2026-09-07 baseline kept for before/after diffs — note it carries
the wall-clock bug, so do not compare *ages* across that boundary. Rebuilds need an ephemeral
venv (`freezegun`, `fastapi`, `httpx`, `websockets`, `pytest`), take ~28 min for 433 frames, and
must be run **after** the change you want to measure. Build to a NEW directory and swap, so a
failed build cannot destroy the working timeline — and note that the rig is hermetic now, so a
build can no longer hang on a third-party API the way the first attempt did.

To look at it:
```bash
python3 tools/replay/server.py --timeline /home/constantineau/backups/replay-jul18/timeline
# http://localhost:8110/   — real console left, ground truth right, notes -> /replay/notes.md
```

---

## Session 2026-09-07 (second pause — the block that was here before)

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

**Do these next, in this order:** ⚠️ *superseded — see the 2026-09-08 block at the top. Item 1
is DONE; the disk advice below is WRONG.*
1. ~~**The iPad surface.**~~ ✅ **Done 2026-09-08.** The engine now returns `/power`,
   `/health/sensors` and provenance +
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

**Disk: ~9 G free (90%).** ~~The obvious reclaim is
`backups/c4-boat-pull-2026-08-30/work/archive-backfill.db` (3.1 GB, no longer needed — #4 is
done)~~ and `timeline-prio-partial/`.
🛑 **WRONG — do not delete `archive-backfill.db`** (corrected 2026-09-08). It is `harness.py`'s
default `--archive` and the source of every replay timeline built so far; deleting it breaks
Race Rewind. `timeline-prio-partial/` was already gone.

---

## Session 2026-09-08 (second pause) — the P0 that was raised against working code

Picked up the one open decision from the block above — "want me to fix the heading cross-check?"
— and the first thing the fix needed was a measurement, which said the check did not need
fixing for the reason given. Kept going anyway, because two of the three findings were real.

**What the whole recording says** (`telemetry_raw`, 23:20Z Jul 18 → 02:09Z Jul 19, 291
samples/path — Postgres holds this; the spool did not):

| | |
|---|---|
| bias 30 min after the crew re-seated the sensor (23:50Z) | **+3 to +24°** — the compass was fine |
| the step | **23:56:10Z**, heading 43.6 → 125.8 → 285.7° in 70 s while COG held ~25-33° |
| 00:20 → 02:09Z, 20-min window, `danger` frames | **110 of 110**, median bias −90.2°, median spread 5.7° against a 25° gate |
| the same, with the *mixed* reference the code had this morning | **110 of 110** — the mixing never changed a verdict |

So the 35-of-36 `unknown` window (23:25 → 00:09Z) was the transition: a 20-minute sliding window
straddling two step changes, which is the only thing it can report there, and it clears itself.
**The rig only saw that stretch because the timeline ended at 00:09:35Z**, thirteen minutes after
the fault began — the end of *racing*, which was a defensible line for tactics and the wrong one
for a sensor fault. Cut a replay window where the evidence ends.

**Fixed regardless (4 commits on `dev`, not pushed):** `choose_source()` picks one named
publisher on a different physical device from the compass and the verdict says which ("vs Orca
Core"); AIS-bearing sources are refused outright; `HEADING_WINDOW_MIN` 20 → **10** (the window is
the detection latency — 5→5, 10→10, 20→19, 30→29 min from the onset — and *zero* false alarms at
any of them over the healthy race at 1 Hz, so the 20 was buying nothing); and `_circular()` no
longer raises `ValueError` when every sample agrees, which took `/health/sensors` down completely
on the cleanest input there is. The rate-aware spread gate is **explicitly dropped** — 5.7°
against a 25° gate is four times the headroom needed.

**The habit that found all of it, and the one that hid it.** Every existing case handed
`heading_bias()` a fixture of pre-paired samples, so the module looked correct while the read
path underneath chose no reference at all. The moment a test let `assess()` build its own series
it produced the `ValueError` on the first try. **Test the read path, not only the check** — and
score a *whole* recording before believing a window of it.

---

## Session 2026-09-08 — the iPad surface, and three bugs it exposed

**The thesis of the session, and it held: a check nobody can see is worth what a check that was
never written is worth.** Yesterday's session built the bank watch, the attitude/heading
cross-checks and the sensor-priority binding, measured all three against the real race, and left
them reachable only over HTTP. Putting them on the iPad took a morning; *looking* at them found
two defects in the code that had been declared finished a day earlier, one of them in the
measuring instrument itself.

**What shipped on the dashboard** (`pi/console/dashboard/`, one commit, no engine changes beyond
the new `/health/sensors` payload):

| surface | what it answers |
|---|---|
| **HOUSE BANK** tile | level, drain rate, hours to the 11.0 V brownout floor; detail adds the raw/min/decision figures and the unconfirmed-thresholds warning |
| **DATA** → instrument health | one chip over five cross-checks; detail lists each with its own verdict |
| **⚑ on any tile** | this number is coming off a backup sensor — hover/tap says which |
| **DATA detail table** | every channel → device → priority rank → age → ⚑ backup / ƒ computed / ≠ sources disagree |

Nine tiles on an eight-cell grid: the two SYSTEMS reads share the last cell, stacked
(`.tile-pair` / `.tile.mini`), so the seven sailing tiles keep the footprint the crew has learned.
DATA and BANK belong together — on Jul 18 the flat bank is what killed the instruments.

**The engine side** is one aggregated endpoint so the chip has a single source of truth:
`sensor_health.assess(conditions=…)` now carries a `provenance` block —
`assess_provenance(channels, ais_excluded)`, pure, so the rig and the tests see what the boat
sees. Three checks: **policy_binds** (every matcher names a device on this bus), **lead_source**
(which channels are on a backup), **own_ship** (the AIS read filter, with the excluded list as
positive evidence it bound). 21 new assertions in `test_sensor_health.py`.

**A distinction worth keeping:** `lead_source` separates a ranked sensor that **went silent**
(happening now → `warn`) from one that has **never published** the channel (a false premise in
the policy → a standing `note`, status left `ok`). The Orca Core is ranked first for
heel/pitch/rate-of-turn/heading and published none of them during the race, so conflating the
two would leave the chip permanently yellow — which is the same as switching it off.

### Bug 1 — the replay rig's frozen clock only reached one endpoint

freezegun's `DEFAULT_IGNORE_LIST` contains `'threading'` and it decides whether to serve the
frozen clock by inspecting a bounded window of the call stack. `TestClient` runs each sync
endpoint on an AnyIO worker thread, so for most endpoints the `threading` frame sat inside that
window and `time.time()` returned the **real** wall clock. It stayed hidden because the one
endpoint it did not affect is `/conditions` — `get_strip()` adds a stack frame, pushing
`threading` out of view — so the first thing anyone checks looked right.

| in the same frame | as built | clock fixed |
|---|---|---|
| `/sources` last-seen age | **4,394,415 s** (51 days) | −0.9 s |
| `/conditions/full` channels `fell_back` | **18 of 18** | 5 of 18 |
| `/conditions` `data_age_seconds` | −1.0 | −1.0 |

Nothing previously *measured* through the rig used a `time.time()`-derived age, so the published
before/after numbers stand. But it was a precondition for this session: every channel reading
"the ranked sensor is stale" is indistinguishable from a real failover, and would have been
reported as one. Fix: `freezegun.configure(default_ignore_list=[])` in `harness.build()`.

### Bug 2 — the bank status flapped, and only the tile made it visible

`_tripped` decided on `min()` over the *sliding* 45-minute window. That is discontinuous in
`now` — a dip enters the window in one step and leaves it 45 minutes later — so the verdict
toggled on window arithmetic rather than on anything the battery did, and the release band
written to prevent exactly this never got a say because the early-out "never tripped in this
window" bypassed it.

Measured over the full-res archive (18,554 samples at ~0.7 Hz): **56 status changes → 22**, and
the seventeen `danger` frames were **each a single 30 s frame**, all between 19:13Z and 19:53Z.
The tile would have flashed red for half a minute and gone amber again, seventeen times, while
the bank sat flat. Fixed by deciding on the **median of the raw samples in the dwell** (~420
samples, slides smoothly, crosses a line once). `POWER_CLEAR_MARGIN_V` is **removed** rather
than left doing nothing — the median *is* a release band a short bounce cannot move.

Two consequences: `charging` no longer clears a `warn` (it sat above the warn test, so 11.71 V
read `ok` on a +0.12 V/h wobble), and **the race never reaches `danger`** — the bank plateaued at
a 11.64–11.72 V median for the last 3½ hours, settling just *above* an 11.60 V line nobody has
confirmed. That is the calibration question, not a missing alarm, and it is the sharpest argument
yet for someone checking the bank. The sags never reach the floor either: **0.0% of samples
≤ 11.0 V in every hour**, absolute minimum 11.08 V.

**Also re-measured, superseding the backlog's figures:** first `warn` at **13:04:30Z**, 7.6 h
before the archiver died. The old 14:25Z / 16:10Z numbers came from the decimated Postgres spool,
where a 10-minute dwell holds two or three points — this module is written for the 0.7 Hz onboard
feed and says so now.

**The assertion that would have caught it, and now does.** `test_power.py` scores the verdict
for **stability**: a bank parked 5 mV off the danger line at 0.7 Hz with deterministic load sags
must produce ≤1 status change in 120 polls (it produces 0). Nothing scored stability before,
which is exactly why a defect this visible survived a day — every assertion asked "is it right at
moment X", and a readout that is right every other poll passes all of them. The console's own
flapping defect was fixed one day earlier by the same reasoning; the lesson did not travel.

### Bug 3 — `display:flex` beats the `hidden` attribute

The RACE CHECKLIST bar, whose whole design is "appears only when something is due", was on
screen permanently as an empty red strip, and the CURRENT SAILS bar showed before any sail state
had loaded. Both JS paths set `.hidden` correctly and neither could take effect;
`.strategy[hidden]` and `.detail[hidden]` already carried the guard. **Seventh instance of
designed, seeded, wired, and silently not in force.**

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

## Disk — ~~the binding constraint~~ RESOLVED 2026-09-09 (55 G free, 43%)
Local `/` is 96 G, **55 G free**. Boat card is 115 G, 75 G free. Everything below was archived to
`gs://constantineau-c4-archive` (restore-verified) and deleted; read the top block before
planning around any of it. What is left under `backups/`:
- `c4-boat-pull-2026-08-30/work/archive-backfill.db` 2.9 GB — 🛑 the replay rig's archive, keep
- `race-data/archive-jul15-race.db` 459 MB — the Jul 15 race at full res, cut from the salvage
- `replay-jul18/` 300 MB — the timelines and the materialised spool
- ~~`pi/sk_archive/archive.corrupt-20260718.db` 9.6 GB~~ salvaged, then archived + deleted
- ~~`pi/sk_archive/archive.db` 2.1 GB~~ malformed; archived + deleted
- ~~`recovered/archive-recovered.db` 2.0 GB~~ fully in Postgres; archived + deleted
- ~~`recovered/archive-jul1517-recovered.db` 8.5 GB~~ archived + deleted; **its Jul 15 race lives
  on in `race-data/` and in Postgres**, the delivery hours only in GCS

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
