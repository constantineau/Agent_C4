"""One full route run: weather → wind field → optimizer → result. Shared by the Lab and the Orin.

The Lab's "Optimize" button (`main._run_optimize`) and the boat's onboard re-route
(`pi/orin/reroute`, issue #124) both call `run_course`. The Lab passes its active boat and its
chart source; the Orin passes the boat profile it carries, its own weather sources (the plume's
GRIB cache first, downloads only within a time budget) and its own obstacle field. Everything the
two share — model-skill weighting, the wind field, current + sea state, the boat overlays, the
optimizer call — lives here once, so a route on the boat is the Lab's route.

Pure: no FastAPI, no labstate. Callers pick the boat and the chart.
"""
from __future__ import annotations

import datetime
import os

from shared import boat_profile

from . import boats, optimizer


def boat_inputs(boat):
    """The optimizer kwargs that come from a boat profile dict (None → no boat: raw polars)."""
    b = boat or {}
    return {
        # a fleet boat added from its ORC cert routes on ITS envelope (optimize_course then drops
        # the own-boat per-sail curves); the overlays below all read the same profile
        "polar": b.get("cert_polar") or None,
        "safety_depth": boats.safety_depth_m(boat),
        "jib_crossovers": b.get("jib_crossovers") or [],
        "sail_config": boats.sail_config(boat),
        "helm_factor": boats.helm_factor(boat),
        "polar_adjustments": boats.polar_adjustments(boat),
        "wave_coeffs": boats.wave_coeffs(boat),
    }


class _Log(list):
    """A list whose .append also mirrors to on_progress (the job status shows it live)."""

    def __init__(self, on_progress=None):
        super().__init__()
        self._cb = on_progress

    def append(self, msg):
        list.append(self, msg)
        if self._cb:
            try:
                self._cb(msg)
            except Exception:
                pass


def run_course(definition, course_id, start_epoch, models, *, ensemble_members=0, avoid=True,
               per_model=False, resolution="auto", use_waves=True, use_current=True,
               on_progress=None, boat=None, chart_source=None, model_skill=True,
               obstacles=None, time_budget_s=None, fallback_tries=None, bbox=None, t_end=None,
               briefing=True, grids=True, on_phase=None, leg_obstacles=None):
    """Blocking: build the multi-model wind field, route the course, write the briefing.

    `models` are model names or ModelSource instances (the Orin passes cache-first wrappers).
    `bbox` / `t_end` override the course box and window (the Orin widens the box to the one its
    GRIB cache already holds). `obstacles` is a prebuilt ObstacleField (else the optimizer builds
    one from the course box). `briefing` / `grids` drop the Lab-only extras (the Opus briefing, the
    map's wind/current/sea-state grids). `on_phase(name)` hears "weather" then "routing" (the
    Orin's progress). Lab defaults reproduce the Lab route exactly."""
    from .wind import build_windfield
    from . import current, wave
    bbox = bbox or optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"available": False, "note": "No mark positions yet. Fix them in Course & Marks."}
    if t_end is None:
        t_end = start_epoch + optimizer.estimate_hours(definition, course_id) * 3600
    log = _Log(on_progress)
    phase = on_phase or (lambda _p: None)
    phase("weather")
    # venue-specific model-skill weighting: weight each model by its measured past accuracy here,
    # de-bias its persistent offset. Best-effort — any failure falls back to the static-priority blend.
    skill = {"enabled": False}
    mweights = mbias = None
    if model_skill:
        try:
            from . import venue as venue_mod, modelskill
            names = [m if isinstance(m, str) else m.name for m in models]
            v = venue_mod.resolve(definition, course_id, bbox=bbox)
            race_date = datetime.datetime.fromtimestamp(start_epoch, datetime.timezone.utc).date()
            skill = (modelskill.venue_weights(v, models=names, race_date=race_date)
                     if v else {"enabled": False})
            if skill.get("enabled"):
                mweights, mbias = skill["model_weights"], skill["model_bias"]
                log.append(f"model-skill: {v['key']} via {skill['station']} → "
                           + ", ".join(f"{m}×{w}" for m, w in mweights.items()))
        except Exception as exc:
            log.append(f"model-skill: skipped ({exc})")
    wf = build_windfield(bbox, start_epoch, t_end, models=models,
                         ensemble_members=ensemble_members, on_progress=log.append,
                         model_weights=mweights, model_bias=mbias, fallback_tries=fallback_tries)
    if not wf.loaded:
        return {"available": False, "note": "No forecast could be loaded (not out yet, or no "
                "internet).", "windfield": wf.status(), "log": log}
    # water current: opt-out per run (compare with vs without) — LMHOFS surface currents, ZeroCurrent on any miss
    cur = (current.build_currentfield(bbox, start_epoch, t_end, on_progress=log.append)
           if use_current else current.ZeroCurrent())
    # sea state (phase 1 seam) — opt-out per run; helm factor still applies (it's crew efficiency, not waves)
    waves = (wave.build_wavefield(bbox, start_epoch, t_end, on_progress=log.append)
             if use_waves else wave.ZeroWave())
    phase("routing")
    result = optimizer.optimize_course(definition, course_id, start_epoch, wf, avoid=avoid,
                                       source=chart_source, obstacles=obstacles,
                                       leg_obstacles=leg_obstacles,
                                       time_budget_s=time_budget_s, per_model=per_model,
                                       resolution=resolution, cur=cur, waves=waves,
                                       **boat_inputs(boat))
    result["current"] = cur.status()
    result["waves"] = waves.status()
    result["model_skill"] = skill                # venue model-skill weights + scorecard (UI panel)
    if briefing:
        result["briefing"] = optimizer.briefing(result, definition.get("name", ""))
    result["boat"] = boat_profile.summary(boat) if boat else None
    if grids:
        wg = wind_grid(wf, bbox, start_epoch, result.get("finish_epoch", t_end))
        result["wind_grid"] = wg
        if cur.status().get("loaded"):                       # current overlay on the SAME times as wind
            cg = current_grid(cur, wg["bbox"], wg["times"], wg["step_deg"])
            if cg:                                           # only when some cell actually flows
                result["current_grid"] = cg
        if waves.status().get("loaded"):                     # sea-state heatmap on the SAME times as wind
            sg = wave_grid(waves, wg["bbox"], wg["times"], wg["step_deg"])
            if sg:                                           # only when there's meaningful sea state
                result["wave_grid"] = sg
    result["log"] = log
    return result


