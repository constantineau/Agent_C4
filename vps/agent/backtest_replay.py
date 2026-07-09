"""Known-answer playbook backtest — host-side decision replay (docs/PLAYBOOK_V2.md §8).

Consumes the lab-produced `replay_input.json` (as-of-gun bundle + per-boat realized-wind
steps from the HRRR analysis chain) and drives the REAL onboard decision code — the same
selector.get_selector() and matcher._evaluate() the boat runs — one step at a time, exactly
the way test_selector/test_matcher stub the live reads.

Honest approximations (documented, deliberate):
  * tactics window is 180 min of ANALYSIS wind (the boat sees 12 min of anemometer noise;
    the replay sees the smooth synoptic evolution — the persistent-shift detector's formula
    is tactics.py's own, applied at the scale a 40 h race actually turns on).
  * drift compares realized-at-the-boat vs the frozen fingerprint (onboard it is
    current-forecast vs fingerprint; reconstructing evolving mid-race forecasts is a later
    enrichment — this replay's drift answers "had the frozen picture gone stale in fact").
  * fatigue/sail/polar/leg signals are None — the matcher's wind/track plays still replay;
    sail-state plays can't (no 2025 sail log exists).

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/backtest_replay.py <replay_input.json>
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app import selector, deviation, matcher            # noqa: E402
from app import drift as drift_mod                      # noqa: E402
from shared import windphrase as wp                     # noqa: E402

R_NM = 3440.065
TAC_WINDOW_S = 180 * 60          # replay shift window (see module docstring)


def _wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def _circ_mean(degs):
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(y, x)) % 360.0


def _slope(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def _dist_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R_NM * 2 * math.asin(math.sqrt(a))


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


def _xte_to_path(lat, lon, path):
    """(xte_nm signed [+ = right of track, facing along-course], plan_t at the projection) vs
    the route polyline. Local-plane projection — plenty at Lake Huron scales."""
    best = None
    coslat = math.cos(math.radians(lat))
    for a, b in zip(path, path[1:]):
        ax, ay = (a["lon"] - lon) * coslat * 60.0, (a["lat"] - lat) * 60.0   # nm, boat at origin
        bx, by = (b["lon"] - lon) * coslat * 60.0, (b["lat"] - lat) * 60.0
        vx, vy = bx - ax, by - ay
        seg2 = vx * vx + vy * vy
        f = max(0.0, min(1.0, (-(ax * vx + ay * vy)) / seg2)) if seg2 else 0.0
        px, py = ax + f * vx, ay + f * vy               # projection, boat-relative (nm)
        d_here = math.hypot(px, py)
        # sign: cross(track-dir, boat-from-projection); boat is at -p relative to the projection
        cross = vx * (-py) - vy * (-px)
        xt = math.copysign(d_here, -cross) if seg2 else d_here
        plan_t = None
        if a.get("t") is not None and b.get("t") is not None:
            plan_t = a["t"] + f * (b["t"] - a["t"])
        if best is None or d_here < best[0]:
            best = (d_here, xt, plan_t)
    if best is None:
        return 0.0, None
    return best[1], best[2]


# --------------------------------------------------------------------- per-step signal builders
def build_tactics(steps, i):
    """tactics.get_tactics()-shaped dict from the realized series — tactics.py's own math."""
    now = steps[i]
    win = [s for s in steps[max(0, i - 24):i + 1] if now["t"] - s["t"] <= TAC_WINDOW_S]
    if len(win) < 4:
        return {"available": False}
    twds = [s["twd"] for s in win]
    mean = _circ_mean(twds)
    devs = [_wrap180(v - mean) for v in twds]
    cur = now["twd"]
    osc = max(devs) - min(devs)
    t0 = win[0]["t"]
    slope = _slope([(s["t"] - t0) / 60.0 for s in win], devs)      # deg/min
    span_min = (win[-1]["t"] - t0) / 60.0
    persistent = abs(slope) * span_min > max(3.0, osc * 0.6)
    sign = 1 if slope > 0 else -1
    hdg = now.get("cog")
    pos, tack = "upwind", "—"
    if hdg is not None:
        rel = _wrap180(cur - hdg)
        twa = abs(rel)
        pos = "upwind" if twa < 70 else ("downwind" if twa > 110 else "reach")
        tack = "starboard" if rel > 0 else "port"
    favored = (wp.favored_side(sign, pos) or "either") if persistent else "either"
    return {"available": True, "tack": tack, "point_of_sail": pos,
            "favored_side": favored,
            "wind": {"now": round(cur, 1), "mean_12min": round(mean, 1),
                     "shift_deg": round(_wrap180(cur - mean), 1),
                     "oscillation_deg": round(osc, 1), "persistent": persistent,
                     "trend": "steady" if abs(slope) <= 0.2 else ("right" if slope > 0 else "left"),
                     "slope_deg_min": round(slope, 2)}}


