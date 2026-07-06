# Fleet Retro Study — learning from past Mackinac races ("pre-Phase-B")

**Status:** design locked with the user 2026-07-06; building. Precedes Playbook v2 Phase B
(`PLAYBOOK_V2.md`) because its outputs — which departure scenarios actually occur, and at what
magnitudes — set Phase B's perturbation ranges and Phase D's predicate thresholds from data.

## 1. The question

For a past race, run OUR optimizer for EVERY boat (each on its own ORC polar) using only the
weather that was knowable at the gun — then compare each boat's actual sailed track against its own
optimal route, and correlate route-adherence with corrected finish rank. How far off the optimizer
line were the winners, where, and why?

## 2. Data inventory (probed live 2026-07-06)

| Piece | Source | Status |
|---|---|---|
| Full fleet tracks | YB `BIN/<race>/AllPositions3` (decoder in `track.py`) | `bayviewmack2025` serves (108 boats); 2022–2024 cold-stored (503) |
| Entries + start + ToT | YB `JSON/<race>/RaceSetup` — teams carry `start`, `finishedAt`, `tcf1/2/3`, `sail`, `model` | live for all years |
| Results | YB `leaderboard` + corrected = (finishedAt−start)×tcf | live for 2025 |
| Per-boat polars | ORC public cert dump `Allowances` block — s/nm at TWA 52–150° × TWS 4–16 kn + Beat/Run VMG + optimum angles (`3600/s_per_nm` → kts) | verified (732 USA certs) |
| Gun forecast | AWS archives via `.idx` byte-range (deepfc pattern): HRRR 2015+, GFS 0.25° 2021+ — freshest cycle ≤ start | machinery exists (`deepfc.py`) |
| Realized wind (oracle) | chained analyses/f00 frames from the same archives (the judge-loop oracle) | exists (`judge.py`) |

2026 goes live race week (~Jul 11) — the same pipeline becomes this year's post-race analyzer.

## 3. Persistence — EVERYTHING gathered is kept (user requirement)

New named volume **`lab_retro`** (`/srv/retro`) + a pure-stdlib SQLite DB (`retrostore.py`, same
pattern as `learning.py`/`modelskill`):

- `races` (race_id, start_epoch, course json, source meta) · `entries` (boat/sail/owner/model/tcf,
  division) · `tracks` (per-boat fix arrays, compressed json) · `results` (elapsed/corrected/rank)
- `certs` (raw ORC cert json) · `polars` (converted grid per boat)
- `runs` (optimizer inputs/outputs per boat: route, ETA, config, wind-field provenance)
- `scores` (per-boat adherence metrics + oracle regret)
- **`grib_files`** — the artifact registry: every GRIB used by a retro run (and by the model-skill
  backtests) is **PINNED — copied into `/srv/retro/grib/`** and indexed (model, cycle, fhr, bbox,
  sha256, byte size, source url). The NOMADS `lab_gribcache` volume stays a cache; the retro store
  is the durable archive, so no future cache eviction can lose a study's inputs.

## 4. Pipeline

1. **Ingest** (`retro.ingest_race`): RaceSetup + AllPositions3 + leaderboard → races/entries/
   tracks/results; corrected order computed per division from tcf.
2. **Polars** (`orcpolar.py`): match entries to ORC certs by sail# → yacht name (the `fleetimport`
   matching); Allowances → a per-boat polar grid shaped like the optimizer's canonical polars;
   store cert + polar. Unmatched boats are recorded and excluded from per-boat runs (stated, not
   silent).
3. **Gun-forecast field** (`histwind.py`): `ArchiveGFS`/`ArchiveHRRR` ModelSources — same
   `ModelSource` interface, but `fetch()` pulls UGRD/VGRD-10m messages by `.idx` byte-range from
   the AWS buckets for the freshest cycle ≤ the historical start, caching into the pinned store;
   `GribFrame`/`WindField`/`optimize_course` are reused unchanged.
4. **Batch** (`retro.run_fleet`): per matched boat, optimize the course on ITS polar through the
   gun field (one shared obstacle mask + field; per-boat polar swap) → store the run; score the
   boat's actual track against its OWN optimal (`track.py` metrics: XTE distribution, first-beat
   side, time-behind, oversail, current-corrected polar %) + oracle regret.
5. **Analysis** (`retro.report`): adherence-vs-corrected-rank correlation (within division —
   rating luck confound), divergence hotspots (where top boats consistently left the line),
   forecast-bust attribution (gun field vs oracle at the divergence), scenario-magnitude
   histograms → the Phase-B perturbation ranges.
6. **Surface**: a **Fleet retro** card in the Lab **Debrief** tab (run/ingest buttons + the
   findings table) — retro is debrief-across-the-fleet.

## 5. Honest confounds (stated in every report)

Corrected-time winners are partly rating luck → fleet-wide rank correlation, not winner anecdotes ·
tracker fixes are minutes apart → race-scale geometry only, no maneuver counting · tracks are SOG
over ground → current-model correction applied · cert vintage is current-year (nearest-year
approximation) · the gun field is GFS(+HRRR early hours) only — coarser than the live multi-model
blend, stated in run provenance.

## 6. What feeds forward

Phase-B perturbation ranges + Phase-D predicate thresholds (divergence magnitudes) · optimizer
physics gaps (systematic fleet-wide deviations) · route-level model skill (which model's route
would have scored best) · a full-system playbook backtest (later: replay the oracle wind through
drift/selector hour by hour against the synthesized playbook).
