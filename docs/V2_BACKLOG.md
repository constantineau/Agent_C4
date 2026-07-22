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

## Triaged v2.0 work

### In-race UX (console, dashboard, coach)

- **P0 — Fix Time to Mark (rated 0/10, broken during the race).** Pull race-day logs from the
  Pi archiver, find out what actually failed (data feed? mark sequencing? the tile itself?),
  fix, and add a dockside/underway self-check so a dead tile is caught before the gun.
  Includes the in-tile course map, which also wasn't working.

### Strategy & playbook

- **P1 — Rebuild the playbook approach from scratch (rated 1/10).** v1's dense prose plays
  were overwhelming and made no sense under race load. v2 design goal: glanceable, actionable,
  minimal text — decide during the retro what (if anything) from the scenario-fan/matcher
  machinery survives. Ground the redesign in what the race actually demanded.

### Weather & routing

### Onboard hardware / deployment (Pi, Orin, N2K)

### Learning loop (Lab-4, retro, LoRA)

### Infra & ops

---

## Dropped / explicitly out of scope

- (record anything ruled out during triage so it doesn't resurface)