def build_deviation(step, path, rec_id, band_state):
    xte, plan_t = _xte_to_path(step["lat"], step["lon"], path) if path else (0.0, None)
    behind = (step["t"] - plan_t) if plan_t is not None else 0.0
    xb = deviation._band(abs(xte), band_state.get("x", 0), deviation.XTE_CONSIDER_NM,
                         deviation.XTE_COMMIT_NM)
    bb = deviation._band(max(0.0, behind), band_state.get("b", 0), deviation.BEHIND_CONSIDER_S,
                         deviation.BEHIND_COMMIT_S)
    band_state["x"], band_state["b"] = xb, bb
    status = ["ok", "watch", "act"][max(xb, bb)]
    return {"available": True, "status": status, "variant": rec_id,
            "xte_nm": round(abs(xte), 2), "xte_side": "right" if xte > 0 else "left",
            "time_behind_s": round(behind)}


def build_drift(step, band_state):
    if step.get("fp_twd") is None:
        return {"available": False, "status": "na"}
    signed = _wrap180(step["twd"] - step["fp_twd"])
    tws_d = step["tws"] - step["fp_tws"]
    tb = deviation._band(abs(signed), band_state.get("t", 0), drift_mod.TWD_CONSIDER,
                         drift_mod.TWD_COMMIT)
    sb = deviation._band(abs(tws_d), band_state.get("s", 0), drift_mod.TWS_CONSIDER,
                         drift_mod.TWS_COMMIT)
    band_state["t"], band_state["s"] = tb, sb
    status = ["ok", "watch", "act"][max(tb, sb)]
    direction = ("right" if signed > drift_mod.DIR_TOL_DEG
                 else "left" if signed < -drift_mod.DIR_TOL_DEG else "steady")
    return {"available": True, "status": status, "drift_dir": direction,
            "drift_twd_deg": round(abs(signed), 1), "drift_twd_signed_deg": round(signed, 1),
            "drift_tws_kn": round(tws_d, 1),
            "ref_twd": step["fp_twd"], "now_twd": step["twd"]}


def matcher_signals(dev, dft, tac, step):
    return {"time_behind_min": (dev.get("time_behind_s") or 0) / 60.0,
            "xte_nm": dev.get("xte_nm"),
            "drift_twd_deg": dft.get("drift_twd_deg"),
            "drift_twd_signed_deg": dft.get("drift_twd_signed_deg"),
            "drift_tws_kn": dft.get("drift_tws_kn"),
            "shift_persistent": bool((tac.get("wind") or {}).get("persistent")),
            "fatigue_index": None, "hoisted_sail": None, "reef": None,
            "sail_out_of_service": [], "tws_kn": step.get("tws"),
            "polar_pct": None, "current_leg": None,
            "upcourse_tws_delta_kn": None, "upcourse_twd_shift_deg": None}


