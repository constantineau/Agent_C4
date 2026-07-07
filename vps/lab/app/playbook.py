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
import os
import time

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


_BISECT = os.environ.get("PB_BISECT", "1").strip().lower() in ("1", "true", "yes", "on")
_BISECT_MAX_PROBES = int(os.environ.get("PB_BISECT_MAX_PROBES", "4"))
# don't probe a gap smaller than the axis's meaningful resolution
_BISECT_MIN_STEP = {"rotation": 6.0, "tws_scale": 0.1, "time_shift": 2.0}


def _scenario_mag(kind, params):
    """(magnitude, sign) of a graduated scenario on its axis — rotation deg / tws offset from
    1.0 / timing hours. (None, 0) for non-graduated kinds (sea state)."""
    if kind == "rotation":
        v = float(params.get("deg", 0))
    elif kind == "tws_scale":
        v = float(params.get("scale", 1.0)) - 1.0
    elif kind == "time_shift":
        v = float(params.get("hours", 0))
    else:
        return None, 0
    return abs(v), (1 if v >= 0 else -1)


def _bisect_scenario(kind, sign, mag):
    """A synthetic midpoint scenario for the probe (scen.apply dispatches on kind+params)."""
    if kind == "rotation":
        return {"id": f"bisect_rot_{'r' if sign > 0 else 'l'}{round(mag)}", "kind": kind,
                "params": {"deg": sign * round(mag)}}
    if kind == "tws_scale":
        return {"id": f"bisect_tws_{'up' if sign > 0 else 'dn'}", "kind": kind,
                "params": {"scale": round(1.0 + sign * mag, 2)}}
    return {"id": f"bisect_time_{'e' if sign > 0 else 'l'}", "kind": kind,
            "params": {"hours": sign * round(mag * 2) / 2.0}}


def _bisect_param(kind, sign, mag):
    """The located threshold back in the axis's native param units (what detect() takes)."""
    if kind == "tws_scale":
        return round(1.0 + sign * mag, 2)
    if kind == "rotation":
        return sign * round(mag)
    return sign * round(mag * 2) / 2.0


