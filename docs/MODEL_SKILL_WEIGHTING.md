# Venue-specific weather-model skill weighting

**Status:** Phases 1 + 2 (2a/2b/2c) **BUILT, VERIFIED, DEPLOYED** 2026-07-03 (lab.racertracer.net);
Phase 3 = future refinements.
**One line:** weight each weather model in the blend by how accurately it has *actually*
forecast the wind **at this venue in the past** — measured, not assumed.

## The idea

Model skill is venue- and regime-dependent. ECMWF is usually the best global model but coarse
near shorelines; HRRR/NAM resolve mesoscale lake-breeze and lake-effect structure that GFS/ICON
smear out — which matters enormously on the Great Lakes (Bayview Mackinac) and much less offshore.
Our blend currently combines models with a **static** per-model priority
(`wind/models.py`: GFS 1.0, NAM 1.1, HRRR 1.2, ECMWF 1.15…). That leaves skill on the table.

Instead: **look back at what each model actually forecast for past race windows at a venue,
compare it to what was actually observed, compute each model's real error, and weight by that.**
The more accurate a model has been *here*, the more it counts.

This is forecast-vs-observed verification, not model-vs-model agreement (which we already compute
as `confidence`). Agreement says "the models concur"; skill says "this model has been right here."

## Ground truth (the crux)

To score a past forecast you need two independent series over the same venue+window:

1. **The forecast as it was issued** — retrieved from the **Open-Meteo Historical Forecast API**
   (`historical-forecast-api.open-meteo.com`), which archives each model's real past forecasts,
   per model, by past date. Verified live: GFS, ECMWF-IFS, HRRR (`gfs_hrrr`), ICON, GEM individually
   retrievable. We do **not** need our own GRIB capture history — the past forecasts are queryable now.
   *Lead time:* v1 scores at the **day-ahead** horizon the archive naturally serves — i.e. what you'd
   actually have on race morning. Separate weighting for longer planning leads is a v2 refinement.

2. **The observed wind** — independent of any model:
   - **METAR** via the Iowa State ASOS archive (`mesonet.agron.iastate.edu`) — clean CSV, year-round,
     shore stations (Alpena KAPN, Port Huron, Mackinac). Verified live.
   - **NDBC buoys** — over-water truth (45003 N-Huron, 45007 Michigan…). Historical archive +
     `activestations.xml` for the live roster. Great Lakes buoys are seasonal (pulled before ice);
     summer coverage is good, METAR fills the gaps.
   - **Boat instrument TWS/TWD** from debrief tracks (`environment.wind.*`) — the most relevant truth
     where we've raced; folded in as a supplement (Phase 3).

   > Why not the judge's GRIB "oracle" wind? It's itself a model product — scoring GFS against a
   > GFS-flavored analysis is circular. The oracle stays for route-regret; skill uses independent obs.

## The math

For each (venue, model): match forecast hours to observed obs (±30 min), convert both to wind
vectors (u,v), and compute:
- **vector RMSE (kn)** — headline skill metric; captures speed *and* direction error in one number.
- **speed bias (kn)** and **direction bias (deg, circular)** — interpretable; direction bias is the
  input to de-biasing.

Deriving the blend weight (`modelskill.derive_weights`, as built) from the scored models:
1. **Gate.** Only models with `n ≥ MIN_N` (12) and ≥2 scored models participate (need a reference);
   the rest stay at weight 1 / no bias.
2. **Inverse-variance factor, geomean-normalized.** `inv_m = 1 / max(0.25, rmse_m)²`;
   `factor_m = inv_m / geomean(inv)`, so a model at the typical RMSE here gets factor 1.0, a better one
   >1, a worse one <1. (Inverse-variance is the optimal linear combination of independent estimators.)
3. **Shrink toward the static prior by sample count.** `shrink = n / (n + SHRINK_N)` (pseudo-count
   `SHRINK_N`=30); `weight = 1 + (factor − 1)·shrink`. Thin data barely moves off 1.0.
