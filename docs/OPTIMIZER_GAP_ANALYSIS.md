# Optimizer gap analysis — Agent_C4 (C4 Performance Lab) vs Bitsailor

Benchmark of our routing/optimizer against **Bitsailor** (github.com/mak08/Bitsailor), a mature
open-source isochrone weather router (Common Lisp). Done 2026-06-20 to steer Lab-2. Sources: the
Bitsailor README + `simulation.cl` routing core.

## TL;DR
Bitsailor is a **better single-route router** (years of polish): real land/zone avoidance,
sail-specific polars, and a smarter isochrone. **We are a broader system** it doesn't attempt:
multi-model **ensemble scenarios → a branching crew PLAYBOOK**, a three-tier onboard architecture
(deterministic engine + onboard LLM + iPad glass-box), and a roadmap of handicap-aware (ORC),
current, buoy-obs and boat-specific-polar layers. **Action: borrow Bitsailor's routing-fidelity
ideas (land avoidance, sail polars, isochrone) into our engine; keep our scenario/playbook/onboard
moat; the four data levers then push us past Bitsailor on physics.**

## Dimension-by-dimension

| Dimension | Bitsailor | Agent_C4 (Lab-1/2a + onboard) | Verdict |
|---|---|---|---|
| **Isochrone algorithm** | VMG-gated angle range + destination "fan"/cone + growing `max-points` with angular-sector bucketing; `filter-isochrone` w/ limits+zones | 0–360° heading fan @12°, prune to farthest point per 3° bearing-sector, backtrack | **Bitsailor ahead** — adopt VMG-gating + dest-cone (faster, esp. since we route many scenarios) |
| **Time stepping** | Adaptive: 120 s for first ~2 h, then 600/900/1800 s — fine where decisions matter | Single `dt` from leg distance (0.15–1.0 h) | **Bitsailor ahead** — adopt fine-early stepping |
| **Polars / sail** | Sail-specific polars; precomputes **optimal sail + speed per (TWS,TWA)** at 0.1°/0.1 m/s; returns the chosen sail along the route | One blended ORC polar (126 pts), nearest-neighbor; optimizer is sail-agnostic (sail crossovers live only in the dashboard speed-guide) | **Bitsailor ahead** — feed sail-specific polars so the route carries a **per-leg sail plan** (the playbook needs this; ties to the *refined-polars* lever) |
| **Land / shoal / exclusion-zone avoidance** | **Yes** — GDAL/GEOS + OSM land polygons (1° tile cache) + zone filtering | **None** — our isochrone can route *through* islands/shoals; we only skip un-geocoded marks | **Bitsailor far ahead — our biggest correctness gap** (Mackinac is full of islands/shoals). Add coastline + RaceDefinition exclusion zones to the prune |
| **Maneuver penalties (tack/gybe/sail change)** | **None** (detects tack/sail state, applies no cost) | **None** (counts tacks, doesn't penalize) | **Parity** — both omit; a small penalty would add realism (optional) |
| **Wind models / scenarios / ensemble** | **Single** forecast model per run (GFS or DWD/ICON) | **Multi-model blend + per-model scenario fan-out + ensemble opt-in + spread→confidence + branching playbook** | **We are well ahead — our moat** |
| **Currents / tide** | Framework can fetch via `get-params` (cl-weather) but not a focus; no Great-Lakes currents | None yet → **Lab-2d GLOFS** planned | Both weak; our GLOFS work would **exceed** Bitsailor for the Lakes |
| **Waves** | `get-params` can fetch waves | None | Minor; optional later (wave→polar degradation) |
| **Live obs / buoys** | No | Planned **Lab-2d NDBC/GLOS** (forecast bias-correction + leading indicator) | Ours would **exceed** |
| **Objective** | Elapsed time | Elapsed now → planned **ORC corrected-time vs class** (Lab-2d) | Ours would **exceed** (handicap-aware) |
| **Live in-race routing** | Routes from live position (`nmea.cl`) | Onboard Pi engine routes live from current position (RRS-41-structured) | **Parity** |
| **Output / product** | One optimal route + web UI (router) | Route + Opus briefing + **playbook variants/decision-tree** + **onboard engine/LLM/iPad glass-box**, frozen-at-the-gun | **Different products** — ours is crew decision-support, not just a router |
| **Maturity / robustness** | High (antimeridian, poles, tile-cached land, adaptive steps) | Newer/simpler but purpose-built + multi-scenario | Bitsailor more battle-tested |

## What to borrow from Bitsailor (routing fidelity)
1. **Land / shoal / exclusion-zone avoidance** — highest-value correctness fix. Add a coastline
   polygon test (Great Lakes shoreline + the RaceDefinition's exclusion zones) to the isochrone
   prune so routes can't cut across islands/shoals. *(New: an "obstacles" track.)*
2. **Sail-specific polars in the optimizer** — precompute optimal-sail+speed per (TWS,TWA) from the
   C4 speed-guide so every route/variant carries a **per-leg sail plan**. *(Feeds Lab-2b's variant
   sail plan; ties to the refined-polars lever.)*
3. **Isochrone upgrades** — VMG-angle gating + destination cone + bilinear-interp polar lookup +
   adaptive fine-early time-stepping → faster and more accurate (compounds across Lab-2a's many
   scenario routes).

## Where we extend beyond Bitsailor (keep building)
- Multi-model **ensemble scenarios → branching playbook** (Lab-2a ✅ → 2b/2c).
- **Three-tier onboard** (engine + Orin LLM glass-box copilot + iPad), frozen homework, RRS-41 line.
- The four prioritized data levers — **current (GLOFS), buoys (NDBC/GLOS), fleet/ORC corrected-time,
  refined boat-specific polars** — none of which Bitsailor has.

## Recommended sequencing (folds the gaps + the four levers into Lab-2)
1. **Lab-2b — Opus synthesis → playbook bundle** (in progress next): variants → rationale/tradeoffs/
   "what-flips-it" + decision tree → signed artifact.
2. **Routing fidelity (Bitsailor parity), highest-ROI first:** (a) **land/exclusion-zone avoidance**,
   (b) **sail-specific polars + per-leg sail plan**, (c) isochrone upgrades (VMG-gate/cone/adaptive).
3. **Lab-2c** — branch children + freeze/deploy to onboard `PLAYBOOK_PATH`.
4. **Lab-2d data levers (all four, prioritized by the user):** current/tide (GLOFS) → buoy obs
   (NDBC/GLOS) → ORC corrected-time objective → refined polars write-back. (a) gives the biggest
   physics gain on the Lakes; (c) changes the optimizer's objective; (d) makes the boat model true.
