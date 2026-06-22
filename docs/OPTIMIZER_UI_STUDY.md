# Optimizer UI Study — Orca + Expedition → C4 Performance Lab Gameplan

**Status:** study + prioritized recommendations (2026-06-22). **Implementation: Tier 0 + Tier 1
(minus laylines) SHIPPED 2026-06-22** — see the per-item ✅ markers + the implementation-phasing
section. Remaining: Tier 1.4 laylines, Tier 2, Tier 3. Surface under study = the Lab **Gameplan / Optimizer**
tab (`vps/lab/web/mapview.js` + `app.js` + `styles.css`). RRS 41: this is PREP (frozen at the gun).

Two reference apps were studied for UI/UX *patterns* (not imagery — both are proprietary): **Orca**
(orca.app — touch-first modern cruising nav) and **Expedition** (expeditionmarine.com — the
grand-prix offshore-routing gold standard, Windows). Sources are listed at the end.

---

## Phase 0 — Our Gameplan/Optimizer as-is (baseline inventory)

**Map (`mapview.js`) — a Leaflet slippy map, layer stack:**
- OSM basemap + optional OpenSeaMap seamark tiles.
- **ENC obstacle overlay** drawn on a `CanvasOverlay` (thousands of polygons stay fast): draft-aware
  shoals (red, α0.20), rocks/obstructions (dark red, α0.55), real land (tan, α0.35, default OFF),
  race zones (purple, always on).
- **GRIB wind overlay** — arrows rotated by TWD, colored by TWS (5-stop ramp blue→green→amber→
  orange→red at 6/12/18/24 kn), **faded by confidence** (α = 0.30 + 0.70·conf), density-thinned to a
  26 px minimum spacing per zoom.
- **Route** — solid black optimized track (weight 3) over a dashed grey rhumb line between the real
  marks; mark dots (start green, finish gold, marks blue).
- **In-map controls:** a top-right **Layers** box (wind/shoals/rocks/ENC-land/seamarks checkboxes)
  and a bottom-left **forecast time slider** (hourly scrub; a boat marker advances along the route to
  show time-synced wind = what the boat will actually meet).

**Controls (`app.js renderGameplan`):** Race · Course · Start (datetime-local) · **Boat** selector +
editable **Draft (ft)** + **Charts** source (Natural Earth | NOAA ENC) · per-model checkboxes
(GFS/NAM/HRRR/GEFS/ECMWF) · **Ensemble members** (number, hardcoded `max=30`) · Avoid-land checkbox ·
**Run optimizer**. Plus a **"Review boat model"** toggle → crossover bands (sail by TWA per TWS) +
editable jib J1/J2/J3 TWS thresholds + the polar grid. Plus a **Branching playbook** card →
Synthesize → variant cards (summary/why/tradeoffs/what-flips-it) + decision tree + **Freeze & sign**.

**Results (`renderOptResult`):** a stats row (hours · nm sailed · nm direct · tacks · confidence w/
min · wind coverage), a **degraded-forecast banner** (warnings list), the map, an obstacle-provenance
note, a **leg table** (To · Min · Point-of-sail · Sail · Tacks · TWS · TWD · Conf), a sail-plan peel
strip, the Opus **briefing** (`<pre>`), and **wind-field provenance** (per-model cycle + frames/
expected + cycle-fallback badge).

**Theme:** dark office/daylight tool — bg `#0f1419`, panels `#19212b`, accent cyan `#36b3ff`,
confidence green `#7ee0a8` / amber `#f5c451` / red `#ff6b6b`. (The night-readable surface is the
separate onboard `pi/console`, not this.) Responsive iPad + desktop, vanilla JS, no build.

**As-is strengths:** real slippy map + ENC draft-aware obstacles; confidence-faded wind (a genuine
differentiator); the time slider with a time-synced boat marker; the branching playbook + signed
bundle; the reviewable/editable boat sail model. **As-is gaps (preview):** no isochrones drawn; only
the single optimal route (the *exploration* is hidden); a fixed single wind layer (no contour/barb
options, no animation play button); confusing ensemble control; no wind color-scale legend on the
map; no laylines; no start-line tools; results are a flat table (no row↔map↔time linking).

---

## Phase 1 — The two references (condensed; full notes + sources at end)

### Orca (touch-first cruising nav)
- **Chart:** own vector portfolio, praised for clean zoom-out legibility + a 3D view; light sectors
  rendered for night.
