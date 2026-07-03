# Venue-specific weather-model skill weighting

**Status:** design locked 2026-07-03 · Phase 1 in progress
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

Deriving the blend weight from N samples of vector error:
1. **De-bias.** If a model runs a persistent directional/speed offset here, subtract it before
   blending (often higher value than down-weighting alone).
2. **Inverse-error weight.** After de-biasing, `skill_weight ∝ 1 / MSE` — the optimal linear
   combination of independent estimators.
3. **Shrink toward the static priors when data is thin.** A pseudo-count prior: with few samples the
   weight barely moves off today's `priority`; it earns its swing only as evidence accumulates.
   `w = priority · (skill_weight·n + 1·k) / (n + k)` style shrinkage (k = prior strength).
4. **Cap the swing** (e.g. ×0.5…×2.0) so no single model can dominate or vanish from a noisy fit.

Default with no data ⇒ every skill_weight = 1, zero bias ⇒ **today's blend exactly**. The system
only changes routing once real venue history earns it.

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

## Storage (learning volume `/srv/learning`)

New SQLite table `model_skill_obs` (raw matched pairs, append-only, auditable) →
aggregated into `model_skill` (per venue×model: n, vector_rmse, speed_bias, dir_bias, updated_at).
Sits beside the existing Lab-4 `learning.db` tables (`debriefs`, `perf_bins`, `proposals`).

## Injection point

`wind/windfield.py: WindField.detail_at` blends with `MODELS[model].priority`. Thread an optional
`model_weights` + `model_bias` (per model) into the field so the effective weight is
`priority · skill_weight(model)` and each model is de-biased before the vector mean. Built by
`build_windfield(... venue=...)` which looks up the venue's `model_skill` row. Identity when absent.

## Phases

- **Phase 1 — verification substrate (value on race #1):** `venue.py` (bbox→venue key + registry),
  `modelskill.py` (obs providers: METAR + NDBC; historical-forecast provider: Open-Meteo; scorer:
  per-model bias + vector RMSE), and a standalone "which model was right here" report. No blend change.
- **Phase 2 — store + auto-weighting + display:** persist skill to `/srv/learning`; derive
  weights (de-bias + inverse-MSE + shrinkage + cap); thread `model_weights`/`model_bias` through
  `build_windfield`→`detail_at`; surface active weights + earning RMSE in the optimize result and a
  Lab panel; env kill-switch.
- **Phase 3 — refinements:** boat-instrument obs supplement; regime-conditional weighting
  (gradient vs thermal/lake-breeze); explicit lead-time buckets for longer planning horizons.