def _scenario_fan(definition, course_id, start_epoch, wf, consensus, cur, waves, marks_finish,
                  jib_crossovers=None, sail_config=None, helm_factor=1.0, polar_adjustments=None,
                  wave_coeffs=None, max_scenarios=None, per_budget_s=90, deep=False,
                  log=None):
    """Playbook-v2 EXTERNAL scenario fan (docs/PLAYBOOK_V2.md §3, §8): route the course through
    perturbed views of the SAME blended field. A scenario whose route sticks to the nominal is
    ROBUSTNESS EVIDENCE, not a play; one that diverges is a play candidate. Priority order is
    point-of-sail aware (input #6). Returns (scenario_routes, robustness, profile, corridor)."""
    from . import scenarios as scen, track as track_mod
    say = (log.append if log is not None else (lambda *_: None))
    profile = scen.pos_profile(consensus)
    routes, robust = [], []
    xtes, detas = [], []
    cls: dict = {}          # (kind, sign) -> [(magnitude, diverged, entry)] for boundary bisection

    def _run(s):
        """Route ONE scenario + classify vs the nominal. Returns (entry, diverged) or None
        (unroutable/truncated — never counted as robustness)."""
        wf2, overrides = scen.apply(s, wf)
        wc = wave_coeffs
        if overrides and overrides.get("wave_scale"):
            base = dict(wave_coeffs or {})
            f = overrides["wave_scale"]
            wc = {**base, **{k: round(base.get(k, d) * f, 3) for k, d in
                             (("k_up", 0.04), ("k_reach", 0.02), ("k_down", 0.01))}}
        r = optimizer.optimize_course(definition, course_id, start_epoch, wf2,
                                      time_budget_s=per_budget_s, resolution="fast",
                                      jib_crossovers=jib_crossovers, sail_config=sail_config,
                                      emit_exploration=False,
                                      cur=cur, waves=waves, helm_factor=helm_factor,
                                      polar_adjustments=polar_adjustments, wave_coeffs=wc)
        if not r.get("available") or not r.get("path"):
            say(f"scenario {s['id']}: no route — skipped")
            return None
        last = r["path"][-1]
        if marks_finish and track_mod._hav_nm((last["lat"], last["lon"]), marks_finish) > 3.0:
            say(f"scenario {s['id']}: truncated route — skipped (not counted as robustness)")
            return None
        deta = round(((r.get("total_hours") or 0) - (consensus.get("total_hours") or 0)) * 60)
        xte = _mean_xte_nm(r.get("path"), consensus.get("path"))
        entry = {"id": s["id"], "name": s.get("name", s["id"]), "kind": s["kind"],
                 "params": s["params"], "category": "external",
                 "narrative_seed": s.get("narrative", ""),
                 "divergence": {"delta_eta_min": deta, "xte_mean_nm": xte},
                 "total_hours": r.get("total_hours"),
                 "favored_side": _favored_side(definition, course_id, r)}
        diverged = not (xte is not None and xte < 2.0 and abs(deta) < 45)
        if diverged:
            entry["route"] = {"legs": r.get("legs"), "path": r.get("path"),
                              "total_sailed_nm": r.get("total_sailed_nm"),
                              "total_tacks": r.get("total_tacks"), "sail_plan": r.get("sail_plan")}
        return entry, diverged

    for s in scen.select(profile, max_n=max_scenarios, deep=deep):
        got = _run(s)
        if not got:
            continue
        entry, diverged = got
        deta = entry["divergence"]["delta_eta_min"]
        xte = entry["divergence"]["xte_mean_nm"]
        if xte is not None:
            xtes.append(xte)
        detas.append(abs(deta))
        mag, sign = _scenario_mag(s["kind"], s["params"])
        if mag is not None:
            cls.setdefault((s["kind"], sign), []).append((mag, diverged, entry))
        if diverged:
            routes.append(entry)
            say(f"scenario {s['id']}: DIVERGES ({xte} nm, {deta:+d} min) — play candidate")
        else:
            robust.append({"scenario": s["id"], "name": entry["name"],
                           "note": f"route holds within {xte} nm / {abs(deta)} min of the nominal"})
            say(f"scenario {s['id']}: nominal HOLDS ({xte} nm, {deta:+d} min) — robustness")

    # BOUNDARY BISECTION (the method refinement, 2026-07-08): where adjacent scenarios of the same
    # axis straddle the hold/diverge boundary (e.g. +10° holds, +20° diverges), probe the midpoint
    # to LOCATE the flip — the located threshold becomes that play's arming predicate instead of a
    # generic band, so the fan spends compute exactly where the answer changes. One probe per axis
    # side, capped, largest-stakes axes first; the probe route itself is never a play (dedupe).
    if _BISECT:
        cands = []
        for (kind, sign), rows in cls.items():
            held = [m for m, dv, _e in rows if not dv]
            div = sorted([(m, e) for m, dv, e in rows if dv], key=lambda t: t[0])
            if not div:
                continue
            h = max(held) if held else 0.0            # the nominal itself holds by definition
            d, d_entry = div[0]
            step = _BISECT_MIN_STEP.get(kind, 0)
            if d - h > step and d_entry.get("route"):
                cands.append((abs(d_entry["divergence"]["delta_eta_min"] or 0),
                              kind, sign, h, d, d_entry))
        cands.sort(reverse=True)
        for _stk, kind, sign, h, d, d_entry in cands[:_BISECT_MAX_PROBES]:
            mid = (h + d) / 2.0
            probe = _bisect_scenario(kind, sign, mid)
            say(f"bisect {kind}{'+' if sign > 0 else '−'}: holds at {h:g}, diverges at {d:g} — "
                f"probing {mid:g}")
            got = _run(probe)
            if not got:
                continue
            _entry, p_div = got
            refined = mid if p_div else (mid + d) / 2.0
            d_entry["boundary"] = {"holds": h, "diverges": d, "probed": mid,
                                   "probe_diverged": p_div,
                                   "refined_param": _bisect_param(kind, sign, refined),
                                   "threshold": round(refined, 2)}
            say(f"bisect {kind}{'+' if sign > 0 else '−'}: flip located ≈{refined:g} — "
                f"play {d_entry['id']} arms at the located threshold")
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
                  jib_crossovers=None, sail_config=None, helm_factor=1.0, polar_adjustments=None,
                  wave_coeffs=None, per_budget_s=90, log=None):
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
            def _route_rest(budget):
                return optimizer.optimize_course(definition, course_id, eta_epoch + dh * 3600, wf,
                                                 from_mark=k, time_budget_s=budget,
                                                 resolution="fast", jib_crossovers=jib_crossovers, sail_config=sail_config,
                                                 emit_exploration=False, cur=cur, waves=waves,
                                                 helm_factor=helm_factor,
                                                 polar_adjustments=polar_adjustments,
                                                 wave_coeffs=wave_coeffs)

            def _truncated(res):
                if not res.get("available") or not res.get("path"):
                    return True
                last = res["path"][-1]
                return bool(finish_pt and
                            track_mod._hav_nm((last["lat"], last["lon"]), finish_pt) > 3.0)

            tag = f"{abs(dh)}h {'behind' if dh > 0 else 'ahead'}"
            sid = f"pace_{'behind' if dh > 0 else 'ahead'}_{abs(dh)}h_{k}"
            r = _route_rest(per_budget_s)
            if _truncated(r):
                # usually the fast-resolution budget running out on a delayed (lighter-wind)
                # remainder, not a wind gap — one honest retry at double budget
                say(f"pace {sid}: truncated at {per_budget_s}s — retrying ×2 budget")
                r = _route_rest(per_budget_s * 2)
            if _truncated(r):
                say(f"pace {sid}: no complete route — skipped")
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
                                      resolution="fast", jib_crossovers=jib_crossovers, sail_config=sail_config,
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

    # ---- LOW-MANEUVER variant: conserve the crew (night / shorthanded / fatigue) ----------------
    # Re-route with the maneuver PRUNE penalties ×3–5 (docs/PLAYBOOK_V2.md §3) — the search avoids
    # tacks/peels much harder while the clock cost of each stays real, so the reported ETA delta vs
    # the nominal is honest. Nominal already minimal → robustness evidence, not a play.
    mult = float(os.environ.get("PB_LOWMAN_MULT", "4.0"))
    r = optimizer.optimize_course(definition, course_id, start_epoch, wf,
                                  maneuver_prune_mult=mult, time_budget_s=per_budget_s,
                                  resolution="fast", jib_crossovers=jib_crossovers, sail_config=sail_config,
                                  emit_exploration=False, cur=cur, waves=waves,
                                  helm_factor=helm_factor,
                                  polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs)
    if (r.get("available") and r.get("path") and not (
            finish_pt and track_mod._hav_nm((r["path"][-1]["lat"], r["path"][-1]["lon"]),
                                            finish_pt) > 3.0)):
        base_m = (consensus.get("total_tacks") or 0) + (consensus.get("total_peels") or 0)
        low_m = (r.get("total_tacks") or 0) + (r.get("total_peels") or 0)
        deta = round(((r.get("total_hours") or 0) - (consensus.get("total_hours") or 0)) * 60)
        if low_m >= base_m - 1:      # require a real reduction (≥2) — resolution noise isn't a play
            robust.append({"scenario": "low_maneuver", "name": "Low-maneuver (conserve the crew)",
                           "note": (f"the nominal is already the quiet route ({base_m} maneuvers — "
                                    f"biasing ×{mult:g} against tacks/peels found nothing quieter)")})
            say("low-maneuver: nominal already minimal — robustness")
        else:
            routes.append({
                "id": "low_maneuver", "name": "Low-maneuver (conserve the crew)",
                "kind": "low_maneuver", "category": "internal",
                "params": {"prune_mult": mult, "maneuvers": low_m, "nominal_maneuvers": base_m},
                "narrative_seed": (f"The crew is tired or shorthanded (night watches, a long race) "
                                   f"— this route trades ~{abs(deta)} min for "
                                   f"{base_m - low_m} fewer tacks/peels ({base_m} → {low_m})."),
                "divergence": {"delta_eta_min": deta,
                               "xte_mean_nm": _mean_xte_nm(r.get("path"), consensus.get("path"))},
                "total_hours": r.get("total_hours"),
                "favored_side": _favored_side(definition, course_id, r),
                "route": {"legs": r.get("legs"), "path": r.get("path"),
                          "total_sailed_nm": r.get("total_sailed_nm"),
                          "total_tacks": r.get("total_tacks"),
                          "sail_plan": r.get("sail_plan")}})
            say(f"low-maneuver: {base_m}→{low_m} maneuvers for {deta:+d} min — play")
    else:
        say("low-maneuver: no route — skipped")

    # ---- REJOIN-VS-CONTINUE tabulation (guidance play, no new track) ----------------------------
    try:
        from . import retro
        vs = retro.venue_stats() or {}
    except Exception:
        vs = {}
    off_nm = round(min(8.0, max(2.0, float(vs.get("xte_p90_nm") or 6.0))), 1)
    tab = _rejoin_tab(definition, course_id, wf, consensus, cur, waves, marks, off_nm,
                      jib_crossovers=jib_crossovers, sail_config=sail_config, helm_factor=helm_factor,
                      polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs, log=log)
    if tab:
        parts = []
        for row in tab:
            call = ("either line is even — hold what you have" if row["verdict"] == "even" else
                    f"CONTINUE to the mark (rejoining costs ~{row['delta_min']} min)"
                    if row["verdict"] == "continue" else
                    f"REJOIN the line (pressing on costs ~{-row['delta_min']} min)")
            parts.append(f"~{row['off_nm']:g} nm {row['side']} on the {row['to']} leg: {call}")
        worst = max(abs(row["delta_min"]) for row in tab)
        routes.append({
            "id": "rejoin_vs_continue", "name": "Off the line — rejoin or continue?",
            "kind": "rejoin", "category": "internal",
            "params": {"off_nm": off_nm, "consider_nm": vs.get("xte_median_nm"),
                       "commit_nm": vs.get("xte_p90_nm")},
            "narrative_seed": (f"You're a genuine departure off the optimizer's line (~{off_nm:g} nm "
                               "— beyond fleet-normal wander, not ordinary weave). Whether sailing "
                               "back to the line pays is tabulated per leg and side — don't "
                               "reflex-rejoin."),
            "divergence": {"delta_eta_min": worst, "xte_mean_nm": off_nm},
            "guidance": ("; ".join(parts) + ". Frozen-forecast numbers — if the onboard re-route "
                         "disagrees, trust the fresher one."),
            "table": tab, "total_hours": None, "favored_side": None})
        say(f"rejoin tab: {len(tab)} rows — guidance play")
    else:
        say("rejoin tab: no rows (legs too short or routing failed) — skipped")
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