4. **Cap the swing** to `[CAP_LO, CAP_HI]` = ×0.5…×2.0 so no single model dominates or vanishes.
5. **De-bias**, likewise shrunk: `bias = (speed_bias, dir_bias)·shrink`, *removed* from each model in
   `detail_at` before the vector mean (often as valuable as the re-weighting).

The `weight` multiplies the model's static `priority` in the blend. Default with no data ⇒ every
weight = 1, zero bias ⇒ **today's blend exactly**. The system only changes routing once real venue
history earns it. Env-tunable: `MODEL_SKILL_MIN_N`, `_SHRINK_N`, `_CAP_LO`, `_CAP_HI`.

## Lookback, seasonality & recency

Don't score one recent stretch — score the **race's calendar window (±21 days) in every year we can
reach**, so the sample is the venue's *race-season regime* (July thermal/lake-breeze on Huron), not a
random six weeks. Then **weight each year by recency** (`0.5 ** ((ref_year − year)/HALFLIFE)`) because
models change over time (upgrades every 1–2 yr) — recent seasons must dominate.

**Half-life = 8 yr** (locked): a ~2012 season still counts ~30% of a 2025 one, so deep history genuinely
influences the weights while newer years lead. (Shorter half-lives make deep data irrelevant; 8 yr is
the point where the deep build pays off.)

**Depth by source** (the forecast archive is the binding constraint — obs go back decades):
- **Open-Meteo Historical Forecast** — clean JSON, **2021→now** (verified: 2021 ✓, 2018/2015 empty).
  The backbone; covers GFS/HRRR/ECMWF/ICON/GEM uniformly.
- **Deep (pre-2021), heavier GRIB pipeline** — byte-range `.idx` subsetting from public AWS buckets
  (both probed + confirmed accessible 2026-07-03):
  - **HRRR archive** `noaa-hrrr-bdp-pds` — **2014-08→now**, CONUS, operational. Our best model here.
  - **GEFS Reforecast v12** `noaa-gefs-retrospective` — **~2005→2019**, ensemble, *fixed 2020 model*
    (so no drift, but GEFS-line only). The one true multi-decade archived-forecast set.
  Deep coverage is **uneven across models** (HRRR 2015+, GEFS 2005+, others 2021+) — handled honestly:
  each model is scored on whatever years it has; recency weighting + the shrink-to-priors gate keep a
  thinly-covered model from swinging on sparse deep data.

