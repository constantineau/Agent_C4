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
  **side** is not yet enforced (an island is avoided either side). Tunables: `GEO_RES_DEG`,
  `GEO_ISLAND_NM`.

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

- **Next:** the copilot's crew-facing narration increment (it now has a signed playbook + boat sail
  model to interpret). Routing fidelity 2c (isochrone VMG-gate/cone/adaptive) and the higher-res GSHHG
  coastline backstop are **done**; enforcing rounding **side** remains an optional upgrade.

## Race documents (found 2026-06-17)

**2026 Bayview Mackinac** (static — auto-fetch works):
- NOR: `https://bycmack.com/Assets/pdf/2026NOR%20V6%20111925%20Approved_Post.pdf` · page `bycmack.com/nor/`
- SER + Cruising Division Rules: on the Official Board · Tracking: `bycmack.com/tracking`
- SI: 2026 not yet posted (~July 2026); 2025 SI is on `bycmack.com/sis/`

**Mills Trophy 2026** (generalization test — Jun 5–7, Toledo YC + Storm Trysail):
- Hub: `yachtscoring.com/emenu/50579` (NOR/SI here, but **JS-rendered → use the paste-link path**)
- Course: `mi6657.wixsite.com/mills-trophy-race/course` (Mills 67.8 / Governor's Cup 52.6 /
  President's 37.6 / Alternate 22.6 nm) · ORC: `orc.org/mills-trophy-race-2026`
