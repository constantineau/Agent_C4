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

## Final venue result — Bayview Mackinac (multi-station, deep, 2026-07-03)

*Pooled stations: NDBC 45008 (open water) + Alpena KAPN (shore). Race window ±21 days, recency-weighted
(t½ 8 yr). GEFS = reference-only (not blended). 77-min backfill; the numbers below drive routing now.*

| model | vector RMSE | **weight** | veer bias removed | n | seasons | source |
|---|---|---|---|---|---|---|
| ICON | 3.89 kn | **×1.16** | +3° | 6,212 | 4 | OM 2023+ |
| **HRRR** | 3.92 kn | **×1.14** | −5° | 11,455 | **12** | OM + deep 2015+ |
| ECMWF | 3.99 kn | **×1.10** | +5° | 4,179 | 3 | OM 2024+ |
| GEM | 4.40 kn | **×0.91** | +6° | 6,212 | 4 | OM 2023+ |
| GFS | 4.79 kn | **×0.76** | +9° | 10,284 | 6 | OM 2021+ |
| *GEFS* | *5.22 kn* | *ref* | *+11°* | *3,666* | *15* | *deep 2005+ (not blended)* |

**Reading it:** with both regimes pooled, the top three — **ICON, HRRR, ECMWF — are effectively tied
(3.89–3.99 kn)**, and HRRR sits right there with the global flagships rather than dominating (shore) or
trailing (open-water). That's the honest, balanced answer: pooling the shore *and* the buoy averages
HRRR's near-shore edge with its smaller open-water advantage. **GFS is clearly the weakest (×0.76)**;
the coarse **GEFS reforecast** (15 deep seasons, ×ref) confirms the NCEP-global lineage runs highest
error here — and is correctly kept out of the blend so it doesn't drag `gfs`.

The **systematic global-model veer bias holds up across all history** (+3…+11°, removed before blending);
HRRR alone runs a small *opposite* (−5°) bias. De-biasing remains a real, free correction.

*Note the uneven Open-Meteo archive floors — GFS/HRRR 2021+, ICON/GEM 2023+, ECMWF 2024+ — so the
globals have only 3–4 seasons vs HRRR's 12 (deep). Shrink-to-priors keeps the thin-sample models
(esp. ECMWF, 3 seasons) from over-swinging; as their archives deepen the weights firm up.*

## Practical takeaways

- On the Great Lakes near shore, **trust HRRR (and NAM) over the global flagships** — measured, not
  assumed. Offshore/mid-lake the gap narrows.
- **De-biasing the globals' ~+10° veer** is a real, free accuracy gain.
- The weights are **auto-applied and always shown** in the GamePlan "Model skill — venue backtest"
  panel; "Deepen history (2005+)" runs the deep backfill on demand.
