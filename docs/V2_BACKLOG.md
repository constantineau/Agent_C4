# Agent_C4 v2.0 Backlog

Post-race (Bayview Mackinac, 2026-07-18) feedback + carry-over items, triaged for the v2.0 cycle.
Started 2026-07-22. Raw feedback gets captured under "Race feedback (raw)" first, then triaged
into the themed sections with a priority.

Priorities: **P0** = fix before next race outing · **P1** = core v2 work · **P2** = nice-to-have / research.

---

## Race feedback (raw, untriaged)

### #1 — Component report card (2026-07-22)

| Component | Rating | Verbatim | Open questions for triage |
|---|---|---|---|
| Playbook | 1/10 | "limited/no value during the race" | Why: wrong/stale plays (v1 bundle was aboard)? scenarios didn't match reality? matcher surfaced them poorly? or just not consulted under load? |
| Forecast | 5/10 | "some value" | Accuracy problem vs presentation problem? Which horizon broke down? |
| Time to Mark | 0/10 | "didn't work" | Broken outright (no data / wrong numbers / crashed) or unusable output? |
| Fleet | 7/10 | "useful but could use improvement" | What was missing — standings freshness, handicap correction, over-the-horizon tracker? |
| TWS trend | 9/10 | "very useful, should be refined" | What refinement — longer window, forecast overlay, gust band, more prominent placement? |
| Data sources | 9/10 | "useful" | Keep as-is; protect from regression. |
| Sail (crossover/guidance) | 7/10 | "useful but didn't work well" | Which part misbehaved — crossover boundaries, CURRENT SAILS state, config overlays (C0/reef defaults were never tuned)? |

### #3 — How the console was actually used (2026-07-22)

- Console was consulted **consistently** throughout the race — the surface earned its place.
- But: **way too much data presented that wasn't useful** — "takes serious time to see what
  is useful or not." The cost wasn't reading prose; it was *finding the signal* in the noise.
- **No contested decisions aboard** — the race didn't present the strategic forks the playbook
  was designed around. Decision-support at forks was a smaller need than assumed.
- Primary viewer = user (navigator role); others glanced but weren't the design target.
- User's conclusion: this is an **overall system design** problem, not a per-tile fix.
  → v2 framing: information architecture / ruthless curation of the whole console,
  with the playbook rebuild as one part of that, not a standalone feature fix.

### #2 — Time to Mark + Playbook detail (2026-07-22)

- **Time to Mark was broken, full stop** — not a presentation issue. The course-map addition
  inside the Time-to-Mark tile (plan legs to scale + live boat, commits `8d59184`/`a1958b7`)
  was liked in concept ("nice") but **also didn't seem to be working during the race**.
  → Diagnose with race-day logs off the Pi (archiver) before rebuilding anything.
- **Playbook: overwhelming walls of text that didn't make sense in the moment.** Not a
  freshness/matcher tweak — user wants to **completely rebuild the playbook approach** for v2.
  The failure is the form factor: dense prose plays are unreadable/unactionable mid-race.

### #4 — Generalize beyond the SR33 (2026-07-22)

- The tool should be **generalizable to boats other than the SR33** — not a one-boat system.
- Explicitly includes the iPad surface: **different sail combinations must surface per boat**
  (CURRENT SAILS bar chips, crossover chart, sail-guidance plays, config-polar learning are
  all currently seeded around C4's inventory: C0/J2/kite+staysail/R1…).
- Existing seams to build on: `shared/boat_profile.py`, per-boat polars + wave_coeffs in the
  Lab, the ORC-cert fleet import (already routes other boats, e.g. fleet boat Bravo on an
  ORC cert polar). Gap: sail inventory/crossovers/dashboard chips are not profile-driven.

### #5 — Onboard race optimization from the iPad (2026-07-22)

- Want the ability to **run race optimizations on weather forecasts + conditions on the boat
  itself, from the iPad** — not just the shore-side Lab optimizer.
- Today's nearest machinery: `pi/engine` `reoptimize.py` (off-script fresh route onboard —
  own polars + live Open-Meteo + frozen obstacles) exists but is a fallback triggered by the
  selector, not a user-facing "run an optimization" surface, and it's single-model
  (no multi-model blend/fan, no A/B, no map cockpit like the Lab Gameplan).
