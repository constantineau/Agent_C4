"""Lab-2 playbook engine — fan the Lab-1 optimizer across forecast SCENARIOS, then cluster the
candidate routes into a small set of strategic VARIANTS with the model-agreement distribution.

Lab-1 gives ONE optimal route through the blended multi-model wind field. Real strategy is fuzzy:
the models disagree, so the honest pre-race homework is "the LEFT route (GFS + NAM back it) vs the
RIGHT route (HRRR), and here's how much each is favored and the time cost of being wrong." We get
that for free from the multi-model field we already downloaded: split it into per-model sub-fields
(each a plausible "what if the wind follows this model" scenario), route the course through each,
and cluster the results by which side of the first beat they favor.

This is the heart of the branching playbook. Opus synthesis (rationale / tradeoffs / "what flips
it" / the decision tree) and the signed, onboard-loadable bundle build on top of this (Lab-2b/c).
RRS 41: all pre-race cloud homework, frozen at the gun.
"""
from __future__ import annotations

import math

from shared import race_def
from . import optimizer
from .wind import build_windfield
from .wind.windfield import WindField


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _favored_side(definition, course_id, result):
    """Which side of the first beat the route commits to: compare its first heading to the rhumb
    bearing to the first mark. Left/right of the rhumb = the classic 'which way off the line'."""
    marks, _s, _c = race_def.course_to_marks(definition, course_id)
    legs = result.get("legs") or []
    if len(marks) < 2 or not legs or legs[0].get("first_heading") is None:
        return "middle"
    rhumb = _bearing(marks[0][2], marks[0][3], marks[1][2], marks[1][3])
    d = ((legs[0]["first_heading"] - rhumb + 540) % 360) - 180
    return "left" if d < -10 else "right" if d > 10 else "middle"


_LABELS = {"left": "Left side", "right": "Right side", "middle": "Up the middle (rhumb)"}


def _subfields(wf: WindField):
    """Split the blended field into one scenario sub-field per model (its own series only)."""
    by_model: dict[str, dict] = {}
    for (model, member), frames in wf.series.items():
        by_model.setdefault(model, {})[(model, member)] = frames
    out = {}
    for model, series in by_model.items():
        meta = [m for m in wf.meta if m["model"] == model]
        out[model] = WindField(series, meta, wf.bbox, wf.t_start, wf.t_end)
    return out


def build_playbook(definition, course_id, start_epoch, models, ensemble_members=0,
                   time_budget_s=200):
    """Multi-scenario playbook: route the course through the blended field (consensus) and through
    each model's sub-field (scenarios), cluster by favored side into variants."""
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"available": False, "note": "course has no geocoded marks — review Course & Marks"}
    hours = optimizer.estimate_hours(definition, course_id)
    t_end = start_epoch + hours * 3600
    log: list[str] = []
    wf = build_windfield(bbox, start_epoch, t_end, models=models,
                         ensemble_members=ensemble_members, on_progress=log.append)
    if not wf.loaded:
        return {"available": False, "note": "no weather model data could be loaded",
                "windfield": wf.status(), "log": log}

    subs = _subfields(wf)
    per = max(40, int(time_budget_s / (len(subs) + 1)))     # split the budget across routes

    # consensus = the blended field (all models) — the baseline "best guess"
    consensus = optimizer.optimize_course(definition, course_id, start_epoch, wf, time_budget_s=per)
    consensus_side = _favored_side(definition, course_id, consensus)

    # one candidate route per model scenario
    candidates = []
    for model, sub in subs.items():
        r = optimizer.optimize_course(definition, course_id, start_epoch, sub, time_budget_s=per)
        if not r.get("available"):
            continue
        legs = r.get("legs") or []
        candidates.append({
            "scenario": model, "favored_side": _favored_side(definition, course_id, r),
            "total_hours": r.get("total_hours"),
            "first_heading": legs[0]["first_heading"] if legs else None,
            "result": r,
        })

    # cluster candidates by favored side → strategic variants
    variants = []
    for side in ("left", "middle", "right"):
        grp = [c for c in candidates if c["favored_side"] == side]
        if not grp:
            continue
        grp.sort(key=lambda c: c["total_hours"] if c["total_hours"] is not None else 9e9)
        rep = grp[len(grp) // 2]["result"]                 # median-time representative route
        hrs = [c["total_hours"] for c in grp if c["total_hours"] is not None]
        variants.append({
            "side": side, "label": _LABELS[side],
            "supported_by": [c["scenario"] for c in grp],
            "share": round(len(grp) / len(candidates), 2) if candidates else 0.0,
            "total_hours": rep.get("total_hours"),
            "hours_range": [min(hrs), max(hrs)] if hrs else None,
            "first_heading": (rep.get("legs") or [{}])[0].get("first_heading"),
            "route_confidence": rep.get("route_confidence"),
            "route": {"legs": rep.get("legs"), "path": rep.get("path"),
                      "total_sailed_nm": rep.get("total_sailed_nm"),
                      "total_tacks": rep.get("total_tacks"),
                      "sail_plan": rep.get("sail_plan")},
        })
    variants.sort(key=lambda v: -v["share"])

    # how strategically loaded is this start? (spread of total time across the side options)
    var_hours = [v["total_hours"] for v in variants if v["total_hours"] is not None]
    spread_min = round((max(var_hours) - min(var_hours)) * 60, 0) if len(var_hours) > 1 else 0.0

    return {
        "available": True, "course_id": consensus.get("course_id"),
        "start_epoch": round(float(start_epoch)),
        "n_scenarios": len(candidates), "n_variants": len(variants),
        "agreement": round(max((v["share"] for v in variants), default=0.0), 2),
        "decision_spread_min": spread_min,    # time gap between the side options — the stakes
        "consensus": {
            "favored_side": consensus_side, "total_hours": consensus.get("total_hours"),
            "route_confidence": consensus.get("route_confidence"),
            "legs": consensus.get("legs"), "path": consensus.get("path"),
        },
        "variants": variants,
        "scenarios": [{"model": c["scenario"], "favored_side": c["favored_side"],
                       "total_hours": c["total_hours"], "first_heading": c["first_heading"]}
                      for c in candidates],
        "windfield": wf.status(), "log": log,
        "skipped_marks": consensus.get("skipped_marks", []),
    }
