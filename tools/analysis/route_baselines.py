#!/usr/bin/env python3
"""Routing baselines (#255): the Lab optimizer on stored courses in steady synthetic wind.

Judging stick for the light-air tacking fix (#66) and the run layline fix (#65). Runs
`optimizer.optimize_course` exactly as the Lab's Optimize button does (same resolution preset,
same boat inputs, same per-leg obstacle masks), but on a constant wind field instead of GRIBs, so
there is no network and no API call. Per case it records tacks, gybes, the clock they cost,
completion (every leg reached, gate passed the right way, every port/starboard mark and island on
the right hand), min clearance to the obstacle mask, route time and optimizer runtime.

Courses: Bayview Mackinac Cove Island and Shore (the repo's race file; the live Lab's
push-test-2026-11 is the same course), a synthetic 5 nm windward/leeward and a synthetic 12 nm
single leg, both in open lower Lake Huron. The other stored races (Mills Trophy, ORC NA) have no
mark coordinates.

Wind directions are set per course from its axis (start -> finish, or start -> windward on the
W/L): axis = dead beat, +45, +90 = beam, +180 = dead run. The 12 nm leg is swept 0/10/20 deg
either side of dead upwind and dead downwind instead.

Run in the CI lab image with the repo mounted (tools/analysis/route_baselines.sh does this).
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

from app import optimizer as OPT
from app import routerun
from app.geo import build_leg_fields
from shared import race_def

START_EPOCH = 1784390400          # 2026-07-18 16:00Z; steady wind, so only a label
REPO = os.environ.get("REPO", "/repo")


class SteadyWind:
    """Same wind every hour, inside `bbox` (north, south, west, east). Never 'held', full
    confidence. The Lab fetches its GRIBs for the course box, so by default the wind stops at the
    box like a real forecast does (the router then borrows nearby wind up to FILL_NM past it).
    bbox=None: wind everywhere."""
    loaded = True

    def __init__(self, tws, twd, bbox=None):
        self.tws, self.twd = float(tws), float(twd) % 360
        self.bbox = bbox

    def _in(self, lat, lon):
        if self.bbox is None:
            return True
        n, s, w, e = self.bbox
        return s <= lat <= n and w <= lon <= e

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd) if self._in(lat, lon) else None

    def detail_at(self, lat, lon, t):
        if not self._in(lat, lon):
            return None
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": [], "synthetic": f"steady {self.tws} kn from {self.twd:.0f}"}


def wl_course(lat0=44.0, lon0=-82.3, leg_nm=5.0):
    """Synthetic windward/leeward: start, W1 (port), L1 (port), W2 (port), finish at the start.
    Open water, mid lower Lake Huron (Port Austin ~38 nm W, Kincardine ~33 nm E)."""
    up = lat0 + leg_nm / 60.0
    return {
        "race_id": "synthetic-wl", "name": "Synthetic W/L (lower Lake Huron)", "zones": [],
        "courses": [{
            "id": "wl", "name": f"W/L {leg_nm:g} nm x2",
            "start": {"lat": lat0, "lon": lon0},
            "marks": [
                {"seq": 1, "name": "Windward 1", "type": "mark", "rounding": "port", "lat": up, "lon": lon0},
                {"seq": 2, "name": "Leeward 1", "type": "mark", "rounding": "port", "lat": lat0, "lon": lon0},
                {"seq": 3, "name": "Windward 2", "type": "mark", "rounding": "port", "lat": up, "lon": lon0},
            ],
            "finish": {"points": [{"lat": lat0 - 0.2 / 60.0, "lon": lon0}]},
        }],
    }


def leg_course(lat0=44.0, lon0=-82.3, leg_nm=12.0):
    """One 12 nm leg due north in open water: the single-leg beat/run sweep (PR #62 used a 12 nm
    dead beat). Swept with the wind 0/10/20 deg either side of dead upwind and dead downwind."""
    return {
        "race_id": "synthetic-leg", "name": "Synthetic 12 nm leg", "zones": [],
        "courses": [{"id": "leg12", "name": "12 nm leg", "start": {"lat": lat0, "lon": lon0},
                     "marks": [], "finish": {"points": [{"lat": lat0 + leg_nm / 60.0, "lon": lon0}]}}],
    }


def load_courses():
    bv = json.load(open(os.path.join(REPO, "vps/lab/races/bayview_mackinac_2026.json")))
    return {"cove_island": (bv, "cove_island"), "shore": (bv, "shore"), "wl": (wl_course(), "wl"),
            "leg12": (leg_course(), "leg12")}


# wind directions relative to the course axis, per course (--rel overrides)
REL = {"leg12": "-20,-10,0,10,20,160,170,180,190,200"}
REL_DEFAULT = "0,45,90,180"


def rel_label(rel):
    r = OPT._wrap180(rel)
    if r == 0:
        return "beat"
    if abs(r) == 180:
        return "run"
    if abs(r) <= 30:
        return f"beat{r:+.0f}"
    if abs(r) >= 150:
        return f"run{(r - 180 if r > 0 else r + 180):+.0f}"
    return {45: "close", 90: "beam"}.get(abs(round(r)), f"{r:+.0f}")


# --- geometry ----------------------------------------------------------------
def _side(path, k, mlat, mlon):
    """Which hand (port/starboard) the point (mlat, mlon) is on, sailing path[k] -> path[k+1]."""
    i, j = (k, k + 1) if k + 1 < len(path) else (k - 1, k)
    h = math.radians(OPT._bearing(path[i]["lat"], path[i]["lon"], path[j]["lat"], path[j]["lon"]))
    ce = (mlon - path[k]["lon"]) * 60 * math.cos(math.radians(path[k]["lat"]))
    cn = (mlat - path[k]["lat"]) * 60
    return "port" if math.sin(h) * cn - math.cos(h) * ce > 0 else "starboard"


def _closest(path, lo, hi, lat, lon):
    return min(range(lo, hi), key=lambda i: OPT._hav_nm(path[i]["lat"], path[i]["lon"], lat, lon))


def _seg_cross(p1, p2, q1, q2):
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = ccw(q1, q2, p1), ccw(q1, q2, p2)
    return (d1 > 0) != (d2 > 0) and (ccw(p1, p2, q1) > 0) != (ccw(p1, p2, q2) > 0), d1, d2


def rounding_checks(definition, cid, res):
    """Every mark the crew must leave on a hand, checked on the routed path. [(name, ok, detail)]."""
    path = res["path"]
    course = next(c for c in definition["courses"] if c["id"] == cid)
    ends = [0] + list(res.get("leg_end_idx") or [])        # path index of each nav vertex
    nav = [m for m in course["marks"] if m.get("type") != "island"]
    out = []
    for m in course["marks"]:
        side = m.get("rounding")
        if m.get("type") == "gate":
            k = nav.index(m)
            lo, hi = (ends[k], ends[k + 1]) if k + 1 < len(ends) else (0, len(path) - 1)
            a, b = (m["lon"], m["lat"]), (m["lon2"], m["lat2"])
            hit = None
            for i in range(max(0, lo - 2), min(len(path) - 1, hi + 2)):
                p1 = (path[i]["lon"], path[i]["lat"]); p2 = (path[i + 1]["lon"], path[i + 1]["lat"])
                x, d1, d2 = _seg_cross(p1, p2, a, b)
                # the route aims at the gate midpoint, so it may touch it exactly at a vertex
                on_mid = OPT._hav_nm(path[i + 1]["lat"], path[i + 1]["lon"],
                                     (m["lat"] + m["lat2"]) / 2, (m["lon"] + m["lon2"]) / 2) < 0.05
                if x or on_mid:
                    nxt = path[min(i + 2, len(path) - 1)]
                    hit = nxt["lat"] > path[i]["lat"]     # SI 7.1: through from S to N
                    break
            out.append((m["name"], bool(hit), "S->N" if hit else ("wrong way" if hit is False else "missed")))
        elif side in ("port", "starboard"):
            if m.get("type") == "island":
                k = _closest(path, 0, len(path) - 1, m["lat"], m["lon"])
            else:
                j = nav.index(m) + 1                       # its leg ends at path[ends[j]]
                if j >= len(ends):
                    out.append((m["name"], False, "not reached")); continue
                k = ends[j]
                # the leg ends on a standoff on the legal side; a real rounding keeps the mark on
                # that hand coming in AND going out (#52: no exit constraint today)
                s_in = _side(path, max(0, k - 1), m["lat"], m["lon"])
                s_out = _side(path, k, m["lat"], m["lon"]) if k + 1 < len(path) else s_in
                out.append((m["name"], s_in == side and s_out == side, f"in {s_in}/out {s_out}"))
                continue
            got = _side(path, k, m["lat"], m["lon"])
            out.append((m["name"], got == side, f"left to {got}"))
    return out


def clearance(field, path, marks, sample_nm=0.1, radius_nm=6.0, skip_mark_nm=1.0):
    """Min distance (nm) from the path to a blocked cell centre: overall, and away from the nav
    marks (the carve opens 0.5 nm pockets at marks set near shore). Also the count of path segments
    that cross a blocked cell (should be 0). None = nothing within `radius_nm`."""
    if field is None or not getattr(field, "active", False):
        return None, None, 0
    M = np.frombuffer(bytes(field.mask), dtype=np.uint8).reshape(field.ny, field.nx)
    res = field.res
    best_all = best_away = None
    hits = 0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        if field.crosses(a["lat"], a["lon"], b["lat"], b["lon"]):
            hits += 1
        d = OPT._hav_nm(a["lat"], a["lon"], b["lat"], b["lon"])
        n = max(1, int(d / sample_nm))
        for s in range(n + (1 if i == len(path) - 2 else 0)):
            f = s / n
            lat = a["lat"] + (b["lat"] - a["lat"]) * f
            lon = a["lon"] + (b["lon"] - a["lon"]) * f
            ry = int(radius_nm / 60.0 / res) + 1
            rx = int(radius_nm / 60.0 / math.cos(math.radians(lat)) / res) + 1
            iy, ix = field._iy(lat), field._ix(lon)
            y0, y1 = max(0, iy - ry), min(field.ny, iy + ry + 1)
            x0, x1 = max(0, ix - rx), min(field.nx, ix + rx + 1)
            if y0 >= y1 or x0 >= x1:
                continue
            ys, xs = np.nonzero(M[y0:y1, x0:x1])
            if len(ys) == 0:
                continue
            clat = field.s + (ys + y0 + 0.5) * res
            clon = field.w + (xs + x0 + 0.5) * res
            dist = float(np.min(np.hypot((clat - lat) * 60.0,
                                         (clon - lon) * 60.0 * math.cos(math.radians(lat)))))
            best_all = dist if best_all is None else min(best_all, dist)
            if min(OPT._hav_nm(lat, lon, ml, mo) for _s, _n, ml, mo in marks) > skip_mark_nm:
                best_away = dist if best_away is None else min(best_away, dist)
    return best_all, best_away, hits


def _boards(path, lo, hi, twd):
    """[(side, twa)] for each non-empty segment of path[lo..hi] in this steady wind."""
    out = []
    for i in range(lo, hi):
        a, b = path[i], path[i + 1]
        if OPT._hav_nm(a["lat"], a["lon"], b["lat"], b["lon"]) < 1e-4:
            continue
        off = OPT._wrap180(twd - OPT._bearing(a["lat"], a["lon"], b["lat"], b["lon"]))
        out.append(("stbd" if off > 0 else "port", abs(off)))
    return out


def maneuvers(path, ends, twd):
    """Board changes on the routed path in this steady wind, split into tacks (mean TWA of the two
    boards < 90) and gybes, counted INSIDE each leg (as the optimizer counts). Board changes at a
    mark (last board of one leg -> first of the next) are counted apart. Per leg: [(tacks, gybes)]."""
    per_leg, at_marks = [], 0
    for j in range(len(ends) - 1):
        bs = _boards(path, ends[j], ends[j + 1], twd)
        tk = gy = 0
        for (s1, a1), (s2, a2) in zip(bs, bs[1:]):
            if s1 != s2:
                if (a1 + a2) / 2.0 < 90:
                    tk += 1
                else:
                    gy += 1
        per_leg.append((tk, gy))
        if j + 2 < len(ends):
            nxt = _boards(path, ends[j + 1], ends[j + 2], twd)
            if bs and nxt and bs[-1][0] != nxt[0][0]:
                at_marks += 1
    return per_leg, at_marks


# --- one case -----------------------------------------------------------------
def run_case(definition, cid, tws, twd, label, fields, boat_in, resolution, wind_everywhere=False):
    base, legs_obs = fields
    wf = SteadyWind(tws, twd, None if wind_everywhere else OPT.course_bbox(definition, cid))
    t0 = time.time()
    r = OPT.optimize_course(definition, cid, START_EPOCH, wf, obstacles=base, leg_obstacles=legs_obs,
                            emit_exploration=False, resolution=resolution, **boat_in)
    rt = time.time() - t0
    if not r.get("available"):
        return {"tws": tws, "twd": twd, "case": label, "error": r.get("note")}
    ends = [0] + list(r.get("leg_end_idx") or [])
    if ends[-1] != len(r["path"]) - 1:                  # an unfinished last leg
        ends.append(len(r["path"]) - 1)
    per_leg, at_marks = maneuvers(r["path"], ends, twd)
    tk, gy = sum(t for t, _g in per_leg), sum(g for _t, g in per_leg)
    marks = [(m["seq"], m["name"], m["lat"], m["lon"]) for m in r["marks"]]
    rounds = rounding_checks(definition, cid, r)
    c_all, c_away, hits = clearance(base, r["path"], marks)
    n, so, w, e = base.bbox
    outside = [p for p in r["path"] if not (so <= p["lat"] <= n and w <= p["lon"] <= e)]
    out_nm = max((max(so - p["lat"], p["lat"] - n, 0) * 60 + max(w - p["lon"], p["lon"] - e, 0) * 60
                  * math.cos(math.radians(p["lat"])) for p in outside), default=0.0)
    legs = [{"to": l["to"], "pos": l["point_of_sail"], "router_tacks": l["tacks"],
             "tacks": (per_leg[i] if i < len(per_leg) else (0, 0))[0],
             "gybes": (per_leg[i] if i < len(per_leg) else (0, 0))[1],
             "min": l["leg_minutes"], "sailed": l["sailed_nm"], "direct": l["direct_nm"],
             "reached": l["reached"]}
            for i, l in enumerate(r["legs"])]
    return {
        "case": label, "tws": tws, "twd": round(twd) % 360,
        "tacks": tk, "gybes": gy, "changes_at_marks": at_marks, "router_board_changes": r["total_tacks"], "peels": r["total_peels"],
        "maneuver_s": (tk + gy) * OPT.TACK_COST_S + r["total_peels"] * OPT.PEEL_COST_S,
        "complete": bool(r["complete"]) and all(l["reached"] for l in r["legs"]),
        "rounding_ok": all(ok for _n, ok, _d in rounds),
        "roundings": [f"{n}: {d}" for n, ok, d in rounds],
        "min_clear_nm": None if c_all is None else round(c_all, 2),
        "min_clear_away_nm": None if c_away is None else round(c_away, 2),
        "land_hits": hits,
        # the mask only covers the course box (+0.5 deg); outside it everything is open water,
        # land included. Path vertices outside, and how far out the furthest one is.
        "outside_box_pts": len(outside), "outside_box_nm": round(out_nm, 1),
        "hours": r["total_hours"], "sailed_nm": r["total_sailed_nm"], "direct_nm": r["total_direct_nm"],
        "runtime_s": round(rt, 1), "timed_out": r["timed_out"],
        "warnings": [w for w in r["warnings"] if "crosses itself" in w or "INCOMPLETE" in w
                     or "out of time" in w or "Shoreline" in w],
        "legs": legs,
    }


ORDER = ["cove_island", "shore", "wl", "leg12"]
CASES = ["beat", "close", "beam", "run", "beat-20", "beat-10", "beat+10", "beat+20",
         "run-20", "run-10", "run+10", "run+20"]


def table(rows):
    """The results as one markdown table (course, wind speed, case order)."""
    def c(v):
        return "–" if v is None else f"{v:.2f}"
    rows = sorted(rows, key=lambda r: (ORDER.index(r["course"]) if r["course"] in ORDER else 9,
                                       r["tws"], CASES.index(r["case"]) if r["case"] in CASES else 99))
    out = ["| course | kn | wind from | case | tacks | gybes | at marks | peels | manoeuvre s "
           "| complete | roundings ok | min clear nm (away from marks) | out of box nm | route h "
           "| sailed/direct nm | runtime s |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            out.append(f"| {r['course']} | {r['tws']:g} | {r['twd']} | {r['case']} | error: {r['error']} |")
            continue
        to = " (timed out)" if r["timed_out"] else ""
        out.append(
            f"| {r['course']} | {r['tws']:g} | {r['twd']:03d} | {r['case']} | {r['tacks']} | {r['gybes']} "
            f"| {r['changes_at_marks']} | {r['peels']} | {r['maneuver_s']:.0f} "
            f"| {'yes' if r['complete'] else 'NO'}{to} | {'yes' if r['rounding_ok'] else 'NO'} "
            f"| {c(r['min_clear_nm'])} ({c(r['min_clear_away_nm'])}) | {r['outside_box_nm'] or 0} "
            f"| {r['hours']} | {r['sailed_nm']}/{r['direct_nm']} | {r['runtime_s']} |")
    return "\n".join(out)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--table":      # render saved result files
        rows = [r for f in sys.argv[2:] for r in json.load(open(f))["rows"]]
        print(table(rows))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--courses", default="cove_island,shore,wl,leg12")
    ap.add_argument("--tws", default="5,8,12,18")
    ap.add_argument("--rel", default=None, help="wind dirs relative to the course axis "
                    f"(default {REL_DEFAULT}; leg12 {REL['leg12']})")
    ap.add_argument("--boat", default=os.path.join(REPO, "vps/lab/boats/sr33.json"))
    ap.add_argument("--source", default="enc", help="chart source (the live Lab uses enc)")
    ap.add_argument("--resolution", default="auto")
    ap.add_argument("--wind-everywhere", action="store_true",
                    help="wind outside the course box too (default: only inside, like the GRIBs)")
    ap.add_argument("--out", default=None, help="write the JSON results here")
    a = ap.parse_args()

    boat = json.load(open(a.boat))
    boat_in = routerun.boat_inputs(boat)
    courses = load_courses()
    rows, meta = [], {"boat": a.boat, "helm_factor": boat_in["helm_factor"],
                      "polar_cells": len(boat_in["polar_adjustments"] or []),
                      "tack_cost_s": OPT.TACK_COST_S, "peel_cost_s": OPT.PEEL_COST_S,
                      "resolution": a.resolution, "wind_everywhere": a.wind_everywhere,
                      "courses": {}}
    for key in a.courses.split(","):
        definition, cid = courses[key]
        marks, _sk, _ = race_def.course_to_marks(definition, cid)
        axis_to = marks[1] if key == "wl" else marks[-1]       # W/L: start -> windward
        axis = OPT._bearing(marks[0][2], marks[0][3], axis_to[2], axis_to[3])
        t0 = time.time()
        bbox = OPT.course_bbox(definition, cid)
        fields = build_leg_fields(definition, cid, bbox, source=a.source,
                                  safety_depth=routerun.boats.safety_depth_m(boat))
        base = fields[0]
        meta["courses"][key] = {"axis": round(axis), "obstacle_build_s": round(time.time() - t0, 1),
                                "land": base.land, "source": base.source,
                                "layers": dict(base.layers)}
        print(f"== {key}: axis {axis:.0f}, obstacles {base.source} land={base.land} "
              f"({time.time() - t0:.1f}s)", flush=True)
        for tws in [float(x) for x in a.tws.split(",")]:
            for rel in [int(x) for x in (a.rel or REL.get(key, REL_DEFAULT)).split(",")]:
                label = rel_label(rel)
                row = run_case(definition, cid, tws, axis + rel, label, fields, boat_in, a.resolution,
                               a.wind_everywhere)
                row["course"] = key
                rows.append(row)
                print(json.dumps({k: v for k, v in row.items() if k != "legs"}), flush=True)
    print(table(rows))
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"meta": meta, "rows": rows}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