**Known v1 simplification — lead time isn't perfectly matched across sources.** Open-Meteo serves ~day-
ahead; the deep GRIB pull uses a same-day 00Z +6..18 h band (chosen because it exists across the *full*
archive depth — HRRR's extended f24+ runs only begin ~2019). Because recency weighting makes the recent
Open-Meteo era dominate, the deep-lead contribution is a modest correction; exact lead-time buckets are
a Phase-3 item. Relative model ranking within a comparison stays fair (all models share the lead).

## Decisions (locked)

- **Ground truth:** NDBC + METAR spine, boat-instrument wind as a supplement.
- **Apply mode:** **auto-apply with shrinkage** (no manual approval gate). Made safe by hard
  shrinkage-to-priors when thin, a swing cap, and an env kill-switch (`MODEL_SKILL_WEIGHTING=off`).
- **Observability (required):** because it's automatic, the active venue weights **and the RMSE that
  earned them** are surfaced in the optimize result + a Lab "Model skill" panel. Never a silent box.
- **Venue key:** auto by bbox-centroid cell (~0.5°) into a venue registry, overridable by an explicit
  `race.venue_tag`. `bayviewmack2025` + `bayviewmack2026` → one venue.

## Model name map (ours → Open-Meteo)

| ours | Open-Meteo id | notes |
|---|---|---|
| gfs | `gfs_global` | |
| hrrr | `gfs_hrrr` | CONUS mesoscale |
| ecmwf | `ecmwf_ifs025` | |
| icon | `icon_global` | |
| gem | `gem_global` | Canadian |
| nam | *best-effort* | skipped if the archive lacks it |
| gefs / ecmwf-ens | — | ensembles verified via their mean (v2) |

## Storage (learning volume `/srv/learning`, in `learning.db` beside the Lab-4 tables)

One SQLite table `model_skill`, keyed `(venue_key, model)` — the aggregated scorecard, upserted per
refresh:

```
venue_key, model, station, obs_source, n, vector_rmse_kn, speed_bias_kn, dir_bias_deg,
window_start, window_end, updated_at, n_years, deep
```

`deep=1` marks a result that folded in the pre-2021 GRIB archives; `modelskill._fresh()` treats such a
row as **permanent** (historical data doesn't change) so the fast inline path never re-runs the heavy
backfill. (v1 stores the aggregate, not raw matched pairs — the seasonal window + recency weighting are
recomputed on refresh, not incrementally merged.)

## Injection point (as built)

`wind/windfield.py: WindField.detail_at` blends samples with `MODELS[model].priority`.
`build_windfield(..., model_weights=None, model_bias=None)` carries two optional per-model dicts onto
the field; `detail_at` then de-biases each model (`_debias_uv`, in kn/deg space) and uses effective
weight `priority · model_weights.get(model, 1.0)`. Both empty ⇒ the blend is byte-for-byte unchanged.
`main._run_optimize` resolves the venue (`venue.resolve`), calls `modelskill.venue_weights(v,
race_date=…)`, and passes the resulting `model_weights`/`model_bias` in — best-effort, so any failure
falls back to the static-priority blend. The applied payload rides out on `result["model_skill"]`.

## Phases

- **Phase 1 — verification substrate (value on race #1):** `venue.py` (bbox→venue key + registry),
  `modelskill.py` (obs providers: METAR + NDBC; historical-forecast provider: Open-Meteo; scorer:
  per-model bias + vector RMSE), and a standalone "which model was right here" report. No blend change.
- **Phase 2a — store + seasonal/recency backbone + auto-weighting (BUILT + VERIFIED 2026-07-03):**
  persist skill to `/srv/learning` (`model_skill` table, incl. `n_years`/`deep`); **seasonal,
  multi-year, recency-weighted** sampling (`refresh_venue_skill`); derive weights (de-bias +
  inverse-MSE + shrink-to-priors + cap); thread `model_weights`/`model_bias` through
  `build_windfield`→`detail_at`; env kill-switch. Verified live at KAPN: HRRR 2.85 kn across 5 July
  seasons (2022–2026, n=4123) → ×1.71; global models down-weighted. `forecast_series()` dispatches
  pre-2021 years to `fetch_reforecast()` (deep hook).
- **Phase 2b — deep GRIB pipeline (BUILT + VERIFIED 2026-07-03):** `deepfc.py` = `.idx` byte-range
  subsetting from AWS, parsed with the existing eccodes — **HRRR archive** (`hrrr_series`, 2015+) +
  **GEFS Reforecast v12** (`gefs_series`, 2005+, the NCEP-global 'gfs' deep proxy) at a same-day
  +6..18 h band (exists across the full archive depth). `fetch_reforecast` wires them in; deep runs
  ONLY via the explicit **`backfill_deep`** (offline, heavy) — the inline optimize path skips pre-2021
  years, and a deep result is marked `deep=True` → treated as permanent so optimize never clobbers it.
  Endpoints: `GET /api/model-skill` (read), `POST /api/model-skill/backfill` (run deep). Verified live:
  HRRR parsed back to 2015, GEFS to 2008; a bounded 2019–2026 backfill merged deep GRIB years with the
  Open-Meteo era (HRRR n=499 → ×1.70), uneven model coverage handled by shrink-to-priors.
- **Phase 2c — display (BUILT 2026-07-03):** a GamePlan **Model skill** rail panel (`optModelSkill`)
  shows each model's RMSE, applied weight (green ↑ / red ↓), veer-bias removed, sample n, the venue/
  station, season count + span, recency t½, and a `deep` badge — plus a **"Deepen history (2005+)"**
  button (`runModelSkillBackfill` → `POST /api/model-skill/backfill`). Required since weighting is auto.
- **Phase 3 — refinements (future):** boat-instrument obs supplement; regime-conditional weighting
  (gradient vs thermal/lake-breeze); explicit lead-time buckets for longer planning horizons; option to
  prefer a year-round METAR over a seasonal buoy when buoy gaps thin the sample.

## As-built: files, config, endpoints, operations

**Files** (all under `vps/lab/`):
- `app/modelskill.py` — obs providers (`fetch_metar`, `fetch_ndbc_realtime/historical/window`), the
  Open-Meteo forecast provider (`fetch_forecast`), the deep dispatch (`forecast_series`,
  `fetch_reforecast`), the weighted scorer (`_acc_*`, `score`, `_match`), seasonal/recency refresh
  (`refresh_venue_skill`, `_season_windows`, `_recency_weight`), the store, `derive_weights`,
  `venue_weights`, and `backfill_deep`. Self-test: `python3 -m app.modelskill` (a live venue report).
- `app/deepfc.py` — deep GRIB byte-range pipeline (`hrrr_uv/series`, `gefs_uv/series`, `.idx` parse,
  `_point_uv`). Self-test: `python3 -m app.deepfc` (one live HRRR + GEFS point fetch).
- `app/venue.py` — venue key from bbox centroid + curated Great-Lakes obs-station registry (`STATIONS`,
  `nearest_station`, `resolve`).
- `app/wind/windfield.py` — `build_windfield`/`WindField` carry `model_weights`+`model_bias`;
  `detail_at` de-biases + weights; `_debias_uv`.
- `app/main.py` — `_run_optimize` wiring + `GET /api/model-skill` + `POST /api/model-skill/backfill`.
- `web/app.js` — `optModelSkill` panel + `runModelSkillBackfill`.

**Model → source map:** Open-Meteo (2021+): gfs=`gfs_global`, hrrr=`gfs_hrrr`, ecmwf=`ecmwf_ifs025`,
icon=`icon_global`, gem=`gem_global`. Deep (pre-2021): hrrr→HRRR archive (2015+), gfs→GEFS Reforecast
v12 (2005+, NCEP-global proxy). ecmwf/icon/gem have no deep source → 2021+ only.

**Config (env):** `MODEL_SKILL_WEIGHTING` (on/off kill-switch) · `_RECENCY_HALFLIFE_Y` (8) ·
`_SEASON_PAD_DAYS` (21) · `_FIRST_YEAR` (2010 default; set 2005 for full GEFS depth) ·
`_TTL_S` (6 h; deep results are permanent regardless) · `_MIN_N` (12) · `_SHRINK_N` (30) ·
`_CAP_LO`/`_CAP_HI` (0.5/2.0). Open-Meteo floor = 2021 (constant).

**Endpoints:** `GET /api/model-skill?race_id=&course_id=` → the stored weights + scorecard (no
refetch). `POST /api/model-skill/backfill {race_id, course_id?, start_epoch?}` → runs the deep
backfill (heavy, synchronous — minutes) then returns the refreshed payload.

**Operating it:**
- **Automatic:** every optimize resolves the venue and applies stored weights (fast; Open-Meteo era
  only inline). First run at a venue seeds the 2021+ seasonal score; thereafter cached (`_TTL_S`).
- **Deepen history:** the GamePlan "Deepen history (2005+)" button, or `POST …/backfill`, or offline
  `MODEL_SKILL_FIRST_YEAR=2005 python3 -c "…venue.resolve…; modelskill.backfill_deep(v, race_date)"`.
  It's thousands of GRIB byte-range gets (tens of minutes) → run in the background; the result is
  written once at the end and marked `deep=1` (permanent).
- **Reset a venue:** `DELETE FROM model_skill WHERE venue_key=?` in `/srv/learning/learning.db`.

**Verified (2026-07-03):** de-bias round-trip, weight-derivation ordering, and the blend-shift are
unit-checked; live scoring gave HRRR 2.85 kn (5 July seasons, n=4123) → ×1.71 vs globals ~×0.8–0.9;
deep HRRR parsed to 2015 / GEFS to 2008; a bounded backfill merged deep years with the Open-Meteo era.
Deployed to lab.racertracer.net; a full 2005→2026 Bayview-Mackinac backfill (buoy 45008) followed.
