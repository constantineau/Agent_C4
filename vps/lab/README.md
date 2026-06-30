# C4 Performance Lab (cloud) — Lab-0 race ingestion + Lab-1 GRIB optimizer

The C4 Performance Lab is the between-races, frontier-model side of the project (strategy studio +
learning loop). **Lab-0** is its foundation: turn a race's published documents into a structured,
reusable **RaceDefinition** (from NOR + SI + **SER**) that feeds three consumers —

- the **optimizer / navigator**: course geometry (marks/gates/finish in WGS84), zones the route
  must respect, the scoring objective, and the fleet;
- the **race checklists** (`requirements`): the **comprehensive** set of things the boat must do or
  carry — safety/SER equipment, registration, navigation lights, the gate/finish procedures — each
  tagged with the phase + trigger it applies at. Pre-race items are the **prep checklist** the team
  works through; race-time items (`deliver_to_ipad=true`) are compiled into the playbook and
  **surfaced on the onboard console at the right moment** (nav lights at sunset; the GPS photo +
  displaying registration/sail numbers at the finish);
- the **rules / scoring layer** (`rules_profile`): rule modifications + scoring. The **RRS-41**
  carve-out (NOR §2.1(d)) is just *one* modification the race gate reads — comprehensive
  requirement-checking is the point, not RRS-41.

One ingestion, three consumers. Full design: `docs/ONBOARD_ENGINE_SCOPING.md` §4.7-6 and
`docs/RRS41_COMPLIANCE.md`.

## Dual input (required) + human review

Users ingest a race either way:
- **(a) auto-find** — give a race URL; the Lab crawls/fetches the NOR/SI/course/entry docs. Works
  for static-doc sites (e.g. `bycmack.com`).
- **(b) paste a link / upload a PDF** — for JS-rendered race hubs where a crawler can't reach the
  files (e.g. Mills Trophy on YachtScoring), the user supplies the direct PDF link or uploads it.

Either way, Opus extracts the RaceDefinition and a **mandatory human-review step** signs off the
geometry/rules before the definition is activated — a wrong waypoint is dangerous, and NOR/SI
coordinate formats vary. (Validator warnings flag the review items; see below.)

## Layout

- `shared/race_def.py` — the **schema** (dataclasses) + a dependency-free validator + a CLI.
  Coordinates are decimal degrees, WGS84 (matches the engine). `dm_to_dd()` documents the
  degrees-decimal-minutes → decimal-degrees conversion NORs use.
- `vps/lab/races/<race_id>.json` — extracted RaceDefinition instances.

Validate an instance:

```bash
python3 -m shared.race_def vps/lab/races/bayview_mackinac_2026.json
# errors block activation; warnings are the human-review items (e.g. null island coords)
```

## Status

- **Schema + first instance: done (schema-first slice).** `bayview_mackinac_2026.json` was
  extracted from the real 2026 NOR and validates. It carries authoritative NOR geometry — the
  **Cove Island virtual gate** (SW N45°20.00′ W081°51.00′, NE N45°20.28′ W081°49.63′) and the
  **virtual GPS finish** at Round Island / Mackinac — plus both courses (Cove Island, Shore), the
  four divisions, the ORC Single-Number ToT scoring (BYC Mack Cove/Shore wind mix or WRS, fixed at
  the race-morning briefing), the `rules_profile` (incl. §2.1(d), the §2.1(f) transponder finish,
  and Appendix WP), and a **22-item `requirements` checklist** spanning safety/SER, registration,
  navigation, procedure, reporting and environmental — **6 flagged `deliver_to_ipad`** (nav lights
  at sunset, sponsor flag, the Cove gate GPS photo, and the finish procedure / GPS photo /
  displaying numbers).
- **Pending (flagged in the instance):** island coordinates (Duck Islands / Bois Blanc / Thunder
  Bay Island) need geocoding + human review; course distances unconfirmed; the `requirements`
  checklist is a **representative subset** — the full SER (~40 equipment items) + SI procedural
  items must be ingested completely before the prep checklist is relied on (and `display-numbers`
  needs SI verification); the **2026 SI is not yet posted (~July 2026)** — it fixes the exact start
  line, zones/marks beyond the NOR, and procedural requirements, so re-ingest when it lands
  (`https://bycmack.com/sis/`).
