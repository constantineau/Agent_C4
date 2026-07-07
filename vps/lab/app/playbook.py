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


def _mean_xte_nm(path, ref_path, step=8):
    """Mean lateral distance of `path`'s sampled points off the `ref_path` polyline — the
    route-vs-nominal divergence score for the v2 dedupe (docs/PLAYBOOK_V2.md §3)."""
    from . import track as track_mod
    ref = [(p["lat"], p["lon"]) for p in (ref_path or [])]
    pts = [(p["lat"], p["lon"]) for p in (path or [])][::max(1, step)]
    if len(ref) < 2 or not pts:
        return None
    xs = [x for x in (track_mod._xte_to_path(p, ref) for p in pts) if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _scenario_fan(definition, course_id, start_epoch, wf, consensus, cur, waves, marks_finish,
                  jib_crossovers=None, helm_factor=1.0, polar_adjustments=None, wave_coeffs=None,
                  max_scenarios=None, per_budget_s=90, log=None):
    """Playbook-v2 EXTERNAL scenario fan (docs/PLAYBOOK_V2.md §3, §8): route the course through
    perturbed views of the SAME blended field. A scenario whose route sticks to the nominal is
    ROBUSTNESS EVIDENCE, not a play; one that diverges is a play candidate. Priority order is
    point-of-sail aware (input #6). Returns (scenario_routes, robustness, profile, corridor)."""
    from . import scenarios as scen, track as track_mod
    say = (log.append if log is not None else (lambda *_: None))
    profile = scen.pos_profile(consensus)
    routes, robust = [], []
    xtes, detas = [], []
    for s in scen.select(profile, max_n=max_scenarios):
        wf2, overrides = scen.apply(s, wf)
        wc = wave_coeffs
        if overrides and overrides.get("wave_scale"):
            base = dict(wave_coeffs or {})
            f = overrides["wave_scale"]
            wc = {**base, **{k: round(base.get(k, d) * f, 3) for k, d in
                             (("k_up", 0.04), ("k_reach", 0.02), ("k_down", 0.01))}}
        r = optimizer.optimize_course(definition, course_id, start_epoch, wf2,
                                      time_budget_s=per_budget_s, resolution="fast",
                                      jib_crossovers=jib_crossovers, emit_exploration=False,
                                      cur=cur, waves=waves, helm_factor=helm_factor,
                                      polar_adjustments=polar_adjustments, wave_coeffs=wc)
        if not r.get("available") or not r.get("path"):
            say(f"scenario {s['id']}: no route — skipped")
            continue
        last = r["path"][-1]
        if marks_finish and track_mod._hav_nm((last["lat"], last["lon"]), marks_finish) > 3.0:
            say(f"scenario {s['id']}: truncated route — skipped (not counted as robustness)")
            continue
        deta = round(((r.get("total_hours") or 0) - (consensus.get("total_hours") or 0)) * 60)
        xte = _mean_xte_nm(r.get("path"), consensus.get("path"))
        entry = {"id": s["id"], "name": s["name"], "kind": s["kind"], "params": s["params"],
                 "category": "external", "narrative_seed": s["narrative"],
                 "divergence": {"delta_eta_min": deta, "xte_mean_nm": xte},
                 "total_hours": r.get("total_hours"),
                 "favored_side": _favored_side(definition, course_id, r)}
        if xte is not None:
            xtes.append(xte)
        detas.append(abs(deta))
        # DEDUPE (locked design §3): sticks to the nominal → evidence, not a play
        if xte is not None and xte < 2.0 and abs(deta) < 45:
            robust.append({"scenario": s["id"], "name": s["name"],
                           "note": f"route holds within {xte} nm / {abs(deta)} min of the nominal"})
            say(f"scenario {s['id']}: nominal HOLDS ({xte} nm, {deta:+d} min) — robustness")
        else:
            entry["route"] = {"legs": r.get("legs"), "path": r.get("path"),
                              "total_sailed_nm": r.get("total_sailed_nm"),
                              "total_tacks": r.get("total_tacks"), "sail_plan": r.get("sail_plan")}
            routes.append(entry)
            say(f"scenario {s['id']}: DIVERGES ({xte} nm, {deta:+d} min) — play candidate")
    # corridor verdict (locked input #2): lateral spread vs the time stakes across the fan
    xs = sorted(xtes)
    corridor = {
        "corridor_p90_nm": xs[int(0.9 * (len(xs) - 1))] if xs else None,
        "stakes_min": max(detas) if detas else 0,
        "verdict": ("geometry" if (detas and max(detas) >= 60) else "execution"),
        "note": ("the lateral decision is worth material time — the side choice matters"
                 if (detas and max(detas) >= 60) else
                 "the corridor is wide and the stakes of the line are small — prioritize boat "
                 "speed and sail choice over the lateral position"),
    }
    return routes, robust, profile, corridor


_PACE_DELAYS_H = (2, 4, -2)          # behind / deep-behind / ahead (Phase C — the user-priority plays)
_GEAR_SAILS = ("A2", "A3", "S2")     # kites we author a loss play for when they're in the nominal plan


def _internal_fan(definition, course_id, start_epoch, wf, consensus, cur, waves, marks,
                  jib_crossovers=None, helm_factor=1.0, polar_adjustments=None, wave_coeffs=None,
                  per_budget_s=90, log=None):
    """Phase-C INTERNAL plays that need routes (docs/PLAYBOOK_V2.md §3): PACE re-routes — reach an
    intermediate mark N hours late/early and the weather you meet downstream is different, so the
    optimal remainder can flip — and GEAR-LOSS re-runs (route the whole course without a kite that's
    in the nominal plan). Returns (routes, robustness) shaped like the external fan entries."""
    say = (log.append if log is not None else (lambda *_: None))
    routes, robust = [], []
    legs = consensus.get("legs") or []
    finish_pt = (marks[-1][2], marks[-1][3]) if marks else None
    from . import track as track_mod

    # ---- PACE plays: per intermediate mark × delay ---------------------------------------------
    eta_min = 0.0
    for k in range(1, len(marks) - 1):
        eta_min += float((legs[k - 1] or {}).get("leg_minutes") or 0) if k - 1 < len(legs) else 0
        mark_name = marks[k][0]
        eta_epoch = start_epoch + eta_min * 60
        nominal_rest_h = max(0.0, (consensus.get("total_hours") or 0) - eta_min / 60.0)
        for dh in _PACE_DELAYS_H:
            r = optimizer.optimize_course(definition, course_id, eta_epoch + dh * 3600, wf,
                                          from_mark=k, time_budget_s=per_budget_s,
                                          resolution="fast", jib_crossovers=jib_crossovers,
                                          emit_exploration=False, cur=cur, waves=waves,
                                          helm_factor=helm_factor,
                                          polar_adjustments=polar_adjustments,
                                          wave_coeffs=wave_coeffs)
            tag = f"{abs(dh)}h {'behind' if dh > 0 else 'ahead'}"
            sid = f"pace_{'behind' if dh > 0 else 'ahead'}_{abs(dh)}h_{k}"
            if not r.get("available") or not r.get("path"):
                say(f"pace {sid}: no route — skipped")
                continue
            last = r["path"][-1]
            if finish_pt and track_mod._hav_nm((last["lat"], last["lon"]), finish_pt) > 3.0:
                say(f"pace {sid}: truncated — skipped")
                continue
            xte = _mean_xte_nm(r.get("path"), consensus.get("path"))
            drest = round(((r.get("total_hours") or 0) - nominal_rest_h) * 60)
            same_sails = _sail_seq(r) == _sail_seq_from(consensus, k)
            entry = {"id": sid, "name": f"{tag} at {mark_name}", "kind": "pace",
                     "params": {"delay_h": dh, "mark": k, "mark_name": mark_name},
                     "category": "internal",
                     "narrative_seed": (f"Reaching {mark_name} ~{abs(dh)}h "
                                        f"{'later' if dh > 0 else 'earlier'} than the plan — the "
                                        "wind you meet on the remaining legs is different from "
                                        "what the nominal was optimized through."),
                     "divergence": {"delta_eta_min": drest, "xte_mean_nm": xte},
                     "total_hours": r.get("total_hours"),
                     "favored_side": _favored_side(definition, course_id, r)}
            if xte is not None and xte < 2.0 and same_sails:
                robust.append({"scenario": sid, "name": entry["name"],
                               "note": (f"{tag} at {mark_name}: the remaining plan HOLDS — same "
                                        f"line, same sails (rest {drest:+d} min)")})
                say(f"pace {sid}: nominal remainder HOLDS — robustness")
            else:
                entry["route"] = {"legs": r.get("legs"), "path": r.get("path"),
                                  "total_sailed_nm": r.get("total_sailed_nm"),
                                  "total_tacks": r.get("total_tacks"),
                                  "sail_plan": r.get("sail_plan")}
                routes.append(entry)
                say(f"pace {sid}: remainder CHANGES ({xte} nm, sails "
                    f"{'same' if same_sails else 'DIFFER'}) — play")

    # ---- GEAR-LOSS plays: kites actually in the nominal plan -----------------------------------
    nominal_sails = set(_sail_seq(consensus))
    for sail in [s for s in _GEAR_SAILS if s in nominal_sails]:
        r = optimizer.optimize_course(definition, course_id, start_epoch, wf,
                                      exclude_sails=[sail], time_budget_s=per_budget_s,
                                      resolution="fast", jib_crossovers=jib_crossovers,
                                      emit_exploration=False, cur=cur, waves=waves,
                                      helm_factor=helm_factor,
                                      polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs)
        if not r.get("available") or not r.get("path"):
            say(f"gear-loss {sail}: no route — skipped")
            continue
        deta = round(((r.get("total_hours") or 0) - (consensus.get("total_hours") or 0)) * 60)
        routes.append({"id": f"gear_loss_{sail.lower()}", "name": f"{sail} out of service",
                       "kind": "gear_loss", "params": {"sail": sail}, "category": "internal",
                       "narrative_seed": (f"The {sail} is out of service (blown/damaged) — the "
                                          "route and sail sequence re-planned without it."),
                       "divergence": {"delta_eta_min": deta,
                                      "xte_mean_nm": _mean_xte_nm(r.get("path"),
                                                                  consensus.get("path"))},
                       "total_hours": r.get("total_hours"),
                       "favored_side": _favored_side(definition, course_id, r),
                       "route": {"legs": r.get("legs"), "path": r.get("path"),
                                 "total_sailed_nm": r.get("total_sailed_nm"),
                                 "total_tacks": r.get("total_tacks"),
                                 "sail_plan": r.get("sail_plan")}})
        say(f"gear-loss {sail}: route without it costs {deta:+d} min — play")
    return routes, robust


def _sail_seq(result):
    seq, out = (result or {}).get("sail_plan") or [], []
    for s in seq:
        s = s.get("sail") if isinstance(s, dict) else s
        if s and (not out or out[-1] != s):
            out.append(s)
    return out


def _sail_seq_from(consensus, k):
    """The nominal's sail sequence from leg k onward (consecutive-deduped)."""
    out = []
    for leg in (consensus.get("legs") or [])[k:]:
        s = leg.get("sail")
        if s and (not out or out[-1] != s):
            out.append(s)
    return out


def build_playbook(definition, course_id, start_epoch, models, ensemble_members=0,
                   time_budget_s=200, jib_crossovers=None, helm_factor=1.0, use_waves=True,
                   polar_adjustments=None, wave_coeffs=None, v2_scenarios=True,
                   scenario_budget_s=420, max_scenarios=None):
    """Multi-scenario playbook: route the course through the blended field (consensus) and through
    each model's sub-field (scenarios), cluster by favored side into variants."""
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"available": False, "note": "course has no geocoded marks — review Course & Marks"}
    hours = optimizer.estimate_hours(definition, course_id)
    # the v2 pace plays re-route the remainder after a +4h-late mark arrival — the field must
    # reach past the nominal window or those runs fall onto fallback wind
    t_end = start_epoch + (hours + (6 if v2_scenarios else 0)) * 3600
    log: list[str] = []
    wf = build_windfield(bbox, start_epoch, t_end, models=models,
                         ensemble_members=ensemble_members, on_progress=log.append)
    if not wf.loaded:
        return {"available": False, "note": "no weather model data could be loaded",
                "windfield": wf.status(), "log": log}

    # water current (set & drift) — every playbook route crabs through the SAME current as the main
    # optimize, so the variants/consensus reflect a fair/foul stream, not just wind. Off-domain / miss
    # → ZeroCurrent (routes unchanged). Built once here and shared across consensus + every sub-field.
    from . import current as currentmod, wave as wavemod
    cur = currentmod.build_currentfield(bbox, start_epoch, t_end, on_progress=log.append)
    waves = (wavemod.build_wavefield(bbox, start_epoch, t_end, on_progress=log.append)
             if use_waves else wavemod.ZeroWave())

    subs = _subfields(wf)
    per = max(40, int(time_budget_s / (len(subs) + 1)))     # split the budget across routes

    # consensus = the blended field (all models) — the baseline "best guess"
    consensus = optimizer.optimize_course(definition, course_id, start_epoch, wf, time_budget_s=per,
                                          jib_crossovers=jib_crossovers, emit_exploration=False, cur=cur,
                                          waves=waves, helm_factor=helm_factor,
                                          polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs)
    consensus_side = _favored_side(definition, course_id, consensus)

    # one candidate route per model scenario
    candidates = []
    for model, sub in subs.items():
        r = optimizer.optimize_course(definition, course_id, start_epoch, sub, time_budget_s=per,
                                      jib_crossovers=jib_crossovers, emit_exploration=False, cur=cur,
                                      waves=waves, helm_factor=helm_factor,
                                      polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs)
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

    # ---- Playbook v2: the external scenario fan over the SAME field (docs/PLAYBOOK_V2.md) -------
    v2 = None
    if v2_scenarios:
        marks, _sk, _c = race_def.course_to_marks(definition, course_id)
        finish_pt = (marks[-1][2], marks[-1][3]) if marks else None
        n_scen = max_scenarios or 9
        s_routes, s_robust, profile, corridor = _scenario_fan(
            definition, course_id, start_epoch, wf, consensus, cur, waves, finish_pt,
            jib_crossovers=jib_crossovers, helm_factor=helm_factor,
            polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs,
            max_scenarios=n_scen, per_budget_s=max(60, int(scenario_budget_s / n_scen)), log=log)
        # Phase C INTERNAL plays — pace + gear-loss (the retro study ranked these the
        # highest-value play types; user priority)
        i_routes, i_robust = _internal_fan(
            definition, course_id, start_epoch, wf, consensus, cur, waves, marks,
            jib_crossovers=jib_crossovers, helm_factor=helm_factor,
            polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs,
            per_budget_s=max(60, int(scenario_budget_s / n_scen)), log=log)
        v2 = {"pos_profile": profile, "scenario_routes": s_routes + i_routes,
              "robustness": s_robust + i_robust, "corridor": corridor}

    return {
        "v2": v2,
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
        "windfield": wf.status(), "current": cur.status(), "waves": waves.status(),
        "realized": consensus.get("realized"), "log": log,
        "skipped_marks": consensus.get("skipped_marks", []),
    }