# ----------------------------------------------------------------------------------- replay
def replay(data):
    b = data["bundle"]
    bundle_stub = {"race_id": b["race_id"], "recommended": b["recommended"],
                   "agreement": b.get("agreement"), "headline": b.get("headline"),
                   "decision_spread_min": b.get("decision_spread_min"),
                   "variants": b["variants"]}
    path = b.get("recommended_path") or []
    plays = b.get("plays") or []
    report = {"recommended": b["recommended"], "agreement": b.get("agreement"),
              "headline": b.get("headline"), "boats": {}}

    for tid, boat in data["boats"].items():
        steps = boat["steps"]
        gun = boat["division_gun"]
        matcher.clear_state()
        dev_state, dft_state = {}, {}
        cur = {"tac": {"available": False}, "dev": {"available": False, "status": "na"},
               "dft": {"available": False, "status": "na"}}
        selector.deviation._load_playbook = lambda: bundle_stub
        selector.tactics.get_tactics = lambda route=None: cur["tac"]
        selector.deviation.get_deviation = lambda route=None: cur["dev"]
        selector.drift_mod.get_drift = lambda route=None: cur["dft"]

        timeline, transitions, armed_events = [], [], []
        prev_key, armed_prev = None, set()
        for i, step in enumerate(steps):
            cur["tac"] = build_tactics(steps, i)
            cur["dev"] = build_deviation(step, path, b["recommended"], dev_state)
            cur["dft"] = build_drift(step, dft_state)
            r = selector.get_selector(now=step["t"])
            sig = matcher_signals(cur["dev"], cur["dft"], cur["tac"], step)
            armed_now = set()
            for p in plays:
                try:
                    ev = matcher._evaluate(p, sig, step["t"])
                    if (ev or {}).get("status") == "armed":
                        armed_now.add(p.get("id"))
                except Exception:
                    continue
            h = (step["t"] - gun) / 3600.0
            row = {"h": round(h, 2), "action": r.get("action"), "value": r.get("value"),
                   "conf": r.get("confidence"), "target": r.get("target_variant"),
                   "persistent": (cur["tac"].get("wind") or {}).get("persistent"),
                   "favored": cur["tac"].get("favored_side"),
                   "pos": cur["tac"].get("point_of_sail"),
                   "twd": step["twd"], "tws": step["tws"],
                   "drift": cur["dft"].get("drift_twd_signed_deg"),
                   "xte": cur["dev"].get("xte_nm"), "side": cur["dev"].get("xte_side")}
            timeline.append(row)
            key = (r.get("action"), r.get("target_variant"), cur["tac"].get("favored_side"))
            if key != prev_key:
                transitions.append(row)
                prev_key = key
            for pid in armed_now - armed_prev:
                armed_events.append({"h": round(h, 2), "play": pid})
            armed_prev = armed_now

        first_right = next((r for r in timeline
                            if r["action"] in ("switch", "off_script")
                            and (r.get("target") == "right" or r.get("favored") == "right")), None)
        report["boats"][tid] = {
            "boat": boat["boat"], "rank_division": boat.get("rank_division"),
            "steps": len(steps), "transitions": transitions,
            "first_right_call_h": first_right["h"] if first_right else None,
            "armed_events": armed_events,
            "final": timeline[-1] if timeline else None,
        }
    return report


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "replay_input.json"
    with open(src) as f:
        data = json.load(f)
    rep = replay(data)
    out = os.path.join(os.path.dirname(os.path.abspath(src)), "backtest_report.json")
    with open(out, "w") as f:
        json.dump(rep, f, indent=1)

    print(f"AS-OF-GUN BUNDLE: recommended={rep['recommended']!r} agreement={rep['agreement']}")
    print(f"  headline: {rep['headline']}")
    for tid, r in rep["boats"].items():
        print(f"\n=== {r['boat']} (Div-I rank {r['rank_division']}, {r['steps']} steps) ===")
        for t in r["transitions"]:
            print(f"  T+{t['h']:6.2f}h  {t['action']:<10} {str(t['value'] or ''):<28} "
                  f"favored={t['favored'] or '—':<6} pers={'Y' if t['persistent'] else 'n'} "
                  f"drift={t['drift']}° xte={t['xte']}nm/{t['side']} pos={t['pos']}")
        fr = r["first_right_call_h"]
        print(f"  first RIGHT call: {'T+%.1fh' % fr if fr is not None else 'NEVER'}")
        if r["armed_events"]:
            print("  plays armed: " + ", ".join(f"{e['play']}@T+{e['h']:.1f}h" for e in r["armed_events"]))
    print(f"\nreport → {out}")


if __name__ == "__main__":
    main()