- **Layers:** one top-right **Control Center** button consolidates all layer/route/display/safety
  toggles into a single labeled panel (not scattered chrome).
- **Wind overlay (richest area):** **dual rendering — a heatmap (color = strength) + animated
  particles (speed = intensity, motion = direction)**, with an **expandable color-scale legend in the
  bottom-left**; layer tabs Rain/Wind/Currents/Tides/Waves.
- **Time:** a bottom **timeline scrubber** (20-min resolution, "now" marked by a translucent blue
  line); **scrubbing the timeline moves the map to your *projected position* at that time** — the
  single most distinctive interaction.
- **Route:** Sail/Engine/Manual autoroute; the line bends with tide/wind, dashed where engine is
  needed, warning symbols along it; auto-reroute prompts on shifts. **No isochrones, no laylines.**
- **Results:** a graphical **route timeline** (not a numeric table) with a viewport rectangle tying
  graph↔chart; users' #1 complaint = **missing numeric per-waypoint detail**.
- **Polars:** ~8000-profile library, picked once in Vessel settings; **no per-leg sail table / polar
  diagram surfaced.** **Confidence/ensemble: none.** **Start-line tools: none.**
- **Theme:** three-state normal/dark/**night-red**, auto at ≤10% brightness, symbology *re-authored*
  per mode (not CSS-inverted). Editing = direct long-press-drag.

### Expedition (offshore-routing gold standard)
- **Isochrones (its signature):** draws **forward isochrones** (equal-time-from-start) AND **reverse
  isochrones** (equal-time-from-finish); reading their **parallelism = a route-sensitivity /
  confidence display** (parallel = decision doesn't matter; a pinched "nose" = a critical decision
  point). Plus **Paths** (all explored candidate routes, not just the winner), **Previous optimal
  paths** (overlay prior runs to A/B GRIBs/polars), and expected wind-barbs-along-route. Each is an
  independent draw toggle.
- **Wind/GRIB:** per-field choice of **barbs vs arrows vs contours**, user-set contour
  **min/max/increment**, **Fade** + **Spectrum** color ramps; documented best practice = wind-speed
  contours @ 2–3 kn + isobars while scrubbing to find hot/cold spots.
- **Time:** discrete **step model** — Animation interval (min, default 60), Forward/Reverse step
  buttons (Up/Down arrows) that step the boat along the path; current time top-left; "Set display
  time" jump.
- **Confidence:** reverse-isochrone parallelism (above) + modern **ensemble routing / "optimal route
  sensitivity" / multiple optimum routes** + a **"what-if"** that pins wind/current to fixed values.
- **Sail/polars:** polars + a **sail chart** (sail per TWS/TWA cell); router reports **which sail is up
  each step**; an Edit-polars window with Start/Heel/Database/**Sails** tabs (per-sail color, type)
  and a sail-test analysis tool that builds polar "patches" per sail group.
- **Routing settings (dense, single page):** Start time + **Now**; **Resolution** with **Auto** +
  plain-language sizing ("10 nm grid for a 100 nm race"); extend-last-wind-field-in-time; use tidal
  streams; optimise-first-leg-only; avoid-land; draw toggles. Documented common-error checklist (start
  time / time zone / GRIB span / resolution).
- **Results:** **View optimum course table** (per-leg rows, **bracketed TWA = a tack/gybe in that
  segment**, **DNM%** progress metric), CSV/Excel export to email crew; a weather tooltip colored to
  match the optimised solution.
- **Start-line tools (well developed):** ping both line ends; boat-length grid/circles; time-to-line/
  time-to-burn/line-bias/rate-of-turn/acceleration; a start polar; auto pre/post-start display swap.
- **Theme:** Day/Dusk/Night modes; utilitarian Windows-native (Verdana); right-edge **number bars**
  (raw + damped, damped underlined); task-grouped toolbars. Pro/dense; discoverability is low for
  novices (everything in tabs + a big help file).

---

## Phase 2 — Comparison matrix

Rubric scored ✓✓ strong / ✓ present / ~ partial / ✗ absent — *for our use case* (a sailing-race
weather-routing optimizer).

| # | Dimension | Orca | Expedition | **C4 Lab (ours)** |
|---|-----------|------|------------|-------------------|
| 1 | Base map + chart symbology | ✓✓ own clean vector + 3D | ✓ raster+vector, NOAA/C-MAP palettes | ✓ OSM + our ENC polygons over it |
| 2 | Layer system & toggles | ✓✓ one Control Center panel | ✓ powerful but scattered across tabs | ✓ top-right box, 5 toggles |
| 3 | Route / **isochrone** viz | ~ route only, no isochrones | ✓✓ fwd+reverse isochrones, all paths, prev runs | ~ single optimal route + rhumb; **no isochrones, no laylines** |
| 4 | Wind/GRIB overlay | ✓✓ heatmap + animated particles + legend | ✓✓ barbs/arrows/contours, ramp controls | ✓ arrows, TWS color, **confidence-faded**; no legend, no contours, no play |
| 5 | Time control | ✓✓ scrubber + map-follows-position | ✓ step buttons | ✓ slider + time-synced boat marker; **no play/animate** |
| 6 | **Uncertainty / confidence** | ✗ none | ✓ reverse-iso + ensemble sensitivity | ✓✓ multi-model spread → confidence shading **(our moat)** |
| 7 | Sail plan / polars | ~ library, hidden | ✓✓ sail chart + per-step sail + editor | ✓✓ per-leg sail + crossovers + jib J1/J2/J3 + polar grid, reviewable **(our moat)** |
| 8 | Routing controls/settings | ✓ minimal (a virtue + a vice) | ✓✓ deep, Auto-resolution + guidance | ✓ moderate; **ensemble control confusing**; no resolution knob |
| 9 | Results / leg table | ~ graphical, weak numbers | ✓✓ course table + DNM% + CSV export | ✓ leg table + stats; **no row↔map↔time link, no export** |
| 10 | Start-line / pre-start | ✗ | ✓✓ full start suite | ✗ (out of scope — onboard/race, not PREP) |
| 11 | Info density & hierarchy | ✓✓ clutter-removed | ~ dense/pro, buried | ✓ clean cards; could group controls |
| 12 | Color / type / day-night | ✓✓ re-authored night-red | ✓ Day/Dusk/Night | ✓ clean dark daylight tool (night = onboard console) |
| 13 | Touch vs desktop ergonomics | ✓✓ touch-first direct-manip | ~ desktop keyboard/mouse | ✓ responsive iPad+desktop |
| 14 | Discoverability / affordances | ✓ (deep menus aside) | ~ low for novices | ✓ labeled + hints; could add legends/tooltips |

**Read:** we already win the two dimensions that matter most for *our* product and that **both**
references are weak/absent on — **uncertainty/confidence (6)** and **reviewable sail model (7)** —
plus we have the branching playbook neither has. We trail clearly on **isochrone/route-exploration
viz (3)**, **wind-overlay richness (4)**, **results linking/export (9)**, and the **routing-settings
polish (8)**. Start-line tools (10) are deliberately out of scope here (that's an onboard/race
concern, not pre-race PREP).

---

## Phase 3 — Gap analysis → prioritized recommendations

Each item maps to a concrete change in our files and **preserves our moats**: multi-model spread →
confidence, the branching playbook, the boat sail model, ENC draft-aware charts, RRS-41 prep framing.
We are NOT trying to clone a single-route pro router — we borrow patterns that amplify what we already
do differently.

### Tier 0 — near-term, already-specced (do first, independent of the restyle) — ✅ SHIPPED (PR-1)
- **0.1 Fix the "Ensemble members" control** ✅ (`app.js` `optModelChecks`/`renderGameplan`/`optToggle`).
  Disable + hint the field when no ensemble model is checked; set `max` to the **real** member count
  of the selected ensemble source(s) from `/api/models` (GEFS 31; ECMWF-ENS 50 once wired) instead of
  the hardcoded 30; add a muted cost/diminishing-returns caption. *(Already detailed in the
  optimizer-UI-study plan memory; small contained change.)*
- **0.2 Wire ECMWF-ENS** ✅ — shipped as a SEPARATE `ecmwf-ens` ensemble source (control + 50
  perturbed members, enfo/cf + enfo/pf), NOT by mutating `ECMWF.members` as originally specced:
  `_members_for` returns `["det"]` for a `kind="deterministic"` source regardless of its `members`,
  so HRES would have broken. The new source keeps HRES loading when ensembles are off; inherits the
  429 cap/cooldown. `/api/models` now lists it (ensemble, 51 members).

### Tier 1 — quick wins (color / legend / layout / overlay polish; ~hours each, no new backend)
- **1.1 Wind color-scale legend on the map** ✅ (`mapview.js`, a bottom-right `L.Control`). TWS ramp
  swatches (our 6/12/18/24-kn stops) + "arrow opacity = model confidence (faint = models split)" —
  makes the confidence-fade moat legible.
- **1.2 Forecast animation play/pause** ✅ (`mapview.js`). A ▶/⏸ button by the slider auto-advances
  the frames (700 ms); a manual scrub stops it; `render()` clears a stale timer.
- **1.3 Group the controls into labeled sections** ✅ (`app.js renderGameplan` + `styles.css`). Three
  cards — **Course** · **Boat & charts** · **Weather models** — + a clean run row. All IDs/handlers
  preserved.
- **1.4 Laylines + rhumb-bearing labels on the route** — **DEFERRED** to the route-viz work (Tier 2):
  drawing correct laylines needs the beat/run VMG angle vs the wind at the next mark, which is the
  same geometry as the isochrone/paths overlay — better built together than as a standalone quick win.
- **1.5 Bracketed-TWA semantics in the leg table** ✅ (`app.js optLegRow`). Legs with maneuvers show a
  ⇄ N badge (Expedition's convention).

### Tier 2 — medium (the differentiating viz; ~days each)
- **2.1 Draw isochrones (forward) as an optional layer** — the biggest single gap. The router already
  computes the isochrone frontier per step in `route_leg`; **emit the per-step frontier polylines**
  (down-sampled) in the optimize result and draw them as a faint `CanvasOverlay` layer with a toggle.
  This turns our map from "here's the line" into "here's *why* the line" — and it's the natural
  substrate for 2.2.
- **2.2 Reverse-isochrone / route-sensitivity overlay → fold into our confidence story.** Expedition's
  killer idea is that isochrone *parallelism* shows where a decision is critical. We have a richer
  signal already (multi-model spread + the branching playbook's `what_flips_it` triggers). **Marry
  them:** shade the map where model routes diverge (we already split per-model fields in `playbook.py`)
  and **plot the per-model candidate routes as faint "Paths"** (Expedition's all-paths overlay) under
  the chosen route — so the user literally sees the fan the confidence number summarizes. This is our
  moat made visual and beats both references on dimension 6.
- **2.3 Routing-table ↔ map ↔ time linking** (`app.js` + `mapview.js`). Borrow Orca's viewport-rect +
  Expedition's tooltip: hovering/selecting a leg row highlights that segment on the map and snaps the
  time slider to the leg's ETA (we already carry per-point `t`). Add **CSV export** of the leg table
  (Expedition's email-the-crew pattern) — trivial and high-value for a navigator.
- **2.4 GRIB display options** (`mapview.js` + a small control). Offer **arrows | barbs | contours**
  and let TWS contour increment be chosen (Expedition). At minimum add **barbs** (the standard
  offshore convention) as an alternative to arrows. Keeps confidence-fade on top.
- **2.5 Resolution / effort control + plain-language guidance** (`app.js` + optimizer `time_budget`/
  grid). Expose an **Auto / Fast / Fine** routing-resolution selector with a one-line explainer
  ("Fine = slower, sharper near shore") instead of the implicit fixed step, and surface the
  common-error checklist inline when an optimize returns degraded/sparse.

### Tier 3 — larger (full Orca-style restyle of the surface)
- **3.1 Consolidated map "Control Center"** — collapse the Layers box + time slider + (new) wind-mode
  + isochrone toggles into one polished bottom/side panel à la Orca, touch-sized.
- **3.2 Map-led layout** — make the slippy map the hero (full-width, controls floating), with the
  stats/legs/briefing/playbook as a collapsible side rail, rather than today's stacked cards. This is
  the "clean modern Orca look" the user likes, adapted to our prep workflow.
- **3.3 Map-follows-projected-position on scrub** — extend our time-synced boat marker (we already
  have it) into Orca's full "scrub the timeline, the map pans to where you'll be" interaction.

**Explicitly NOT borrowed:** Orca's minimal/hidden polars (we want them reviewable — our moat) and
its missing numeric detail; Expedition's start-line suite (race/onboard scope, not PREP); Expedition's
buried-in-tabs density (we keep the clean card layout). We are not cloning a single-route pro router —
every recommendation above either makes our existing moats *legible* or adds chartcraft both
references prove sailors expect.

---

## Phase 4 — Mockups + proposed implementation phase

**Mockup A — map with legend + animation + isochrone toggle (Tier 1.1/1.2 + 2.1):**
```
┌─────────────────────────────────────────── optMap ──────────────────────────┐
│                                              ┌─ Layers ───────────────┐      │
│        ·  ·  ·  ·  (faint forward            │ ☑ Wind  ☑ Shoals       │      │
│      ·  ╱╱ isochrone frontiers)              │ ☑ Rocks ☐ ENC land     │      │
│    ·  ╱╱  ╲╲                                 │ ☐ Seamarks ☐ Isochrones│      │
│   ╱╱ ●━━━━━━●  optimized route               │ Wind: [arrows▾]        │      │
│  ╱   start    ╲   (per-model paths faint)    └────────────────────────┘      │
│              ╲╲●  finish                                                      │
│ ┌─ Forecast ▶⏸ ──────────────┐   ┌─ TWS kn ──────────────┐                   │
│ │ Sat 28  18:00 UTC (T+12h)  │   │ ▮<6 ▮<12 ▮<18 ▮<24 ▮24+│                   │
│ │ ●──────────┼───────────────│   │ arrow opacity = model  │                   │
│ │ drag to scrub (hourly)     │   │ confidence (faint=split)│                  │
│ └────────────────────────────┘   └────────────────────────┘                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Mockup B — grouped controls (Tier 1.3):**
```
┌ Course ────────────┐ ┌ Boat & charts ───────────┐ ┌ Weather models ──────────┐
│ Race  [Mackinac ▾] │ │ Boat [SR33 (7 ft) ▾]      │ │ ☑GFS ☑NAM ☑HRRR ☐GEFS    │
│ Course[Cove Is. ▾] │ │ Draft [7.0] ft            │ │ ☐ECMWF                   │
│ Start [____UTC___] │ │ Charts[NOAA ENC ▾]        │ │ Ensemble [ 0 ] (disabled │
│                    │ │ [Review boat model]       │ │   — pick GEFS/ECMWF-ENS) │
└────────────────────┘ └───────────────────────────┘ └──────────────────────────┘
        Resolution [Auto▾]   ☑ Avoid land/islands/zones        [ Run optimizer → ]
```

**Proposed implementation phasing:**
1. **PR-1 (Tier 0):** ensemble-control fix + ECMWF-ENS wiring. ✅ SHIPPED (`8bc95f7`).
2. **PR-2 (Tier 1 quick wins):** wind legend, animation play/pause, grouped controls,
   bracketed-TWA badges (laylines deferred to PR-3). ✅ SHIPPED (`06fb1a4`).
3. **PR-3 (Tier 2a):** isochrone frontier emission + optional layer (the marquee viz upgrade) +
   laylines (1.4, same geometry). ← NEXT
4. **PR-4 (Tier 2b):** per-model candidate-paths overlay + confidence shading (our moat, visualized)
   + leg-row↔map↔time linking + CSV export.
5. **PR-5 (Tier 3):** the consolidated Control Center + map-led layout restyle (the big Orca-style
   look), gated on the user's own Orca UX notes.

Fold in the user's own Orca UX/UI notes when they arrive (they slot naturally into PR-2 and PR-5).

---

## Sources

**Orca:** help.getorca.com (weather + create-route articles); getorca.com/blog (orca_sail_routing,
weather-for-routes, new-weather-models-and-routing, dark-mode, control_center_chart_coverage,
instrument-panels); yachtingmonthly.com Orca review; yacht.de Orca weather article; forums.ybw.com
Orca-nav-app thread. *(morganscloud reviews paywalled/403; App Store rate-limited — feature claims via
search snippets.)*

**Expedition:** Expedition Help v5 PDF (blur.se mirror — primary source for concrete UI detail);
expeditionmarine.com feature list (ensemble routing, route sensitivity, multiple optimum routes, wave
avoidance, asymmetric polars); expedition.boardhost.com routing thread; expeditionmarine.com/downloads
manual; academy.islersailing.com intro. *(Unconfirmed/flagged: exact chart projection; full optimum-
course-table column list; modern ensemble-routing UI specifics; dedicated polygon exclusion-zone tool;
a true graphical time-scrubber — Expedition uses discrete stepping.)*

**Caveat:** both apps proprietary — patterns studied, our own assets produced; no UI/imagery copied.
```
