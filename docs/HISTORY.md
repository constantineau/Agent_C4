# Development history — Agent_C4 / SR33 AI Navigator

A chronological outline of how the system was built. Each entry is a shipped, verified
milestone (dates are commit dates on `main`). This file is the project's development
record — the *what and when*; the *how it works today* lives in `CLAUDE.md`, `DESIGN.md`
and the per-component READMEs. Detailed design rationale for the big arcs is in the other
`docs/` files, which are kept (some explicitly marked superseded) as part of this record.

## 2026-06-16 — Foundations (Phases 0–2, the data paradigm)

- **Phase 0** scaffold: monorepo, dev/prod compose isolation, TimescaleDB schema, stubs.
- **Phase 1**: containerized Signal K + uplink (boat → cloud); `vcan0` bench so the whole
  pipeline runs with no boat; the SR33 ORC Speed Guide ingested as the **boat-speed gospel**
  (real polars + sail crossovers in the agent's context).
- **Sensor research**: full N2K device matrix (`pi/sensors.md`); Orca Core chosen as primary
  heel source; gWind Race apparent-wind implications captured.
- **Collect-everything paradigm**: every `(source, path)` reading stored verbatim
  (`telemetry_raw`); per-quantity source priority + automatic failover; the agent prompted
  to sensor skepticism. (The original wide `telemetry` table this superseded was finally
  dropped 2026-07-08, migration 006.)
- **Phase 2**: independent full-res onboard archive (SQLite, crash-safe) + resumable
  backfill; outage-proof disk-backed uplink queue; true wind via `signalk-derived-data`.
- **Helm fatigue index**: multi-signal composite (heading/reversals/heel/AWA-detrended/
  speed deficit) vs the boat's own trailing baseline.

## 2026-06-17 — Cloud agent complete + the three-tier pivot (Phases 5–7, 9.0–9.2, Lab-0)

- **Phase 5** iPad crew interface: day/night, sail dial, course plot + navigator, tactics,
  weather routing.
- **Phase 6** alerting + summarizer + polar mining (6.0 live AIS w/ CPA/TCPA · 6.1 debounced
  alerts over WebSocket · 6.2 on-demand summary/debrief · 6.3 observed-vs-rated polar mining ·
  6.4 consistency sweep; the `Decimal` alerts bug found + fixed).
- **Phase 7** server-side shared-password web auth + TLS scaffolding (nginx + certbot).
- **RRS 41 compliance review** (`docs/RRS41_COMPLIANCE.md`): the 2026 Bayview Mackinac NOR
  §2.1(d) makes customized off-boat advice while racing prohibited outside help; the
  "make-it-public" loopholes analyzed and rejected. **This drove the three-tier pivot**:
  deterministic engine onboard (legal) / onboard LLM (optional) / cloud between races.
- **Phase 9.0** data-access abstraction (`datasource.active()`, cloud ↔ onboard backends);
  **9.1** onboard engine service on the Pi (:8200, no LLM, no cloud); **9.2** server-side
  fail-closed race-mode gate + the onboard race console (:8091, boat-local).
- **Lab-0**: the RaceDefinition schema + validator; dual-input race ingestion (auto-discover /
  URL / PDF → Opus extraction → human review → save); comprehensive requirements checklists;
  Course & Marks review + geocoding; the homework→onboard course loader.

## 2026-06-18/19 — Lab-1 optimizer + Orin bring-up + the crew dashboard

- **Lab-1**: multi-model GRIB wind field (GFS/NAM/HRRR/GEFS/ECMWF, key-free, lag-aware
  freshest-cycle pick) + self-contained isochrone optimizer + Opus briefing; model spread
  reported as route confidence.
- **Orin Nano bring-up** (9.4): the forum "super-variant" bootloader hack **bricked the unit
  once** (recovered by SDK-Manager reflash; the runtime `nvpmodel` path is the safe one —
  warning preserved in `pi/orin/DEPLOYMENT.md`); the planned MLC path didn't fit R39 —
  pivoted to **from-source Ollama (cuda_v13 @ sm_87)**, Qwen2.5-7B q4 at ~12 tok/s, 100%
  GPU, systemd-persistent turnkey appliance (the MLC-era docs are kept as superseded history).
- **SR33 copilot** decision-support layer (:8300): bounded read-only engine-fact tools,
  grounding validation (ungrounded output dropped), deterministic fallback always works.
- **Crew dashboard** phases 1–4: fixed status grid → live engine wiring → LLM commentary →
  streamed tap-to-detail; simplified to higher-order tiles on crew direction.

## 2026-06-20/21 — Map accuracy + Lab-2 playbook + routing fidelity begins

- **Obstacle avoidance** (2a): global coastline + race zones + island buffer disks.
- **Map-accuracy arc**: NOAA ENC vector charts (draft-aware shoals), BoatProfile (draft →
  depth no-go), GRIB-on-ENC Leaflet slippy map; feedback widget → GitHub issues.
- **Lab-2a/2b**: per-model fan-out → side variants → **Opus synthesis → signed playbook
  bundle** (`c4.playbook/v1`), frozen at the gun, loaded onboard; the copilot verifies the
  signature.
- **Routing fidelity 2b**: per-leg sail plan + the reviewable boat sail model (incl. J1/J2/J3
  jib change-downs the cert can't see); **degraded-GRIB hardening** (coverage gate,
  route-sanity, cycle fallback); GSHHG full-res coastline backstop (sub-nm islands).

## 2026-06-22/23 — Optimizer UI study + fleet tactics

- **Optimizer UI study** (`docs/OPTIMIZER_UI_STUDY.md`, Orca/Expedition gap analysis)
  implemented through **all tiers**: ensemble fix + ECMWF-ENS, legends/animation, isochrone
  frontier + laylines + CSV, the per-model route fan (the confidence moat made visual), wind
  barbs/shading, Auto/Fast/Fine resolution, and the Tier-3 Orca-style restyle (Control
  Center + map-led cockpit + ride-along scrub).
- **Routing 2c/2d**: VMG-gate + cone-prune + anti-over-tack; mark-approach overstand gate +
  rounding-side standoff.
- **Copilot narration**: proactive, grounded, timed crew callouts (rounding prep, playbook
  branch, sail change-down) with per-route speak-once dedup.
- **AIS/Fleet**: source-agnostic AIS (same CPA/TCPA code cloud + onboard), the AIS/Fleet
  tile, then **handicap-aware fleet tactics** — AIS→roster match + ORC corrected-time delta +
  the over-the-horizon public-tracker source (verified: bycmack = YB Tracking).

## 2026-06-28/30 — Currents, waves, Lab-4 learning loop, fleet auto-import

- **Routing 2e/2f/2g**: the finish "scramble" fixed (cumulative tack cost + position prune +
  layline commit) + honest tack counter; island rounding-side enforcement (wrong-side
  barrier); **sail-aware routing** (per-sail polars, peel cost + hysteresis — the sail is
  part of the search state).
- **Water currents** (NOAA GLOFS/LMHOFS): drift in the step + wind-over-water correction,
  threaded through the fan + playbook; map overlay.
- **Realized speed**: helm factor + conservative wave model → **NOAA GLWU** provider;
  sea-state heatmap overlay; per-run opt-out.
- **Lab sections built out**: Lock-in & Deploy, Fleet, Rules/Safety/Checklists, Learnings,
  Monitor, Debrief; C4 Energy brand pass.
- **Lab-4 learning loop**: Debrief actual-track ingestion (GPX + the reverse-engineered YB
  AllPositions3 binary) scored vs the oracle re-route; persistent learning archive;
  **human-approved** helm/polar refinement (propose never mutates); helm may exceed 1.0 with
  current-corrected measurement.
- **Fleet auto-import**: YB entry list + ORC public cert DB + regatta websites
  (YachtScoring API, iframe-follow for bycmack) → reviewed draft roster.
- **GRIB parse isolation**: cfgrib segfaults survive as skipped frames (persistent child
  process); pandas pinned around a numpy-3.0 break.

## 2026-07-01/03 — Lab-3 onboard executor + strategy synthesis + model skill

- **Lab-3**: route-deviation + forecast-drift branch triggers (Schmitt-hysteretic, on the
  iPad Strategy card) → the unified **selector** (HOLD / SWITCH / OFF-SCRIPT) → the onboard
  **re-optimizer** fallback route (own polars + Open-Meteo + frozen island/zone obstacles —
  the homework pattern), with a hoistable sail plan. The PLAYBOOK tile unified onto
  `/selector` (one source of truth).
- **Lab-4 condition attribution**: wave-corrected helm (`helm_pct` flat-water-equivalent),
  wave-coefficient calibration from the archive (incl. deadband knee-fit), multi-race trend,
  reshape gate.
- **In-race strategy synthesis** Phases 0–3: Tier-1 deterministic cross-signal digest
  (concordance over shift/drift/deviation/fleet) → Tier-2 LLM phrasing → the iPad SYNTHESIS
  apex + auto-coach callout → off-book chaining (a departing rec carries a concrete onboard
  re-route).
- **Dashboard de-dup**: VMG + Tactics tiles retired (8 tiles, 4×2); audio alert signal
  (visual-only narration + attention tone); racer-native wind language (no veer/back).
- **Venue model-skill weighting** Phases 1–2c: forecast-vs-observed verification (METAR +
  NDBC, Open-Meteo historical + deep AWS GRIB to 2005) → seasonal recency-weighted de-biased
  weights auto-applied to the blend + the GamePlan backtest panel. Findings:
  `docs/MODEL_SKILL_FINDINGS.md` (mesoscale wins on the lake; GFS weakest; global veer bias).

## 2026-07-06 — The descope, the retro study, Playbook v2 Phase A

- **Strategy-LoRA system removed** after one day live (judgment-DPO labeling ranker at
  /training/): the user locked the descope — **the onboard LLM never originates strategy**;
  it narrates + condition-matches. Replaced by the gated `docs/MATCHER_LORA_PLAN.md`.
- **Fleet retro study** (`docs/RETRO_STUDY.md`): every 2025 boat optimized on its own ORC
  polar with the forecast knowable at its gun, scored vs its real YB track. Headline:
  **execution beat geometry in 2025** (polar% correlated with rank in every division; XTE
  didn't); thresholds seeded Playbook v2 (behind median 157 min / p90 384; XTE 3.5/6.0 nm).
- **Playbook v2 design locked** (`docs/PLAYBOOK_V2.md`) + **Phase A**: copilot descope
  enforced in code; Fable-primary → Opus-fallback model chain; forecast-horizon fix (a >5-day
  start had routed on zero frames).

## 2026-07-07/08 — Playbook v2 B/C/D: the play library, end to end

- **Phase B**: scenario registry (rotations ±10/20°, TWS ×0.75/1.25, timing ±3h, sea state)
  fanned through the SAME blended field → plays with machine predicates + narratives;
  corridor verdict (2026: GEOMETRY — models split, the opposite of 2025); venue stats frozen
  from the retro archive; synthesis as a background job. Live e2e at the real Jul-11 start.
- **Phase C**: internal plays — pace re-routes from each intermediate mark, gear-loss
  inventory re-runs, sail-guidance crossover calls, low-maneuver variant, rejoin-vs-continue
  tabulation. Mark-approach loop fix (adaptive endgame step + monotone gate). Code 0 +
  mainsail reef points as crew sail-config overlays.
- **Phase D**: the Tier-1 onboard **matcher** (`/plays`, Schmitt arm/clear, crew sail-state +
  gear toggle, armed-plays card, coach callout) + Tier-2 ranked `play_matches` in the
  strategy brief; fan-depth control; boundary bisection (arm at the located flip); NDBC
  **buoy corroborators** (confidence-raising, never gating); then the last v1 limits closed —
  **polar_pct** as a windowed live matcher signal and **applicability leg gating**
  (hard for pace plays, advisory for sail guidance) + `play_matches` rendered on the
  Strategy card.
- **Whole chain deployed** (lab / bench / real Pi / Orin); codebase audit sweep (gzip
  everywhere, legacy telemetry table dropped, superseded deploy script + systemd units
  removed, dead code out, docs synced).

## 2026-07-08 (later) — Long-term onboard living: race log, sail configurations, ORC helpers

- **Race-log sessions**: the iPad's one-tap record switch (no Lab prep needed) → only
  session windows are kept long-term + backfilled; the archiver prunes everything else
  after 14 days (fail-safe: never deletes blind) — day sails and deliveries never
  accumulate or leave the boat. The Lab Debrief gained the boat's own log as a track
  source (full-res, sail changes riding along).
- **Sail configurations**: the crew sail state became a SET (C0 alone · C0+J2 ·
  kite+staysail; SS joined the inventory) + main-reef state, picked on the dashboard's
  SAILS bar, timestamped into an append-only onboard log — closing the 9.3 "hoisted sail
  never persisted" data gap. Play predicates match by membership.
- **ORC ratings**: multi-country enrich (USA+CAN in one pass) + a per-boat fuzzy
  cert-candidate picker for unrated roster boats.

## 2026-07-08 (evening) — per-configuration polar development

- The sails bar became **CURRENT SAILS** and its log became the innovation record: boat-log
  debriefs attribute every fix to the crew's active configuration, the learning archive keeps
  performance bins per config, and the Learnings tab grows observed curves for combinations
  the crossover chart doesn't rate (the team innovates while racing; the data follows). The
  sail-log backfill was also scoped to race-session windows like everything else.

## 2026-07-08 (evening) — the Pi↔Orin ethernet goes live

- The direct cable was plugged (both ends pre-addressed: Pi eth0 10.10.10.1 ↔ Orin enP8p1s0
  10.10.10.2): sub-millisecond RTT, the engine answers the Orin in ~4 ms. The console's
  `/copilot/*` proxy default moved off the Orin's Tailscale address (a hidden WAN dependency)
  onto the cable — the whole in-race loop (iPad → Pi console → Orin copilot → Pi engine) now
  runs on boat-local wire + Wi-Fi with no WAN anywhere, and the engine↔copilot leg survives
  even a boat-AP failure.

## 2026-07-08 (night) — remote-operations hardening (the boat moves aboard long-term)

- The system stays on the boat in Sarnia on Starlink, owner-powered. Verified: Tailscale key
  expiry disabled on both nodes, full power-cycle survival (restart policies + boot-enabled
  units), storage self-limits. Built: the **cross-hop** — ed25519 keys exchanged so either box
  reaches the other over the Pi↔Orin ethernet if one drops off the tailnet. `docs/REMOTE_OPS.md`
  carries the access ladder, the session rules (never touch wlan/tailscale/default route; never
  `compose down` remotely; one box at a time), and the accepted residual risks.

## 2026-07-09 — the watch system (crew rotation aboard)

- **Watch plan end to end**: `shared/watchplan.py` (explicit block list is the canonical format —
  teams A/B/ALL-hands, absolute epochs; pattern GENERATORS 4/4 · 3/3 · 6/6 · Swedish 4-4-4-6-6 are
  conveniences; hold/swap/all-hands quick edits with a capped edit log) → engine + cloud
  `GET/POST /watch` (kv-persisted, restart-proof) → Lab Races-tab **Watch plan card** (generate →
  hand-edit → Save; `watch_plan` joined the `_write_race` SI-re-ingest preserve list; deploy
  readiness pill + homework `watch_load` + a jq load command) → the **CREW tile** (formerly
  C4 Energy; face = energy + who's-on + countdown, T-15 `watch`/T-5 `act` escalation; detail =
  schedule with tap-to-cycle team blocks + Hold +30m/+1h · Swap · All-hands quick edits, live
  only) → auto-coach **watch-change callouts** (T-15 wake-the-next-team — rides the audio
  attention tone — and T-5 handover, staged show-once like the rounding prep). Advisory-only
  maneuver coupling (the user's call): the SAIL tile + the watch/rounding callouts note when a
  pending sail change or rounding lands near the boundary ("full hands at the change" / "the
  incoming watch takes this rounding") — routing/matcher never re-time anything.
- **Fixed a latent narration bug** the watch work surfaced: the staged-callout threshold pick
  was first-match, not tightest-match, so the rounding prep pinned at the :15 heads-up forever —
  the :10/:5 escalations (and the act-level audio tone at the mark) never fired. Both rounding
  and watch stages now take `min()` of the matching thresholds.

## 2026-07-09 (later) — the known-answer playbook backtest, and the fix it forced

- **PLAYBOOK_V2 §8's pre-race validation, run for real** (RETRO_STUDY.md §8): the 2025 playbook
  synthesized exactly as-of-gun from the pinned archive GRIBs (fingerprint rebuilt from the same
  frozen blend), the realized race replayed through the REAL selector/matcher along two YB tracks
  (Div-I winner + rank 88) on an hourly HRRR-f00 oracle chain. Verdict: **the gun bundle
  recommended RIGHT — the side that paid 18:2 — at 0.74 consensus**; the selector held the plan
  in steady state but chattered Switch→Left downwind (15 short excursions, 13 downwind, 17–21%
  of race time). That was locked input #5, designed and never implemented.
- **Selector: downwind pivot-hygiene confirmation clock** (`SEL_SWITCH_CONFIRM_DOWNWIND_S`,
  default 60 min, clear-fast): downwind the decisive SWITCH must sustain before it fires; upwind
  unchanged. Wrong-side time on the replay fell to 6–10%; survivors are hour-plus real shifts.
  `get_selector(now=)` is injectable for replay. New harness kept: `vps/lab/app/pbbacktest.py`
  (as-of synthesis + oracle sampling) + `vps/agent/backtest_replay.py` (host decision replay).

## 2026-07-09 (night) — the COACH WINDOW: crew briefs + three new engine signals

- **Three deterministic engine signals** widen what the boat can synthesize from: `trend.py`
  (1 h/3 h TWS build/fade + TWD walk rates, bucket means from the own archive — an observation,
  not an alarm; `GET /trend`), `plangap.py` (own OBSERVED wind vs the bundle's frozen
  `forecast_fingerprint` promise for here/now — the honest "the promised breeze isn't here" that
  drift's forecast-vs-forecast design deliberately can't make; Schmitt `PLANGAP_*` bands +
  position-honesty caveat; `GET /plangap`), and the matcher's **distance-to-trigger**: every
  predicate reports a normalized `closeness`, a quiet play's weakest predicate is the play's,
  and the closest quiet plays ride `/plays` + the strategy digest as the **watchlist** ("what
  flips the plan", with the live number against the threshold). All three feed the play
  predicates (`tws_trend_kn_per_hr`, `plangap_twd_deg`, …), the strategy picture, and the
  Tier-2 grounding allow-list (`get_trend`/`get_plangap` joined the copilot tool surface).
- **CREW BRIEFS** (`pi/orin/copilot/briefs.py`): four synthesis surfaces — **handover** (T-15:
  the race the incoming watch inherits, incl. the SAIL-WINDOW clock: trend rate vs the frozen
  reef/C0 thresholds → "you cross reef-1 in ~2 h"), **recap** (the last hour: wind from→to,
  polar %, plan-vs-reality, top rival, sail-log events), **mark** (~T-20 pre-brief: the leg
  after, the staged sail change, who takes the rounding from the watch clock, next leg's
  plays), **watchlist**. Deterministic sections always (engine numbers only); the LLM only
  REPHRASES into crew language (schema-forced; fallback = the deterministic text). **Data
  honesty** leads every brief (stale instruments / fallback link / no plan aboard). The
  auto-coach timer schedules them show-once (rotation/hour/mark keys); `GET /briefs/{kind}` on
  demand; the held set rides `GET /coach`.
- **The coach window (iPad)**: the commentary panel is now the coach's FACE — tap it for the
  full window (brief cards + 4 Brief-me buttons + active callouts + recent-brief rail). Tile
  hooks: PLAYBOOK detail's stack gains the plan-gap line + the CLOSE-TO-FLIPPING watchlist;
  SAIL detail the trend read; FORECAST detail the promise-vs-actual line; CREW/ETA details get
  handover/mark brief buttons. Playwright-verified; engine+console rebuilt on the bench;
  12 unit suites + bench_copilot green (briefs section added).

## 2026-07-10 — the gameplan on the GPS screen (GPX → GPSMAP 943)

- **Question answered: can the system drop waypoints/a path on the boat's chartplotter?**
  Researched against the Garmin GPSMAP touch-series PGN tables: the 943 **imports GPX** user
  data (memory card / ActiveCaptain) and draws it; it **receives 129284/129285** (external
  navigation data — a live "steer-to" leg from the Pi is possible, dockside verification
  required once the N2K cable is plugged); it does **NOT receive 129041 AIS AtoN**, so the
  virtual-AIS-marks trick is out on N2K for this plotter.
- **Shipped: Lab GPX export** (`vps/lab/app/gpx.py`, `GET /api/playbooks/{pid}/gpx`, a
  "⤓ GPX (chartplotter)" button beside the bundle download): course marks as C4- prefixed
  waypoints, the RECOMMENDED variant as a navigable route (named points, plan ETAs as times),
  every variant as a drawn track (`?variants=all`). Verified against the frozen
  bayview-mackinac-2026 bundle (3 marks + 43-point route + 2 tracks); unit test host + baked
  image. Live-leg N2K broadcast (129284/129285 emitter on the Pi) is DESIGNED, not built —
  post-cable, needs a dockside render check on the 943.

## 2026-07-10 (later) — the iPad button that puts the route on the Garmin

- **The live push, built** (the "designed, not built" from the same morning): **`pi/n2kout/`**,
  the one Tier-1 service that WRITES to the bus, and only on the crew's button. Pure-stdlib
  NMEA-2000 encoder (`n2k.py`: SocketCAN AF_CAN, fast-packet TX framing, canboat-exact field
  packing for 129283 XTE · 129284 Navigation Data · 129285 Route/WP list · a minimal 60928
  address claim; STRING_LAU names; n/a conventions; long routes CHUNKED via startRps — and
  nItems = rows-in-this-message, the canboat reference reading, or decoders chase a phantom
  row into the frame padding). FastAPI service on :8210 (host net, `CAN_IFACE`): POST /route
  starts a broadcast thread (route chunks ~5 s, XTE+nav ~1 Hz), POST /stop goes silent,
  passive by default — harmless with no bus (bench vcan0 / pre-cable boat alike).
- **Engine `POST /gps/show` / `/gps/clear` / `GET /gps/status`** (`app/gpsout.py`): assembles
  the frozen bundle variant's path (default recommended) or the onboard re-route, downsamples
  to ≤24 points (endpoints kept), names them C4-01…, hands to n2kout. **iPad**: the playbook
  detail's strategy stack gains the **GARMIN 943 row** — ▶ Gameplan route · ⟳ Re-route ·
  ✕ Clear + live broadcaster status.
- **Verified on the bench end-to-end**: Playwright tap → engine → n2kout → vcan0 frames →
  **canboatjs round-trip decode exact** (route name, named waypoints, correct lat/lon 1e-7,
  ETA date/time, chunk placement 0/10/20). Unit suites: `pi/n2kout/test_n2k.py` (ids, packing,
  framing, chunk cap) + `vps/agent/test_gpsout.py` (variant selection, downsample, honest
  failure notes). **Dockside verification still owed**: how the 943 *renders* an external
  route (line on chart vs data fields) — first time the N2K cable goes in.

## Standing decisions (still binding)

- **RRS 41 bright line**: all frontier/cloud work pre-start, frozen at the gun; in-race =
  onboard only (own data + common public data); the cloud is race-gated, fail-closed.
- **The onboard LLM never originates strategy** (2026-07-06): it narrates engine facts and
  condition-matches against the frozen play library; off-book verdicts are the deterministic
  engine's call.
- **Human-in-the-loop learning**: proposals never mutate the boat model; a person approves
  every polar/helm/wave-coefficient change.
- **Homework pattern**: anything the boat needs in-race (playbook, obstacles, fleet roster,
  forecast fingerprint, venue stats) is compiled ashore and frozen aboard before the start.
- **Collect everything**: every sensor, per source, raw SI; readers pick + cross-check.