- **Lab shell + race library + editable review + sign-off: live (dev :8103).** `vps/lab/` is a
  FastAPI service that serves the browser-based Lab (shared team login, tabbed sections) + the
  race-library API (`/api/races`, `/api/races/{id}`, `/api/races/{id}/validate`,
  `/api/races/{id}/approve`). The **Races** tab lists the library and renders a RaceDefinition as an
  **inline-editable review form** — every field is editable in place (header, course marks
  [name/type/rounding/lat/lon, add/remove], the requirements checklist [text/category/phase/
  trigger/critical/→iPad/source, add/remove], rules & scoring modifications [add/remove],
  tracker-permitted, provenance notes), bound straight to the in-memory definition so typing never
  loses focus (only add/remove rows + Save/Approve repaint). **Save edits** (`POST /api/races`)
  persists to the `lab_ingested` volume + re-validates; **Approve & sign off**
  (`POST /api/races/{id}/approve`) stamps a `reviewed`/`reviewed_at` flag — **refused while blocking
  validation errors remain** (warnings don't block) — and `{"approved":false}` un-approves. The
  library list shows a **✓ approved** / *awaiting approval* pill per race. Geometry with maps +
  geocoding stays the **Course & Marks** tab's job; the Races form edits the data everywhere else.
  Other sections are descriptive placeholders. Run:
  `docker compose -f compose.dev.yml up -d --build lab` → `http://localhost:8103` (dev pw `lab-dev`).
  *(Verified: backend approve/un-approve/refuse-on-errors + save round-trip; Playwright UI — 124
  editable inputs / 77 selects / 45 checkboxes / 22 editable requirement rows render, edit→Save→
  Approve drives the ✓ Approved pill + the library-list pill. Internal `_labstate.json` no longer
  leaks into the race list.)*
- **Dual-input ingestion: live.** The Races tab ingests a race three ways → Opus extraction →
  a **draft** RaceDefinition → the review view → **Save to library**:
  - **auto-find** — `POST /api/ingest/discover {url}` scrapes a race page for candidate PDFs;
  - **paste a direct PDF link** / **upload PDF(s)** — `POST /api/ingest {urls}` / `POST /api/ingest/upload`;
  - extraction (`app/extract.py`) pulls text with pypdf and a frontier model (Opus,
    `ANTHROPIC_MODEL`) emits a RaceDefinition matching the schema — coordinates only when the docs
    state them (else `needs_review`), a comprehensive `requirements` checklist with the race-time
    items flagged `deliver_to_ipad`, and the `rules_profile`. `POST /api/races` saves the
    (reviewed) draft to the `lab_ingested` volume.
  - *Verified on the real 2026 NOR + SER:* 0 errors, **56 requirements** (8 →iPad), the Cove Island
    gate + finish coordinates, 13 RRS modifications, ORC ToT scoring — a draft more complete than
    the hand-built instance. Always a DRAFT pending human review before it's relied on.
- **Course & Marks review: live.** The Course & Marks tab renders each course on a schematic map +
  an editable marks table; fill any `needs_review` mark by hand or **Geocode** (Nominatim,
  human-confirmed), then Save — the reviewed copy lands on the `lab_ingested` volume and **overrides
  the bundled seed**, so the validator's review warnings drop as marks are filled.
- **Course loader (homework→onboard): wired.** `shared.race_def.course_to_marks()` flattens a course
  (gate→midpoint, finish→midpoint; un-geocoded marks skipped + reported) and `POST /course/load`
  (on the cloud agent **and** the onboard engine) writes it to the marks store + activates it, so the
  navigator/plot use the real course. Verified on Mackinac (cloud + onboard). The per-race
  `rules_profile`→gate wiring is deferred until a consumer exists (tracker access / optimizer scoring).
- **Lab-1 multi-model GRIB optimizer: built (dev :8103).** `app/wind/` builds a real `WindField` from
  public weather models — `grib.py` downloads 10 m UGRD/VGRD GRIB2 bbox subsets and parses them with
  cfgrib/eccodes (the eccodes pip wheel bundles the binary, `python -m eccodes selfcheck` runs at
  build); `models.py` defines key-free sources (**GFS / NAM / HRRR** via NOMADS GRIB-filter, **GEFS**
  ensemble + **ECMWF** open-data opt-in), each with its cadence / forecast-hour grid / lag-aware
  freshest-cycle picker; `windfield.py`'s `wind_at(lat,lon,epoch)` blends the models and reports the
  **model/ensemble SPREAD as a confidence** (fuzzy adherence — models disagree → sail conservatively).
  `optimizer.py` routes the course leg-by-leg (isochrone on `polars.py`, the canonical ORC polars) →
  one optimal route + per-leg ETA/tacks/wind + a route confidence + an Opus briefing that flags the
  low-confidence legs. `POST /api/optimize` + `GET /api/models` drive the **Gameplan → Optimizer** tab
  (route canvas + leg table + briefing + model provenance, confidence-coloured); the `lab_gribcache`
  volume caches GRIB so re-runs / ensemble members are cheap. Verified end-to-end on the Mackinac cove
  course (live GFS 18Z + NAM 00Z + HRRR 01Z, ~73 frames; 133 nm / 17.8 h / 1 tack; confidence 0.69).
  - **Parse crash isolation (`GRIB_ISOLATE_PARSE`, default ON):** cfgrib/eccodes can intermittently
    SEGFAULT on a frame (uncatchable, kills the worker), so the parse runs in a persistent child
    process (`_grib_parser.py`, one per build); `grib.IsolatedGribParser` respawns + retries on a
    child crash/hang, then skips the frame (degrades to a skipped frame, not a dead optimize). Also pin
    `pandas==2.2.3` — the unpinned transitive dep pulled 3.0.x which segfaults under numpy 2.1.3 in
    cfgrib's datetime decode. See `test_grib_isolation.py`.
- **Obstacle avoidance (routing fidelity, Bitsailor parity item 2a): built.** `app/geo/` keeps the
  route off land. It's **race-agnostic** — three layers rasterize into one boolean mask the isochrone
  prune queries (`blocked(lat,lon)` / `crosses(a,b)`):
  1. a **global** coastline (`coastline.py`, land∧¬lake + islands-in-lakes, fetched once to the
     `lab_coastline` volume and auto-clipped to whatever course bbox the RaceDefinition yields — so
     any race anywhere gets its own coast, ocean or lake; **GSHHG full-res by default**, Natural Earth
     1:10m as the fallback — see "Higher-res coastline backstop" below);
  2. the race's own `zones[]` (exclusion / hazard / tss polygons);
  3. the race's geocoded `island` marks, buffered to a disk (`radius_nm`) — islands are obstacles to
     route AROUND, not waypoints to hit (so `course_to_marks` omits them as waypoints).
  `optimize_course(..., avoid=True)` builds the field from the course bbox + this race's zones/islands
  (cached by `cache_key` so Lab-2's same-course scenarios share one mask) and threads it through
  `route_leg`, which rejects any heading step that would cross an obstacle. `POST /api/optimize`
  takes `avoid_land` (default true) and returns an `obstacles` summary + `obstacle_steps_avoided`;
  the Gameplan tab draws the coast/island/zone overlay on the route canvas. **A/B-verified on the
  real Cove Island GRIB route:** avoidance OFF passes 1.9 nm from Bois Blanc's center (cutting across
  the island); ON clears at 5.7 nm (no violations) for +0.3 nm / +1 tack. *Caveats:* the global
  backstop now defaults to GSHHG full-res (below) which catches sub-nm islands; the race-island/zone
  layer still backs the race-critical ones (island coords geocoded `approx` → human-review); rounding
  **side** is now enforced for islands that are MARKS of the race (see 2f below) — plain hazards stay
  avoided either side. Tunables: `GEO_RES_DEG`, `GEO_ISLAND_NM`.

### Higher-res coastline backstop — GSHHG full-res

Natural Earth 1:10m is coarse: it omits sub-nm islands (it had **zero** islands across the whole
North Channel / Georgian Bay) and is imprecise at the shoreline. The global coastline (`coastline.py`)
now defaults to **GSHHG** (Global Self-consistent Hierarchical High-resolution Geography), full
resolution. Its hierarchy maps exactly onto our three roles — **L1 = land**, **L2 = lakes**,
**L3 = islands-in-lakes** (and L3 is precisely where the small Great-Lakes islands like Cove Island,
in Ontario, live) — so the existing fill logic (fill land → carve lakes → re-add islands) is
source-agnostic. GSHHG ships as shapefiles, so a prep-time `ogr2ogr -clipsrc <bbox>` (GDAL is already
in the lab image for ENC) clips each level to the course bbox into cached GeoJSON; the hot path stays
pure-python, exactly like `enc.py`. The 149 MB bundle is fetched + the chosen-res L1–L3 extracted once
to `lab_coastline`. Config: `COASTLINE_GLOBAL` (`gshhg` | `natural_earth`, default `gshhg`) and
`GSHHG_RES` (`f`|`h`|`i`|`l`|`c`, default `f`); GSHHG falls back to NE automatically if it can't be
fetched (no download / no ogr2ogr). It is the global backstop under **both** modes — it runs first in
ENC mode too, so US-only ENC's Canadian coverage gap (Cove Island, Manitoulin) is filled by GSHHG
rather than coarse NE. **Mask A/B-verified** on the Bayview Mackinac cove_island bbox: GSHHG blocks
**778 cells across 251 island clusters** NE leaves open + refines 663 cells where NE over-blocked
water; ENC mode still blocks Canadian Manitoulin with US draft-aware shoals intact; a live optimize
ran 42.4 h, coverage 1.0, reaching the finish.

### Map accuracy upgrade — NOAA ENC charts + BoatProfile + a real slippy map

The driving complaint was "the map is not accurate" (Natural Earth 1:10m misses the straits islands
and is imprecise at the shoreline). Three pieces fix it (all on `dev`, live at the test instance):

- **[A] NOAA ENC charts** (`app/geo/enc.py`). The authoritative US S-57 vector charts as a pluggable
  obstacle source, selected by `COASTLINE_SOURCE=enc` (default `natural_earth`; auto-falls-back if
  ENC isn't prepped / no egress). At PREP time `ogr2ogr` (GDAL S-57 driver) turns the covering ENC
  cells into cached GeoJSON on the `lab_enc` volume; the routing hot loop stays pure-python. Role
  layers: **LNDARE** (real land/island polygons), **DEPARE** filtered by the boat **safety depth**
  (= draft + under-keel margin) = real **shoal no-go**, **OBSTRN/UWTROC** = rocks/obstructions.
  `POST /api/enc/prep {race_id,course_id?}` warms the cache with progress. Verified on the real
  Bayview Mackinac course (65 land + 108 shoal + 157 rock polys in the straits — Natural Earth had
  zero straits islands). NOAA GIS export = non-navigational → planning data; verify vs the official
  chart. GDAL is a prep-time dep only.
- **[B] BoatProfile** (`shared/boat_profile.py`, `app/boats.py`). Race × boat are two dimensions; a
  boat's **draft** sets the ENC depth no-go. Canonical SI metres, UI enters/shows **feet**. SR33 =
  profile #1 (`boats/sr33.json`, draft 7 ft = 2.1336 m, margin 0.5 → no-go < 2.63 m). `GET/POST
  /api/boats` (list/get/save, `draft_ft` accepted) + `GET/POST /api/boats/active` (active boat +
  chart source, persisted in `app/labstate.py`). The Gameplan tab gains a boat selector + editable
  draft + a Charts toggle; the optimizer reads the **active boat's** draft. *Also fixes a real
  routing bug ENC surfaced:* a finish/mark set near shore is correctly flagged shoal/land, but you
  can't route AROUND your own destination → `build_for_course` carves a small navigable pocket
  (`GEO_MARK_CARVE_NM=0.5`) at each waypoint. Without it the Mackinac finish was blocked and the
  router thrashed to 1406 nm / 148 tacks; with it the same ENC route is 278 nm / 6 tacks.
- **[C] GRIB-on-ENC slippy map** (`web/mapview.js`, Leaflet vendored in `web/vendor/`). The Gameplan
  route canvas is now a real **Leaflet** map — a layer stack [OSM basemap (+ OpenSeaMap seamarks) +
  ENC obstacle overlay (our shoal/rock/land polygons on a canvas) + GRIB **wind** overlay (arrows by
  TWS/TWD, **faded by confidence**) + route/marks]. A **forecast time slider** scrubs an embedded
  multi-time `wind_grid` (from `WindField.sample_grid`) and moves a boat marker along the route
  (time-synced wind). NOAA's ENC Online dynamic tiles are SCAMIN-gated / near-blank and the RNC
  raster service was sunset, so the authoritative chart layer is **our own extracted ENC polygons**
  (the same data the router uses) over OSM — robust, self-contained, no CDN/build step.

### Lab-2 branching playbook bundle (built)

The optimizer's one route → a small set of strategic **variants** + a crew decision-tree,
synthesized + **signed**, dropped onboard as the copilot's frozen homework. Two stages:

- **Lab-2a fan-out → variants** (`app/playbook.py`). The blended field gives one answer but the
  models disagree, so split the multi-model `WindField` into per-model sub-fields (each a "what if
  the wind follows THIS model" scenario — free, reuses the GRIB already downloaded), route the course
  through each + the blended consensus, and **cluster by which side of the first beat** each favors
  (left/middle/right of the rhumb). Variants carry `supported_by` models, `share` (agreement),
  total-hours + range, a representative route, and the **decision spread** (the time stakes between
  the side options). `POST /api/playbook`.
- **Lab-2b synthesis → signed bundle** (`app/synthesis.py`). **Opus** writes, per variant, a
  crew-facing `summary` / `rationale` / `tradeoffs` and — most important — `what_flips_it`: the
  concrete **observable** trigger (a wind shift past a bearing relative to the first-beat rhumb,
  persistent vs oscillating) that flips the decision to another variant. Plus a `headline`, a
  `recommended` start default, and an ordered `decision_tree`. A **deterministic fallback** builds a
  valid bundle with no API key. The **`c4.playbook/v1`** schema is a *superset* of what the onboard
  copilot's `playbook.Playbook` reads (`race_id` + `variants[].id/summary/what_flips_it`) — so
  freezing one and pointing the copilot's `PLAYBOOK_PATH` at it is the whole onboard wiring.
  **Signing:** `sign_bundle()` hashes the canonical content (sha256 over the bundle minus its
  `signature`, sorted-key/no-space JSON) → tamper-evident "frozen at the gun"; the copilot recomputes
  the identical bytes (`playbook.verify_signature`), so a byte-for-byte bundle verifies. `pbstore.py`
  persists frozen bundles on the `lab_playbooks` volume (`/srv/playbooks`). Endpoints: `POST
  /api/playbook/synthesize` (draft) · `POST /api/playbook/freeze` (sign + persist) · `GET
  /api/playbooks[/{id}]` · `GET /api/playbooks/{id}/download` (the exact signed bytes — scp to the
  Orin). The **Gameplan tab** gains a "Synthesize branching playbook" panel: headline + recommended +
  stakes, per-variant cards (summary/why/tradeoffs/what-flips-it), the decision tree, and **Freeze &
  sign** → signature + download.
- **Verified end-to-end on the real Bayview Mackinac cove_island course** (live GFS+NAM+HRRR + Opus,
  ~2.5 min): a 3-way split (HRRR-left / NAM-middle / GFS-right), agreement 0.33, 252-min decision
  spread; Opus wrote specific rhumb-relative triggers; freeze → signed (sha256) → download → the
  onboard copilot loaded it, **verified the signature**, and emitted the LLM digest with each
  variant's flip trigger. UI Playwright-verified.

### Routing fidelity 2b — per-leg sail plan + reviewable boat sail model (built)

The optimizer routes on the Best-Performance polar envelope (= the max-over-sails speed, so the
route's speed is already sail-optimal) but didn't say WHICH sail. 2b attaches that and makes the boat
sail model reviewable + part of the frozen homework:

- **`vps/db/seed/sr33_crossovers.json`** — the per-TWS sail crossover bands (optimal sail by TWA: J1
  upwind → A2/A3 reaching → S2 running), precomputed from the ORC cert by
  `vps/agent/knowledge/build_speed_guide.py::write_crossovers` (reuses the existing `optimal_sail()`).
  Regenerate with the guide/seed after a cert update.
- **`app/sailplan.py`** loads it: `optimal_sail(tws,twa)` (clamped so an upwind beat's sub-close-hauled
  *direct* TWA still maps to the up sail), `crossovers(tws)`, `model()`.
- **`app/optimizer.py`** adds `sail` to each leg (TWS/TWA already in scope at the leg midpoint) + a
  route-level `sail_plan` (the peel sequence). These flow into the playbook variants for free.
- **`app/synthesis.py`** adds a **`boat_model`** block to the bundle (the crossover table + polar
  source + active-boat draft) so the reviewed boat model is **frozen into the signed homework and
  loaded onto the copilot** (`pi/orin/copilot/playbook.py` surfaces it + the per-variant sail plan in
  its LLM digest; `/health` reports `sail_inventory`).
- **Jib change-downs by TWS (J1/J2/J3).** The ORC cert rates only ONE headsail (the speed-optimal
  J1), so J2/J3 — the same upwind slot, smaller jibs for a building breeze — aren't in the polar. The
  `BoatProfile` carries an editable **`jib_crossovers`** (TWS bands; SR33 = J1<14 / J2 14–20 / J3>20
  kn, crew/sailmaker thresholds, **not** from the cert). `sailplan.optimal_sail(tws,twa,jib_crossovers)`
  specialises the upwind jib by TWS; the active boat's bands thread through `optimize_course` →
  `build_playbook` → `synthesize` → the bundle's `boat_model`. The copilot digest surfaces them
  ("Upwind jib by wind: J1 <14; J2 14–20; J3 20+"). `crossovers_specialized()` relabels the upwind
  band of **each TWS row** to the real jib for that wind (each row is one TWS, so it's exact) — so the
  crossover chart itself shows J1 (light) → J2 (mid) → J3 (heavy), not just the cert's single J1.
- **Review UI:** the Gameplan tab's "Review boat model — polars & sail crossovers" panel shows the
  upwind jib change-downs (editable TWS thresholds → `POST /api/boats/jib-crossovers`), the per-TWA×TWS
  crossover bands (color-coded sails over a 0–180° axis), and the polar grid (TWS × TWA → target
  boatspeed) — exactly what gets loaded onto the copilot, reviewable before lock-in. Endpoints
  `GET /api/crossovers` + `/api/polars`; the optimizer leg table gains a Sail column + a sail-plan strip.

### Routing fidelity 2e — finish/mark over-tack ("scramble") fixes

A real `cove_island` run whose finish leg was a light-air beat came back as a degenerate zig-zag
(dozens of tiny tacks, ~3x oversail, ~2x slow). Structural in the isochrone `route_leg`: the 2c tack
penalty was a per-step haircut (not cumulative), the prune buckets by bearing-from-leg-start so the
two tacks never eliminate each other, and nothing committed the boat to a layline near the mark. Three
env-flagged fixes (all default ON):
- **`ROUTE_LAYLINE_COMMIT`** — within `ROUTE_LAYLINE_COMMIT_NM` (10 nm) of the mark, once a node can lay
  it (bears > the VMG half-angle `_vmg_twa()` off the LOCAL wind axis) drop the opposite-tack headings so
  it fetches the layline; re-checked each generation (a shift re-opens it), final-approach-only so the
  strategic side choice is kept farther out.
- **`ROUTE_TACK_CUMULATIVE`** — the tack cost accrues into a per-path penalty `pen` (+ node ETA); the
  prune ranks by `rng_eff = rng − pen`, so repeated alternation truly loses ground (not a ~5% nudge).
- **`ROUTE_MARK_POS_PRUNE`** — within `ROUTE_MARK_PRUNE_NM` (6 nm) of the mark, bucket the prune by
  POSITION (`ROUTE_MARK_PRUNE_CELL_NM` ≈0.25 nm cell) not bearing-from-start, so near-colocated
  opposite-tack nodes compete and the least-tacked wins.

A/B against ONE frozen GFS+NAM+HRRR field (the reported Jun-29 19:00Z case): baseline finish 27 tacks /
2.7x / 83 h → #2 alone **0 tacks / 1.1x / 40 h**, #3 alone **0 tacks / 1.1x / 40 h** (each kills it
independently; #1 is the clean-layline finisher for genuine-beat finishes). Anti-under-tack guarded:
a steady dead-upwind leg still tacks the minimum and reaches (`test_routing_2e.py`; 2c/2d still green).
Also fixed the per-leg tack **counter**: it classified tack-side off a frozen leg-start wind, so on a
clocking leg it mis-counts in either direction (on the frozen baseline it UNDER-counted, 135 vs 173 real
maneuvers, and would have shown the clean route as a false "0 tacks" vs the real 3). Now each segment is
classified against the wind LOCAL to where/when it's sailed → the true tacks-up/gybes-down tally
(metric-only; route geometry unchanged).

### Routing fidelity 2f — island rounding-side enforcement

Obstacle avoidance (2a) keeps the route off islands but on EITHER side; a race often designates an
island as a mark ("leave Bois Blanc to port / Duck Islands to starboard") and that side was discarded
(`course_to_marks`/`course_roundings` drop all `type:"island"` marks). **We enforce a side only when
the island is a MARK OF THE RACE** — its `rounding` is `port`/`starboard`; a plain hazard
(`rounding:"none"`) stays avoided either side. Mechanism = a **wrong-side barrier** in the mask
(`geo/obstacles.py`): `_island_rounding_marks()` finds the island's leg (transit bearing prev-nav →
next-nav, islands skipped, gate/finish→midpoints); `_fill_wrong_side_barrier()` blocks the illegal side
(perpendicular wall within |along| ≤ radius + `ROUTE_ROUNDSIDE_BAND_NM`) so only the legal hand is open.
Source-independent (ENC or GSHHG/NE), applied after the waypoint carve; provenance →
`obstacles.geometry.rounding_barriers`, drawn as a P/S marker on the Gameplan map. Verified
(`test_routing_2f.py`): scoping (hazard island gets no side), a controlled open-water flip (natural
route takes the wrong side → barrier flips it, both port and starboard, still reaching the mark), and
the real cove_island Duck/BoisBlanc barriers (legal open, illegal blocked). Tunables
`ROUTE_ROUNDSIDE_ISLANDS` (default ON) / `ROUTE_ROUNDSIDE_BAND_NM`.
- **Crew-facing roundings summary:** the route enforces island sides but didn't *tell* the crew, so
  `race_def.marks_with_side()` lists the ordered required sides for all marks (nav + islands + gates),
  the optimize result carries `roundings`, and the briefing states them ("leave Duck Islands to
  starboard, Bois Blanc to port"). See `test_roundings.py`.

### Routing fidelity 2g — sail-aware routing (per-sail polars + a peel cost)

2b attached a per-leg sail LABEL but the optimizer still routed on the Best-Performance *envelope*
(the max-over-sails speed) and peeled for free — so a route could thrash sails across a crossover at
zero cost, and the sail plan was a post-hoc per-leg guess. 2g makes the sail a first-class part of the
isochrone search:
- **Per-sail polars.** `build_speed_guide.py` now also emits `vps/db/seed/sr33_sail_polars.json` — the
  speed of EACH inventory sail (J1/A2/A3/S2) across its rated TWA domain, not just the envelope.
  `polars.sail_polars()` loads it (env `SAIL_POLARS_FILE`); empty → the optimizer routes on the
  envelope exactly as before.
- **Sail in the node state + hold-vs-peel.** `route_leg` carries the current sail; per step `sail_step`
  HOLDS it (at its OWN, slower-off-optimal per-sail speed) until it's `ROUTE_PEEL_HOLD_TOL` (6%) off the
  envelope-optimal sail, then PEELS to the optimal sail at full speed. A peel costs `ROUTE_PEEL_COST_S`
  (90 s, honest ETA) + a one-off `ROUTE_PEEL_PRUNE_S` prune penalty (mirrors the cumulative tack cost),
  so the isochrone disfavors a course that needs an extra peel — the route peels only when it pays. A
  kite is `0` outside its rated TWA domain (you can't fly it hard upwind → a forced peel); a jib
  change-down (J1→J2/J3) shares the J1 curve, so it's a free relabel, not a routing peel.
- **Continuity + a real sail plan.** The carried sail threads across marks (`start_sail`), so a peel at
  a rounding counts once; the route's `sail_plan` is rebuilt from the isochrone's own sail track (where
  it actually peeled), and the result carries per-leg `peels` + `total_peels`. The Gameplan cockpit
  shows a *sail peels* stat + a per-leg peel badge + a peels column in the CSV; the briefing states the
  real sail plan + peel count.

Env-flagged `ROUTE_SAIL_AWARE` (default ON) for A/B; off ⇒ envelope routing, geometry byte-identical.
Verified `test_routing_2g.py`: per-sail load + domain gate (kite infeasible upwind), carrying the wrong
sail into a leg peels to the right one (jib→kite on a run, kite→jib on a beat), a within-tolerance
sub-optimal sail is HELD (no thrash), SAIL_AWARE off reproduces the envelope baseline exactly, and
starting on the optimal sail adds no peel. End-to-end on the real cove_island course: `S2 → J1`, one
peel (the post-hoc labeler's spurious A3 transient correctly not flown).

### Realized (achievable) speed — helm-skill factor + sea state (routing fidelity 2d-d, phase 1)

The ORC polar is a FLAT-WATER, perfectly-sailed target; the boat never quite makes it. The optimizer
now routes on **realized** speed = `polar × helm_factor × wave_factor(hs, twa)`, so ETAs are achievable
(not theoretical) and the gap to the polar is a coaching number — the fuzzy-adherence baseline the whole
design hinges on.
- **Helm-skill factor** — a `BoatProfile.helm_factor` (0–1, default 1.0 = sails the book), editable in
  the Gameplan boat panel (`Helm %`); the Lab-4 learning loop can refine it from real tracks.
- **Sea state** — a `WaveField` seam (`vps/lab/app/wave.py`) parallel to the wind/current fields:
  `wave_at(lat,lon,epoch) → hs_m`. Phase 1 ships the seam (`ZeroWave` default — no behaviour change —
  + `ConstantWave` for tests / a `WAVES_CONST_HS` what-if). The degradation MODEL (`optimizer._wave_factor`)
  is source-agnostic and **deliberately CONSERVATIVE** (under-correcting beats distorting the route on an
  uncalibrated guess): a low-Hs **deadband** (`ROUTE_WAVE_HS_DEADBAND`=0.5 m — small chop costs nothing,
  so ripples never perturb the route), then a gentle slope on the *excess* Hs scaled by point of sail — a
  head sea slows most (`ROUTE_WAVE_K_UP`=0.04/m → ~6% at 2 m), a following sea least
  (`ROUTE_WAVE_K_DOWN`=0.01/m), capped by `ROUTE_WAVE_FLOOR`=0.6. Coefficients are PRIORS to be calibrated
  from the boat's realized-polar archive (Lab-4), not trusted as-is. **Phase 2 wires the real provider
  — `GLWUWave`:** NOAA GLWU (Great Lakes WAVEWATCH III) `HTSGW` (cfgrib `swh`) from the gridded 2.5 km
  `grlc_2p5km` product via the NOMADS GRIB-filter (the wind-model machinery, not THREDDS/unstructured) —
  one bbox download = every forecast hour (anl + hourly to ~149 h, 01/07/13/19Z cycles); curvilinear
  nearest-in-space + linear-in-time; the cfgrib parse runs in an isolated subprocess; outside the
  Great-Lakes domain / any miss → `ZeroWave`. `realized.wave_source` = `glwu`.
- **Threaded everywhere** like currents: the main optimize, the per-model fan, and the playbook
  consensus + every variant. The result carries a `realized` roll-up (`realized_pct`, `helm_factor`,
  `sea_state_hs_mean`) + per-leg `realized_factor`; the cockpit shows a *realized %* stat, and the
  briefing states "routing at ~N% of the flat-water polar (helm X%, sea state ~Y m)".
- **Per-run opt-out:** the optimize/playbook endpoints take `use_waves` (the Gameplan **"Sea-state
  (waves)"** checkbox, default on) → uncheck for pure flat-water (polar) routing; the helm factor still
  applies (crew efficiency, not waves).
- **Map overlay:** the Hs field is drawn on the slippy map as a shaded **heatmap** (`mapview.js`
  `drawWaves`, calm-teal→rough-red ramp, low opacity, UNDER the wind/current arrows so they stay
  legible), scrubbed by the same forecast slider. Fed by `result.wave_grid` (Hs on the same bbox/times
  as the wind/current grids via `WaveField.sample_grid`, emitted by `_wave_grid` only when peak Hs ≥
  `WAVE_GRID_MIN_HS`=0.25 m). A **"Sea state"** layer toggle (default OFF) + an Hs legend join the
  Control Center.

Env-flagged + default no-op (helm 1.0 + flat water ⇒ geometry/ETA byte-identical to baseline). Verified
`test_routing_realized.py` (wave factor shape + deadband + point-of-sail scaling + floor, helm slows the
ETA and is reported, sea state degrades a beat more than a run, default == baseline) + `test_routing_waves.py`
(GLWUWave nearest + time-blend + NaN-land/out-of-grid → flat + peak + cycle-picker/URL + non-GL reject)
+ LIVE on the real Bayview Mackinac cove_island (GLWU 19Z, ~25 frames, ~0.4–0.8 m Hs, `realized` carries
`wave_source:glwu`). Tunables `ROUTE_WAVE_HS_DEADBAND` / `ROUTE_WAVE_K_UP` /
`ROUTE_WAVE_K_REACH` / `ROUTE_WAVE_K_DOWN` / `ROUTE_WAVE_FLOOR` / `WAVES_ENABLED` / `WAVES_CONST_HS` +
per-run `use_waves` + the GLWU `WAVES_STEP_H` / `WAVES_MAX_SLICES` / `WAVES_FETCH_TIMEOUT` /
`WAVES_PARSE_TIMEOUT` / `WAVES_CYCLE_LAG_H` / `WAVES_CYCLE_FALLBACKS` / `WAVES_GLWU_PRODUCT` knobs.

## Debrief — actual-track ingestion (helm vs optimal, Lab-4 enrichment)

The judge loop (`app/judge.py`) re-routes the course on the wind that actually blew (the ORACLE) and
measures plan-vs-foresight regret. `app/track.py` adds the boat's **real sailed track**, scored against
that oracle line — the perflab §5 fuzzy-adherence metrics (time behind optimal, oversail %, XTE off the
optimal route, the first-beat side the boat actually WORKED, and % of the flat-water polar the helm
achieved from the realized wind). The boat never sails the optimal line exactly, so these are coaching
deltas, never pass/fail; the Opus critique separates tactical vs helm-execution vs conditions/luck, and
self-flags non-physical readings (>100% polar ≈ current/soft rating; boat "faster than oracle" ≈
oracle-window mismatch). Two inputs:
- **GPX upload** (`POST /api/debrief/track/upload?race_id=`, multipart) — the certain, offline path:
  export the track from Expedition / a Vakaros / the instruments / a phone. Namespace-agnostic trkpt
  parse; sog/cog derived between fixes.
- **YB our-boat** (`POST /api/debrief/track/fetch {race_id, boat}`) — auto-fetch our boat's full sailed
  track from the permitted public YB tracker. The JSON GetPositions feed carries only the LATEST fix;
  the full track is the delta-encoded **AllPositions3** binary, decoded by `_decode_allpositions3`
  (format reverse-engineered + verified — see the YB-format reference). Our boat is identified by
  matching a decoded block's latest fix to its GetPositions fix (self-validating), falling back to the
  RaceSetup team index. Shore-side debrief use of a public tracker is always fine (the in-race
  onboard-use gate `rules_profile.tracker_permitted` is separate).
One stored track per race (`_track_<race_id>.json` on the ingested volume, `_`-prefixed so the race
library skips it); `GET /api/debrief/track` returns a lightweight polyline + status, `POST
/api/debrief/track/clear` removes it. The Debrief tab gains a **Boat track** card (upload / fetch /
remove) and a **Helm vs optimal** scorecard; `judge.run_judge` fills the report's `actual_track` slot
and feeds it to the critique. Verified `test_routing_track.py` (synthetic AllPositions3 round-trip +
two-team resync + GPX + scoring math + caveats) + a LIVE decode of the real bayviewmack2025 feed
(Illuminati Port Huron→Mackinac) + end-to-end (GPX upload → judge → scorecard, Playwright UI).
Tunables `TRACK_YB_TIMEOUT_S`.

- **Next:** wind-over-water correction (2nd-order); Lab-3 onboard executor; verify the YB fetch against
  the live bayviewmack2026 feed (~July 2026). Routing fidelity 2c/2e/2f/2g, the GSHHG coastline
  backstop, water currents, realized-speed **phases 1 + 2 (GLWU)**, and **Debrief actual-track
  ingestion** are **done**.

## Race documents (found 2026-06-17)

**2026 Bayview Mackinac** (static — auto-fetch works):
- NOR: `https://bycmack.com/Assets/pdf/2026NOR%20V6%20111925%20Approved_Post.pdf` · page `bycmack.com/nor/`
- SER + Cruising Division Rules: on the Official Board · Tracking: `bycmack.com/tracking`
- SI: 2026 not yet posted (~July 2026); 2025 SI is on `bycmack.com/sis/`

**Mills Trophy 2026** (generalization test — Jun 5–7, Toledo YC + Storm Trysail):
- Hub: `yachtscoring.com/emenu/50579` (NOR/SI here, but **JS-rendered → use the paste-link path**)
- Course: `mi6657.wixsite.com/mills-trophy-race/course` (Mills 67.8 / Governor's Cup 52.6 /
  President's 37.6 / Alternate 22.6 nm) · ORC: `orc.org/mills-trophy-race-2026`
