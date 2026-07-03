# Model-skill backtest — findings (Bayview Mackinac / Lake Huron)

What the venue weather-model skill backtest actually *found*, as of 2026-07-03. Method + code:
[`MODEL_SKILL_WEIGHTING.md`](MODEL_SKILL_WEIGHTING.md). One line: we scored each model's **past
forecasts against observed wind** (METAR + NDBC buoy) over the July race window across many seasons,
recency-weighted, and the numbers below are the result. Metric = **vector RMSE (kn)** (speed + direction
error in one number); lower = better. Weight = how much that model counts in the blend (×its priority).

## Headline

**Model skill is strongly venue- and regime-dependent, and it's measurable.** On Lake Huron the
high-resolution mesoscale model (**HRRR**) beats the "best global model" (**ECMWF**) *near shore*, where
the lake-breeze gradient lives — the opposite of the global reputation ranking. A static equal-ish blend
was demonstrably mis-trusting the models here.

## Evidence

**1. Shore station (Alpena KAPN), single race week — 2025-07-12…14, 613 METAR obs:**

| model | vector RMSE | speed bias | dir bias |
|---|---|---|---|
| **HRRR** | **2.45 kn** | −0.5 | −1° |
| GEM | 3.17 | −0.5 | +11° |
| ICON | 3.54 | −1.1 | +10° |
| GFS | 3.56 | +0.2 | +12° |
| ECMWF | 3.71 | +0.5 | +13° |

**2. Shore station (KAPN), 5 July seasons (2021–2025 window, recency-weighted), n=4123:**
HRRR **2.85 kn → weight ×1.71**; ICON/ECMWF/GEM/GFS ~3.7–4.2 kn → ×0.8–1.0. The single-week result
holds up across five seasons — HRRR's edge here is **robust, not a fluke**.

**3. A systematic global-model direction bias.** Every global model (GFS/ICON/ECMWF/GEM) runs a
persistent **+6…+13° veer** vs observed that HRRR does not share. This is a clean, free correction: the
system **de-biases each model** (subtracts its measured offset) before blending — arguably as valuable
as the re-weighting.

## Two methodology findings (they changed the build)

**A. The answer is sensitive to which observation station anchors "truth."**
Re-scoring against a *mid-lake buoy (NDBC 45008)* instead of the shore airport **flipped HRRR to
mid-pack** and lifted ECMWF/ICON — because HRRR's advantage *is* the shore/lake-breeze structure, which
a mid-lake point doesn't see. Neither is "wrong"; they answer different questions (shore vs open-water
wind). → **Fix:** a venue now **pools both** a shore METAR and an over-water buoy, so skill reflects both
regimes. (Also: NDBC buoys measure wind at ~4–5 m, not 10 m, adding a uniform ~+1 kt speed bias that
inflates all RMSEs but largely cancels in the *relative* ranking.)

**B. Don't let a proxy contaminate a model's score.**
Using **GEFS Reforecast** (a coarse ensemble control) as the deep stand-in for **GFS** made GFS look far
worse than the operational model actually is (RMSE 5.4, +14° bias → ×0.72). → **Fix:** GFS stays on
clean operational data (Open-Meteo 2021+); the deep GEFS reforecast is tracked as its **own `gefs`
reference line** — shown, but not blended and kept out of the weighting math so it can't distort routed
models.

## Data depth & limits

- **The binding constraint is archived *forecasts*, not observations.** Observations go back decades
  (METAR ~1970s+, buoys 20–40 yr); the *forecast* archive is the wall.
- **Open-Meteo Historical Forecast** covers all models uniformly **2021→now** (~5 yr).
- **Deep GRIB archives** extend two lines further: **HRRR 2015+**, **GEFS reforecast 2005+** — uneven
  across models, absorbed by the shrink-to-priors gate.
- **Recency-weighted (t½ 8 yr):** recent seasons dominate (models are upgraded every 1–2 yr), but a
  ~2012 season still counts ~30%, so deep history genuinely informs without over-trusting stale model
  versions.
- **Lead time isn't perfectly matched** across sources (Open-Meteo ~day-ahead; deep GRIB same-day
  +6…18 h) — a known v1 simplification; relative ranking within a comparison stays fair.

## Final venue result — Bayview Mackinac (multi-station, 2005→2026, deep)

*Pooled stations: NDBC 45008 (open water) + Alpena KAPN (shore); GEFS = reference-only.*

> **[PENDING]** — the full multi-station deep backfill is running; this table is filled from
> `backfill2_mackinac.json` the moment it completes. (Interim single-station buoy-only run, for
> reference: ECMWF ×1.29 / ICON ×1.27 / HRRR ×0.93 / GEM ×0.92 / GFS ×0.72 — see finding A for why the
> buoy-only run under-rates HRRR.)

## Practical takeaways

- On the Great Lakes near shore, **trust HRRR (and NAM) over the global flagships** — measured, not
  assumed. Offshore/mid-lake the gap narrows.
- **De-biasing the globals' ~+10° veer** is a real, free accuracy gain.
- The weights are **auto-applied and always shown** in the GamePlan "Model skill — venue backtest"
  panel; "Deepen history (2005+)" runs the deep backfill on demand.