WIND_GRID_STEP_H = float(os.environ.get("WIND_GRID_STEP_H", "1"))     # forecast frame cadence (hours)
WIND_GRID_MAX_FRAMES = int(os.environ.get("WIND_GRID_MAX_FRAMES", "72"))
WIND_GRID_CELLS = int(os.environ.get("WIND_GRID_CELLS", "16"))


def wind_grid(wf, bbox, start_epoch, finish_epoch, n_cells=WIND_GRID_CELLS):
    """Multi-time wind-field grid for the map overlay + forecast time slider ([C]).

    Sampled at HOURLY (`WIND_GRID_STEP_H`) steps across the route window (capped at
    `WIND_GRID_MAX_FRAMES`); ~`n_cells` cells across the bbox. The slider scrubs the frames + moves
    the boat marker; each point carries confidence (model agreement) for the fuzzy-adherence shading."""
    n, s, w, e = bbox
    step = max(0.05, max(n - s, e - w) / n_cells)
    span = max(1.0, float(finish_epoch) - float(start_epoch))
    step_s = max(900.0, WIND_GRID_STEP_H * 3600.0)               # >= 15 min between frames
    n_times = min(WIND_GRID_MAX_FRAMES, max(2, int(span / step_s) + 1))
    times = [round(start_epoch + min(span, i * step_s)) for i in range(n_times)]
    if times[-1] < round(start_epoch + span):
        times.append(round(start_epoch + span))                  # always include the finish time
    frames = [wf.sample_grid(t, step, bbox) for t in times]
    return {"step_deg": round(step, 3), "step_h": WIND_GRID_STEP_H, "bbox": [n, s, w, e],
            "times": times, "frames": frames}


def current_grid(cur, bbox, times, step):
    """Set/drift grid for the map's CURRENT overlay (routing fidelity 2d follow-up), on the SAME bbox +
    times as `wind_grid` so one slider scrubs both. Returns None when nothing flows (drift < 0.05 kn
    everywhere) so a no-current race draws no overlay. Frames mirror the wind frames' lattice."""
    frames = [cur.sample_grid(t, step, bbox) for t in times]
    peak = max((c.get("drift", 0.0) for fr in frames for c in fr), default=0.0)
    if peak < 0.05:
        return None
    return {"step_deg": step, "bbox": bbox, "times": times, "frames": frames,
            "peak_drift_kn": round(peak, 2)}


WAVE_GRID_MIN_HS = float(os.environ.get("WAVE_GRID_MIN_HS", "0.25"))   # below this = effectively flat


def wave_grid(waves, bbox, times, step):
    """Significant-wave-height grid for the map's SEA-STATE heatmap (realized-speed follow-up), on the
    SAME bbox + times as `wind_grid` so one slider scrubs wind/current/waves together. Returns None
    when the sea is effectively flat (peak Hs < `WAVE_GRID_MIN_HS`) so a calm day draws no overlay."""
    frames = [waves.sample_grid(t, step, bbox) for t in times]
    peak = max((c.get("hs", 0.0) for fr in frames for c in fr), default=0.0)
    if peak < WAVE_GRID_MIN_HS:
        return None
    return {"step_deg": step, "bbox": bbox, "times": times, "frames": frames,
            "peak_hs_m": round(peak, 1)}