_REJOIN_EVEN_MIN = 10.0        # |rejoin − continue| under this → "even" (don't call a coin flip)
_REJOIN_LEG_TIMEOUT_S = 25.0   # per-isochrone budget for a tabulation cell


def _rejoin_tab(definition, course_id, wf, consensus, cur, waves, marks, off_nm,
                jib_crossovers=None, sail_config=None, helm_factor=1.0, polar_adjustments=None,
                wave_coeffs=None, log=None):
    """Phase-C REJOIN-VS-CONTINUE tabulation (docs/PLAYBOOK_V2.md §3): from a representative
    off-track position on each leg (offset `off_nm` = the venue's commit-band XTE, both sides),
    is it faster to sail back to the optimizer's line or to press on to the mark from where you
    are? Pre-computed ashore so the onboard matcher ANSWERS the question the deviation trigger
    raises instead of just flagging it. CONTINUE = a fresh isochrone from the off position to the
    leg's mark; REJOIN = an isochrone to the nominal line ~1.5×off ahead (a diagonal rejoin, not
    a U-turn) + the nominal's own pace from there. Returns table rows (may be empty — short legs
    where the offset is comparable to the leg itself are skipped)."""
    from . import polars as POL, track as track_mod
    say = (log.append if log is not None else (lambda *_: None))
    path = consensus.get("path") or []
    legs = consensus.get("legs") or []
    if len(path) < 3 or not legs or len(marks) < 2:
        return []
    P = POL.apply_adjustments(POL.polars_stw(), polar_adjustments)
    if not P:
        return []
    SP = POL.sail_polars()
    obstacles = None
    try:
        from .geo import build_for_course
        bbox = optimizer.course_bbox(definition, course_id)
        if bbox:   # cache-shared with the scenario fan's optimize_course builds
            obstacles = build_for_course(definition, course_id, bbox)
    except Exception:
        obstacles = None
    rp = optimizer._resolution("fast")
    kw = dict(obstacles=obstacles, hstep=rp["hstep"], dt_cap=rp["dt_cap"], cur=cur,
              sail_polars=SP, jib_crossovers=jib_crossovers, sail_config=sail_config, waves=waves,
              helm_factor=helm_factor, wave_coeffs=wave_coeffs)
    rows = []
    leg_start_t = float(consensus.get("start_epoch") or path[0].get("t") or 0)
    for i, leg in enumerate(legs):
        eta = float(leg.get("eta_epoch") or 0)
        try:
            if i + 1 >= len(marks) or not eta:
                continue
            if (leg.get("direct_nm") or 0) < off_nm * 4:   # offset ≈ the leg itself → meaningless
                continue
            dlat, dlon = marks[i + 1][2], marks[i + 1][3]
            t_mid = (leg_start_t + eta) / 2.0
            idx = min(range(len(path)), key=lambda j: abs(float(path[j].get("t") or 0) - t_mid))
            if idx < 1 or idx >= len(path) - 2:
                continue
            p0 = path[idx]
            brg = _bearing(path[idx - 1]["lat"], path[idx - 1]["lon"],
                           path[idx + 1]["lat"], path[idx + 1]["lon"])
            # the rejoin target: the nominal path point ~1.5×off ahead of the abeam point
            ahead, j = 0.0, idx
            while j < len(path) - 1 and ahead < off_nm * 1.5:
                ahead += optimizer._hav_nm(path[j]["lat"], path[j]["lon"],
                                           path[j + 1]["lat"], path[j + 1]["lon"])
                j += 1
            rj = path[j]
            rj_nominal_min = max(0.0, (eta - float(rj.get("t") or eta)) / 60.0)
            for side, sgn in (("left", -90.0), ("right", 90.0)):
                olat, olon = optimizer._advance(p0["lat"], p0["lon"], (brg + sgn) % 360.0, off_nm)
                if obstacles is not None and obstacles.blocked(olat, olon):
                    continue                                # the offset position is on land/no-go
                cont = optimizer.route_leg(wf, P, olat, olon, t_mid, dlat, dlon,
                                           deadline=time.time() + _REJOIN_LEG_TIMEOUT_S, **kw)
                rejn = optimizer.route_leg(wf, P, olat, olon, t_mid, rj["lat"], rj["lon"],
                                           deadline=time.time() + _REJOIN_LEG_TIMEOUT_S, **kw)
                # route_leg returns the NEAREST node when it can't lay the target — verify arrival
                ce, re_ = cont["path"][-1], rejn["path"][-1]
                if (track_mod._hav_nm((ce["lat"], ce["lon"]), (dlat, dlon)) > 1.0 or
                        track_mod._hav_nm((re_["lat"], re_["lon"]), (rj["lat"], rj["lon"])) > 1.0):
                    say(f"rejoin tab leg {i} {side}: truncated isochrone — cell skipped")
                    continue
                cont_min = round((cont["eta"] - t_mid) / 60.0)
                rejn_min = round((rejn["eta"] - t_mid) / 60.0 + rj_nominal_min)
                if cont_min <= 0 or rejn_min <= 0:
                    continue
                delta = rejn_min - cont_min                 # + → continuing is faster
                verdict = ("even" if abs(delta) < _REJOIN_EVEN_MIN
                           else "continue" if delta > 0 else "rejoin")
                rows.append({"leg": i, "to": leg.get("to"), "side": side, "off_nm": off_nm,
                             "continue_min": cont_min, "rejoin_min": rejn_min,
                             "delta_min": int(delta), "verdict": verdict})
                say(f"rejoin tab leg {i} ({leg.get('to')}) {side}: continue {cont_min}m vs "
                    f"rejoin {rejn_min}m → {verdict}")
        finally:
            leg_start_t = eta or leg_start_t
    return rows


