# C4 Performance Lab (cloud) — Lab-0: race ingestion

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
- **Next:** the ingestion service (dual-input + Opus extraction + storage + the review UI), then
  wire the RaceDefinition into the navigator's course loader and the race gate's `rules_profile`.

## Race documents (found 2026-06-17)

**2026 Bayview Mackinac** (static — auto-fetch works):
- NOR: `https://bycmack.com/Assets/pdf/2026NOR%20V6%20111925%20Approved_Post.pdf` · page `bycmack.com/nor/`
- SER + Cruising Division Rules: on the Official Board · Tracking: `bycmack.com/tracking`
- SI: 2026 not yet posted (~July 2026); 2025 SI is on `bycmack.com/sis/`

**Mills Trophy 2026** (generalization test — Jun 5–7, Toledo YC + Storm Trysail):
- Hub: `yachtscoring.com/emenu/50579` (NOR/SI here, but **JS-rendered → use the paste-link path**)
- Course: `mi6657.wixsite.com/mills-trophy-race/course` (Mills 67.8 / Governor's Cup 52.6 /
  President's 37.6 / Alternate 22.6 nm) · ORC: `orc.org/mills-trophy-race-2026`