- RRS-41 posture: onboard compute on own polars + public forecast data is the legal tier —
  this is architecturally aligned, it's a UI + capability expansion of Tier 1.
- **Requirement (user, 2026-07-22): the onboard optimizer must rely on the SAME model set as
  the shore baseline (ICON, HRRR, GFS, ECMWF, GEM…), not the current single Open-Meteo blend.**
  Implementation angle: full GRIB pulls are likely too heavy for boat bandwidth, but Open-Meteo
  serves the individual models by name (`models=gfs_global,icon_global,ecmwf_ifs,gem_global,
  gfs_hrrr…`) as point/grid JSON — same underlying models, a fraction of the bytes. The bundle
  already freezes venue model-skill weights, so the onboard blend could reuse the shore
  weighting without re-measuring. Needs: multi-model fetch in the engine's wind layer,
  blend, and a coverage/staleness story for mid-lake connectivity.
- **UI decision (user, 2026-07-22): NO full per-model route fan on the iPad.** Surface =
  the blended route, plus a picker to route on ONE chosen model instead of the blend.
- **In-race model scoring → re-optimize (user, 2026-07-22):** based on conditions seen "so
  far" in the race, tell which models are likely the accurate ones TODAY, and let us
  re-optimize on those. Building blocks already aboard: `plangap.py` (own observed wind vs
  the frozen promise) and `buoys.py` (per-station obs-vs-forecast deltas) — but both score
  the single blended fingerprint. New piece: carry each model's individual promise series
  in the bundle (or fetch per-model hindcast-to-now), score each model against own
  instruments + up-course buoys since the gun, rank them ("HRRR is on, GFS is 15° left
  today"), and feed the ranking into the onboard blend / one-model re-route choice.
  This is the live, same-day complement of the shore model-skill weighting (which is
  historical/venue-seasonal).

---

## Race-data situation — ⚠ SUPERSEDED 2026-09-07, see "Replay findings" below

**The block that follows is kept for the record but its premise is false.** The race data is no
longer stranded: the Jul 18 archive was recovered on 2026-08-30 (10,445,463 readings) and the
pre-race remainder went up on 2026-09-07, so `telemetry_raw` now holds the whole race. The P0
below — *"pull race-day logs from the Pi archiver"* — is **done**, and every Pi-dependent item
here is unblocked and needs no boat access. Diagnosis now runs on `tools/replay/` (Race Rewind),
which replays the real engine and the real console against race-day data on a laptop.

## Race-data situation (checked 2026-07-22)

- **Boat Starlink is dead** (user report). Pi + Orin both offline on Tailscale, last seen Jul 19;
  the Verizon fallback hotspot isn't running either. Full-race Pi archiver data is **stranded
  aboard** until Starlink is fixed or someone connects locally (ethernet rescue @ 10.10.10.1).
  **Confirmed 2026-07-22: no boat telemetry until the Starlink is repaired — treat all
  Pi-dependent work (archiver pull, log-based Time-to-Mark diagnosis, instrument-level retro)
  as blocked; don't re-check connectivity each session.**
- **Cloud TimescaleDB has partial race telemetry**: Jul 18 has 315k rows / 120 paths but stops
  at **20:59 UTC race day** (the presumed Starlink death). Start + first afternoon have full
  instruments; nothing after.
- **YB full-race tracks SNAPSHOTTED** to `data/retro/bayviewmack2026/` (RaceSetup.json +
  AllPositions3.bin, 106 teams incl. C4, Cove Island Course) — retro fleet/track analysis can
  run now; decoder already exists in `vps/lab/app/track.py`.
- Retro sequencing: track/fleet/weather retro now; instrument-level analysis (helm %, sail
  calls vs actual TWS) waits on the Pi. **P0 when boat access returns: pull the archiver data.**

## Carry-over items (queued before the race)

| Item | Notes | Priority |
|---|---|---|
| Post-race retro on bayviewmack2026 | Debrief actual-track ingestion + Lab-4 learning loop on the real race; was planned for ~Jul 21 | TBD |
| Point-of-sail favored-side | Planned, not built: make `tactics.favored_side` point-of-sail-aware; also fixes the latent downwind leverage/favored frame-mismatch bug (docs/… see memory plan) | TBD |
| AIS ship-wind (Msg 8 met-hydro) | Commercial ships as extra live wind "buoys" feeding the upcourse/leading-indicator layer; RRS41-clean via own receiver | TBD |
| Model-skill weighting Phase 3 | Boat-obs + regime + lead-time + buoy-height (docs/MODEL_SKILL_WEIGHTING.md) | TBD |
| Optimizer UI study | Orca/Expedition GUI study → Gameplan recs; near-term: "Ensemble members" control clarity + wire ECMWF-ENS (docs/OPTIMIZER_UI_STUDY.md) | TBD |
| Matcher LoRA /brief-forgetting regression | Accepted for the race with safe fallback; revisit in the next LoRA cycle (docs/MATCHER_LORA_PLAN.md) | TBD |
| Playbook single-load-point | Playbook currently loads in TWO places (copilot PLAYBOOK_PATH + engine /playbook/load) — bit us with a stale v1 bundle aboard; unify | TBD |

---

## Replay findings (2026-09-07, `tools/replay/` against the real Jul 18 data)

**One bug explains most of the low ratings.** `navigator.py` advanced the next mark with a
*stateless proximity test* — "the first mark you are not within `ROUND_NM` (111 m) of". On a
250 nm race to a virtual gate that can never fire, so `next_mark` stayed **"Start"** for the
entire race: at 19:30Z the tile pointed at bearing 186°, due south, at a mark 16 nm astern.

Blast radius, because five of the seven rated components read that index:

| Consumer | What it did on race day |
|---|---|
| `navigator` — Time to Mark | `eta_min` null all race (VMG toward a mark astern is negative) → **0/10** |
| `matcher.py:231` — leg gating | index 0 → returns `None` → *"leg gating fails open"* → **every play applicable on every leg, all race** |
| `tactics.py:96` — leverage / favored side | `i=0` → `start=None` → leverage never computed |
| `routing.py:216` — onboard re-route | destination resolved to **the Start** |
| `buoys.py:223` — corroborators | up-course stations chosen on a bearing pointing astern |

So the Playbook's 1/10 ("overwhelming walls of text") was at least partly an **unfiltered**
playbook, not just a badly-shaped one — leg scoping, a primary relevance filter, was inert.
**Do not commit to a from-scratch playbook rebuild until it has been replayed with leg gating
working.** That is now a cheap experiment rather than a guess.

**FIXED 2026-09-07** — advance is now a plane crossing (monotone, so correct with no stored
state, which also keeps it replayable), with a kv ratchet for the pathological cases and the
close-aboard rule preserved for buoy racing. `vps/agent/test_navigator_progress.py`, 22
assertions; agent suite 16/16.

### In-race UX (console, dashboard, coach)

- **P0 — Time to Mark ✅ BOTH DEFECTS FIXED 2026-09-07.** The sequencer (above) and, separately,
  the ETA estimator: `distance / instantaneous VMC`, which assumes the boat can sail straight at
  the mark — upwind it cannot — and takes its speed from one sample.

  Estimators were scored on **arrival-time stability**: as the clock advances by *d* a good ETA
  falls by *d*, so the predicted arrival should sit still and its wander is the error. That needs
  no knowledge of the real arrival, which matters because the archive never reaches Cove Island.

  | estimator | arrival spread | median jump | worst jump |
  |---|---|---|---|
  | instantaneous VMC (as raced) | 74.34 h | 78.7 min | 33.88 h |
  | closing rate, 30 min window | 11.68 h | 2.1 min | 0.21 h |
  | polars with a beat/reach branch | 33.58 h | 0.1 min | **26.37 h** |
  | **polars, best VMG, smoothed wind** | **10.39 h** | **3.8 min** | **0.25 h** |

  The branch version is the lesson: `if twa < beat` is a **discontinuity**, so no amount of wind
  smoothing helps — drifting across close-hauled makes the answer leap hours. Maximising VMG over
  sailable headings has no branch to jump across. Snapping to the polar grid reintroduced the same
  cliff one level down; interpolating both axes cut the worst jump 1.75 h → 0.25 h.

  Residual ~10 h is largely **real** — TWS ranged 9–19 kn over those hours and across 116 nm that
  genuinely moves arrival. Only forecast-aware routing can improve on it; `/reoptimize` already
  computes per-mark ETAs and is now cheaply cached, so **wiring routed ETA into `next_mark` is
  the natural follow-up** (mind the recursion: `reoptimize` calls `get_navigator`).
- **P0 — Readouts flap between a value and "no data" ✅ FIXED 2026-09-07.** The archive proves
  this was never sensor or link loss: every strip path has **12,990 samples, one per second,
  across the whole race, zero gaps over 1 s**. It was the console — `fetchJSON` aborts at 5 s
  and maps failure to `null`, which the tile builders render as `—`, so one slow ENGINE
  response was indistinguishable from a dead sensor. `commitStatus` did not help: it dwells the
  status enum while value/sub/why come straight off the NA object, so a missed poll showed a
  dash under a still-committed "OK" dot. Compare `47c1138`, whose fix evidently did not hold.

  Fixed by holding each endpoint at its last good value for a bounded `HOLD_MS`, surfacing the
  age on the tile past `STALE_AFTER_MS`, and falling back to NA honestly after that. This makes
  the main poll consistent with the secondary pollers, which already held indefinitely
  (`if (r) App.x = r`) — bounded, because silently showing minutes-old wind is its own hazard.

  Measured in the replay rig with injected engine stalls (7 s in every 12 s), 40 s, 158 samples:

  | | eta | sail | ais |
  |---|---|---|---|
  | before — flips | 7 | 7 | 7 |
  | before — % showing "no data" | 63% | 63% | 63% |
  | **after — flips** | **0** | **0** | **0** |
  | **after — % showing "no data"** | **0%** | **0%** | **0%** |

  Hold expiry verified separately under a permanent outage: the value holds with a counting age
  badge to 30 s, then goes to `—` / "Engine unreachable" at 31 s. Genuinely-absent endpoints
  (e.g. `/forecast`) show no badge and stay blank — stale and absent stay distinguishable.
- **P1 — Ruthless curation of the console.** Unchanged from #3 above; ground it in the replay.

### Strategy & playbook

- **P1 — Rebuild the playbook approach (rated 1/10)** — but re-evaluate first, see above: leg
  gating was disabled for the whole race, so v1 has never actually been observed working.

### Weather & routing

- **P1 — `/strategy` re-optimizes the whole remaining course on every call ✅ FIXED 2026-09-07.**
  ~14 s for the remaining 259 nm on a fast x86 box, polled every 15 s by `dashboard.js`. It was
  already supposed to be cached, but the key carried raw instrument readings (position 3 dp,
  TWS 1 kn, TWD 1°) so it **missed ~97% of the time** — 466 distinct keys in 480 calls, TWD
  alone churning on 84% of consecutive polls.

  Bucketing the key just moves the problem to the bucket edges (coarse buckets still recomputed
  287×/2 h), so invalidation is now a threshold on the delta from the conditions the cached
  route was computed at. Live wind only counts when the route was actually computed from live
  wind — with a forecast reachable, `make_wind_fn` routes on the forecast and `live` is only the
  fallback. Structural change (mark rounded, new obstacles) forces a synchronous recompute;
  drift is served stale-while-revalidate so the endpoint never blocks.

  | | recomputes / 480 polls | reuse | engine CPU duty |
  |---|---|---|---|
  | before | 467 | 2.7% | ~91% |
  | **after** | **88** | **82%** | **~17%** |

  This was the gate on deploying the sequencer fix to the boat.

### Onboard hardware / deployment (Pi, Orin, N2K)

- **P0 — 🔴 THE HOUSE BANK WENT FLAT DURING THE RACE, AND THE SYSTEM NEVER SAID SO.** Found
  2026-09-07 while looking for why the archiver died. Voltage fell monotonically 12.91 V
  (pre-start) → 11.61 V mean with minima at **11.08 V** over six hours of racing, recovering
  only when the engine went on for the trip home. At **20:40:30Z**, at the bottom of that
  curve, the full-res archiver (SQLite corruption) and the em-trak AIS receiver (67,149 rows in
  the previous 100 min → **11** in the next 3.5 h) stopped in the same minute; every other N2K
  source kept reporting through the uplink. The boat retired ~3 h later.
  `electrical.batteries.0.voltage` was arriving from the Orca at ~0.7 Hz the entire time and
  appeared in **no** PRESENT table, **no** alert rule and **no** tile.

  ✅ **Bank watch built** (`vps/agent/app/power.py`, engine `GET /power`): smoothed level,
  robust trend, projected hours to an 11.0 V floor, ok/warn/danger/charging, stateless so the
  replay rig agrees with the boat. Against the real curve it would have warned at **14:25Z**
  and escalated to danger at **16:10Z** — 6 h 15 m and 4 h 30 m before the failure — and it
  does not alarm on the charging recovery. ⚠️ The absolute thresholds assume 12 V lead-acid and
  are **unconfirmed**; the bank's chemistry and capacity are recorded nowhere in this repo.
  Trust the trend/projection first, and set `POWER_*` once someone checks the bank.

  ✅ **Brownout-tolerant archiving** (`pi/archiver/archiver.py`): the corruption cost six weeks
  only because `open_db()` raised, the process exited and Docker restarted it 48 times,
  archiving nothing Jul 19 → Aug 30. It now quick-checks on startup, rotates a bad file aside
  (kept, for salvage), survives corruption mid-write and re-writes the buffered rows, requeues
  transient locks, and counts rotations in `sync_state`. `pi/archiver/test_brownout.py`.

  Still open, and the parts a person has to do: **confirm the bank** (chemistry, capacity,
  charging regime — is there a solar/alternator budget for a 40 h race at all?), and decide
  whether the Pi deserves its own supply or supercap-backed shutdown. Three SD corruptions in
  six weeks on a bus that browns out is not a coincidence, and software can only make it cheap,
  not absent.

- **P0 — 🔴 THE ARCHIVER RECORDS AIS TRAFFIC AS OWN-SHIP TELEMETRY ✅ FIXED 2026-09-07.** Root
  cause of the "unreliable source" symptom, and much worse than it first looked.

  **Fixed in two layers.** `archiver.py` now tracks the `self` context from the hello frame and
  skips foreign contexts (asymmetric on purpose — anything not positively identifiable as
  another vessel is kept, because dropping own-ship data is the worse failure); and
  `datasource_onboard` excludes AIS-bearing sources from its five archive reads, which is what
  rescues the archives already written, including the Jul 18 race the replay rig depends on.
  AIS channels are identified from the data (a source publishing AIS-only marker paths), not a
  hand-maintained list. Measured on own-ship position as `latest_value()` returns it:

  | | median | max | steps implying >15 kn |
  |---|---|---|---|
  | as raced | 5.63 kn | **4,091 kn** | **15.9%** |
  | **fixed** | 4.86 kn | **8.0 kn** | **0.0%** |

  **Archive cleanup — measured 2026-09-07 (later session), and the answer is "keep them".**
  `n2k-socketcan.43` is an em-trak B951 AIS Class B transceiver (per `sources-cache.json`) and
  contributes **3,394,173** rows to `telemetry_raw`; *every* path it publishes is AIS or AtoN,
  so there is no own-ship data interleaved to preserve. Two things follow:

  - **Retro Fleet replay out of `telemetry_raw` is impossible — do not plan on it.** Vessel
    identity was never archived (no `context`/`mmsi` column, everything flattened to one
    `boat_id`). Each AIS position carries a *unique* timestamp (92,257 distinct, zero
    collisions, over Jul 18), so rows can be regrouped into single per-vessel reports but can
    never be attributed to a vessel. MMSI is gone, not merely unindexed.
  - **`ais_targets` is the identified store and already has the race**: 902,710 rows / 430
    MMSIs with name, lat/lon, SOG/COG, CPA/TCPA, running 120–131 vessels through the start
    hours. Build Fleet replay on that table.

  Recommendation: **do not DELETE the 3.39 M rows.** The read-side filter already hides them,
  they are ~1% of a 36×-compressed archive, and `telemetry_raw` has no PK — an irreversible
  bulk delete on a hypertable buys nothing under a "lose no telemetry" policy.

  Still worth doing, forward-looking: **archive AIS with an `mmsi`/context column.**
  `ais_targets` is uplink-fed and — per the design — `/ingest/ais` is **best-effort, not
  queued** ("stale positions must not replay"), while telemetry is spooled and replayed later.
  Measured consequence on Jul 18: AIS ingest stopped at **20:59:00Z** and never resumed, while
  the telemetry spool kept flowing to **00:09:35Z** — so the boat has **zero fleet data for the
  last ~3 hours of racing**, including the retirement decision, even though the link was good
  enough to carry 33,014 telemetry rows across 74 paths in that window. Not-queued is the right
  call for a *live* CPA display and the wrong one for history. An identified AIS column in the
  boat's own archive is the only route to fleet history that survives a degraded link; it is a
  schema change, so it needs a deliberate call.

  `pi/archiver/archiver.py` has **no vessel-context filtering at all** — no `context`, no
  `mmsi`, no `self` check. Signal K's `subscribe=all` delivers own-ship deltas *and* every AIS
  target, and the archiver writes them all under the single own-ship `boat_id`.
  `pi/uplink/uplink.py` and `datasource_onboard._ingest_live()` both filter by context; the
  archiver was never given the same treatment.

  `n2k-socketcan.43` is the AIS receiver. It also carries `sensors.ais.class` (36,899 rows),
  `atonType.id` / `offPosition` / `virtual` (aids-to-navigation), `design.aisShipType.id`. Its
  `navigation.position.*` is a stream of *other vessels*, so consecutive samples imply
  impossible speeds, and its longitude range runs to **−2.4°** — the Atlantic off Africa, not
  Lake Huron.

  Because `datasource_onboard.latest_value()` is `ORDER BY time DESC LIMIT 1` across **all**
  sources — freshest wins, no sanity check — the engine consumed another vessel's position for
  a sixth of the race:

  | source | median implied speed | max | samples implying >15 kn |
  |---|---|---|---|
  | **mixed (what `latest_value` returns)** | 6.5 kn | **48,876 kn** | **17.2%** |
  | `n2k-socketcan.15` | 6.4 kn | 8.9 kn | 0% |
  | `n2k-socketcan.3` | 6.4 kn | 8.5 kn | 0% |
  | `n2k-socketcan.43` (AIS) | **15,859 kn** | **171,588 kn** | 72.7% |

  Consequences: position/COG/SOG glitches feeding everything downstream; spurious re-optimize
  invalidation; and a latching hazard for the mark ratchet (guarded in `b355058`, but the data
  fix is what actually solves it). It also fully explains the earlier "n2k.43 throws excursions"
  finding — those were other ships' SOG and COG.

  Two fixes needed, both boat-independent to write: **(1)** filter contexts in the archiver so
  AIS never lands under `boat_id`; **(2)** stop `latest_value()` trusting whichever source wrote
  last — a `source_priority` table already exists (migration 003) but is consumed only by the
  **cloud** path (`tools.py:65`) and was never wired into the onboard datasource.
  ⚠️ The existing archive is already contaminated, so the replay rig and any retro analysis
  should exclude AIS-bearing sources until it is cleaned.

- **P1 — 🔴 `source_priority` has never matched anything: the boat has no sensor-priority
  policy in force.** Found 2026-09-07 (later session). Not just the missing onboard wiring
  noted above — the **cloud** path that does consume the table never matches either, so every
  channel silently takes the `"no preferred source fresh — using freshest available"` branch of
  `tools.py:_choose_preferred`. This is the same shape as the other four defects: designed,
  seeded, wired, and quietly not in force.

  Cause: the seeded matchers are **device names** (`orca`, `24xd`, `reactor`, `gnd`, `gwind`,
  `943`) but archived and uplinked `$source` labels are **N2K addresses** (`n2k-socketcan.15`).
  `m in r["source"].lower()` therefore cannot match on any channel, on any boat. The identity
  lives only in Signal K's `sources-cache.json`, which neither the archiver nor the uplink
  records. The SR33's map, recovered from the Aug 30 pull:

  | source | device | | source | device |
  |---|---|---|---|---|
  | `.0` | Garmin GND10 | | `.6` | Garmin GHC 50 |
  | `.1` | Garmin Reactor 40 (autopilot AHRS) | | `.11` | Garmin GPSMAP 943 |
  | `.2` | Garmin Intelliducer | | `.12` | Garmin GNX20 |
  | `.3` | Garmin GPS24xd | | `.15` | **Orca Core** |
  | `.4` | Garmin GST10 | | `.43` | em-trak B951 AIS |

  Two of the table's premises are also false, measured over the race window
  (17:03:31–20:40:31Z):

  - **heel / pitch / rate_of_turn: rank 1 (`orca`) published nothing at all.** Heel and pitch
    came from Reactor 40 (65,857 samples) — the source the table annotates *"autopilot AHRS
    (non-racing only)"* — and GPS24xd (52,880). Freshest-wins gave the race to the
    non-racing sensor. **Impact is small, though**: the two agree to 1.29° median / 2.98° max,
    never crossing the 6° disagree threshold. Fix the ranking because it is wrong, not because
    it corrupted this race.
  - **aws / awa: rank 1 (`gwind`) is not a distinct source.** The masthead reaches N2K
    *through* the GND10, so the real contest is GND10 (130,990) vs Orca Core (128,107) —
    alternating ≈50/50, sample to sample. They disagree on **AWS by 0.35 kn median, 1.75 kn
    p95, 5.9 kn max**, against a 0.6 kn "sensors disagree" threshold: the displayed apparent
    wind jitters by *which device reported last*. TWS is unbiased (Orca vs `derived-data` both
    mean 15.40 kn) but still carries 0.15 kn median / 0.84 kn p95 instantaneous jitter into the
    TWS-trend tile — the highest-rated component (9/10), fixable for free by pinning a source.

  Work: (a) resolve `$source` → device once and record it (archiver/uplink column, or a
  committed per-boat address map regenerated from `sources-cache.json` — addresses can be
  re-claimed, so a map needs a staleness check); (b) re-rank heel/pitch/ROT against what the
  Orca actually publishes; (c) wire priority into `datasource_onboard.latest_value()`, which
  still ignores it entirely; (d) assert it: a startup check that every seeded matcher resolves
  to a live source, because an unmatched matcher is currently indistinguishable from a
  satisfied one.

- **P2 — 5.5 M synthetic rows in `telemetry_raw`, timestamped as if live.** The Signal K
  sample-data provider wrote `n2k-sample-data.{115,160,129,43}` — STW, AWA/AWS, depth, water
  temp, position, SOG/COG, current set/drift, battery — with 2026 timestamps from 06-16 to
  07-30 (5,506,867 rows; a stray 8,355 keep the sample file's 2014 stamps). **The race is
  clean** (zero sample rows Jul 18–20). Overlap, measured by the hour:

  - **493 hours** carry synthetic *and* non-synthetic rows (Jun 16 22:00 → Jul 30 23:00),
    5,275,089 synthetic against 778,892 other — but almost all of that "other" is
    `derived-data`, i.e. the engine's own output, itself computed from the demo feed. That
    stretch is simply the bench period, and reads there are synthetic whether or not they
    are filtered.
  - **Only 6 hours** put synthetic data against *real N2K instruments*: **2026-07-15 20:00 →
    2026-07-16 02:00Z**, ~6,900 synthetic rows/h against 1,415–15,515 instrument rows/h. That
    is the window where a replay or retro read can silently prefer a demo value over the boat.

  The AIS filter does not catch them: `.115/.160/.129` publish no AIS marker paths. The provider is
  already gone from the boat's `settings.json` (only `n2k-socketcan` remains as of the Aug 30
  pull), so this is historical, not a live risk. Do **not** blanket-exclude these sources from
  own-ship reads — the dev bench has no other data (see the comment at
  `datasource_onboard.series`). Prefer real over synthetic *when both exist for a path*, and
  keep synthetic as the fallback.

### Learning loop (Lab-4, retro, LoRA)

- **P1 — Log Tier-2 copilot output.** The Orin's narration was never archived, so it cannot be
  replayed for Jul 18 (the only `crew` paths that race are `crew.sail.state`/`crew.session`).
  Every future debrief is blind to what the copilot actually said unless this is fixed.

### Infra & ops

- **P2 — Capture AIS into the replayable record.** The archiver stores own-ship contexts only;
  AIS went to the cloud `ais_targets` table by a separate path, so the Fleet tile (7/10) cannot
  be replayed. Postgres has the data — wiring it into `tools/replay/` would close the gap.

---

## Dropped / explicitly out of scope

- (record anything ruled out during triage so it doesn't resurface)