def build_playbook(definition, course_id, start_epoch, models, ensemble_members=0,
                   time_budget_s=200, jib_crossovers=None, sail_config=None, helm_factor=1.0, use_waves=True,
                   polar_adjustments=None, wave_coeffs=None, v2_scenarios=True,
                   scenario_budget_s=None, max_scenarios=None, fan_depth="standard"):
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
                                          jib_crossovers=jib_crossovers, sail_config=sail_config, emit_exploration=False, cur=cur,
                                          waves=waves, helm_factor=helm_factor,
                                          polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs)
    consensus_side = _favored_side(definition, course_id, consensus)

    # one candidate route per model scenario
    candidates = []
    for model, sub in subs.items():
        r = optimizer.optimize_course(definition, course_id, start_epoch, sub, time_budget_s=per,
                                      jib_crossovers=jib_crossovers, sail_config=sail_config, emit_exploration=False, cur=cur,
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
        # FAN DEPTH — the method behind 'how many scenarios' (user ask 2026-07-08): each scenario
        # is a full isochrone re-route, so depth trades synthesis wall-clock for decision-space
        # coverage. quick = race-morning refresh; standard = the always-informative core grid;
        # deep = the wide grid (±30° / ×0.6-1.4 / ±6 h) for early-week homework when time is cheap.
        # The dedupe keeps the LIBRARY honest at any depth — a scenario that sticks to the nominal
        # becomes robustness evidence, not a play.
        _FAN = {"quick": (6, 300, False), "standard": (9, 420, False), "deep": (15, 900, True)}
        _n, _budget, _deep = _FAN.get((fan_depth or "standard").lower(), _FAN["standard"])
        n_scen = max_scenarios or _n
        budget = scenario_budget_s or _budget
        s_routes, s_robust, profile, corridor = _scenario_fan(
            definition, course_id, start_epoch, wf, consensus, cur, waves, finish_pt,
            jib_crossovers=jib_crossovers, sail_config=sail_config, helm_factor=helm_factor,
            polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs,
            max_scenarios=n_scen, per_budget_s=max(60, int(budget / n_scen)), deep=_deep,
            log=log)
        # Phase C INTERNAL plays — pace + gear-loss (the retro study ranked these the
        # highest-value play types; user priority)
        i_routes, i_robust = _internal_fan(
            definition, course_id, start_epoch, wf, consensus, cur, waves, marks,
            jib_crossovers=jib_crossovers, sail_config=sail_config, helm_factor=helm_factor,
            polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs,
            per_budget_s=max(60, int(budget / n_scen)), log=log)
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
