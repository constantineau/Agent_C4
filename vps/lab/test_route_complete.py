"""#31 — a course whose leg never reaches its mark must NOT come back as a finished route.

Before the fix, `route_leg` returned its nearest node on a miss, `optimize_course` started the next
leg AT the mark anyway (a free teleport, possibly across land) and reported available/not degraded —
a fully blocked course came back available=True, degraded=False, 0 minutes. Locked here
deterministically (constant wind, stub obstacles, no network):
  1) route_leg flags `reached` (True on a clear leg, False + short_nm on a blocked one);
  2) a fully blocked course comes back degraded + incomplete, naming the leg and how far short,
     never routing the legs after it;
  3) a time-budget truncation is flagged the same way (reason "time budget"), with no 0-min legs;
  4) a clear course is complete and not degraded (no false alarms);
  5) incomplete_reason / playbook._truncated read both the flag and the 3 nm finish rule;
  6) playbook: an incomplete gear-loss route is retried, then dropped by name (never a play);
  7) judge: an oracle that didn't finish makes the tactics half unavailable, by name;
  8) synthesis._downsample keeps mark vertices and never shortcuts across an obstacle.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break

from app import optimizer as OPT       # noqa: E402
from app import playbook as PB         # noqa: E402
from app import polars as POL          # noqa: E402
from app import synthesis as SYN       # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class WF:
    loaded = True

    def __init__(self, tws=12.0, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


class Wall:
    """Every step is blocked — the 'all headings blocked' miss."""

    def crosses(self, *a):
        return True

    def blocked(self, *a):
        return False

    def summary(self):
        return {"active": True}


class Box:
    """A rectangular island (south, north, west, east)."""

    def __init__(self, s, n, w, e):
        self.b = (s, n, w, e)

    def _in(self, la, lo):
        s, n, w, e = self.b
        return s <= la <= n and w <= lo <= e

    def crosses(self, la1, lo1, la2, lo2):
        return any(self._in(la1 + (la2 - la1) * k / 40.0, lo1 + (lo2 - lo1) * k / 40.0)
                   for k in range(41))

    def blocked(self, la, lo):
        return self._in(la, lo)

    def summary(self):
        return {"active": True}


THREE = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                      "marks": [{"name": "M1", "type": "buoy", "lat": 44.0, "lon": -81.7,
                                 "rounding": "none"}],
                      "finish": {"points": [{"lat": 44.2, "lon": -81.7}]}}]}
P = POL.polars_stw()

# ---- 1) route_leg's reached flag -----------------------------------------------------------------
print("1) route_leg flags whether it reached the mark")
leg = OPT.route_leg(WF(), P, 44.0, -82.0, 0.0, 44.0, -81.7)
check("clear leg: reached=True, short_nm=0", leg["reached"] is True and leg["short_nm"] == 0.0)
leg = OPT.route_leg(WF(), P, 44.0, -82.0, 0.0, 44.0, -81.7, obstacles=Wall())
print(f"     blocked leg: reached={leg['reached']} short_nm={leg['short_nm']}")
check("blocked leg: reached=False", leg["reached"] is False)
check("blocked leg: short_nm ~ the whole leg (~13 nm)", 12.0 < leg["short_nm"] < 14.0)

# ---- 2) the Codex repro: fully blocked course ----------------------------------------------------
print("2) fully blocked course (was available=True, degraded=False, 0 min)")
r = OPT.optimize_course(THREE, "x", 0, WF(), obstacles=Wall(), emit_exploration=False,
                        resolution="fast", time_budget_s=20)
print(f"     available={r.get('available')} degraded={r.get('degraded')} "
      f"complete={r.get('complete')} incomplete={r.get('incomplete')}")
check("degraded", r.get("degraded") is True)
check("complete=False", r.get("complete") is False)
inc = r.get("incomplete") or {}
check("names the leg that stalled (M1) and how far short", inc.get("to") == "M1"
      and (inc.get("short_nm") or 0) > 12)
check("reason: blocked (not the time budget)", "heading" in (inc.get("reason") or ""))
check("lists the legs it never routed (Finish)", inc.get("legs_not_routed") == ["Finish"])
check("course stops: only the stalled leg is in legs", len(r.get("legs") or []) == 1
      and r["legs"][0]["reached"] is False)
check("no teleport: the path never touches M1 or the finish",
      all(OPT._hav_nm(p["lat"], p["lon"], 44.0, -81.7) > 1 and
          OPT._hav_nm(p["lat"], p["lon"], 44.2, -81.7) > 1 for p in r["path"]))
check("leading warning says INCOMPLETE and names M1",
      r["warnings"] and "INCOMPLETE" in r["warnings"][0] and "M1" in r["warnings"][0])

# ---- 3) time-budget truncation -------------------------------------------------------------------
print("3) time-budget truncation")
LONG = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                     "marks": [{"name": "Far", "type": "buoy", "lat": 46.0, "lon": -82.0,
                                "rounding": "none"},
                               {"name": "Back", "type": "buoy", "lat": 44.5, "lon": -82.5,
                                "rounding": "none"}],
                     "finish": {"points": [{"lat": 44.0, "lon": -82.0}]}}]}
r = OPT.optimize_course(LONG, "x", 0, WF(tws=4.0, twd=0.0), avoid=False, emit_exploration=False,
                        resolution="fine", time_budget_s=0.3)
inc = r.get("incomplete") or {}
print(f"     timed_out={r.get('timed_out')} degraded={r.get('degraded')} incomplete={inc}")
check("timed out and flagged incomplete + degraded",
      r.get("timed_out") and r.get("degraded") is True and r.get("complete") is False)
check("reason is the time budget", inc.get("reason") == "time budget")
check("no 0-minute legs chained after the deadline",
      all((l.get("leg_minutes") or 0) > 0 for l in r["legs"]) and len(r["legs"]) <= 3
      and r["legs"][-1]["reached"] is False)
check("incomplete_reason sees it", OPT.incomplete_reason(r, (44.0, -82.0)) is not None)

# ---- 4) a clear course is complete (no false alarm) ---------------------------------------------
print("4) clear course")
r = OPT.optimize_course(THREE, "x", 0, WF(), avoid=False, emit_exploration=False,
                        resolution="fast", time_budget_s=30)
check("complete, not degraded, no incomplete",
      r.get("complete") is True and r.get("incomplete") is None and r.get("degraded") is False)
check("incomplete_reason: None", OPT.incomplete_reason(r, (44.2, -81.7)) is None)
le = r.get("leg_end_idx") or []
check("leg_end_idx: one per leg, each at its mark, last = path end",
      len(le) == 2 and le[-1] == len(r["path"]) - 1
      and OPT._hav_nm(r["path"][le[0]]["lat"], r["path"][le[0]]["lon"], 44.0, -81.7) < 0.05)
check("playbook._truncated: None", PB._truncated(r, (44.2, -81.7)) is None)
clear = r

# ---- 5) the 3 nm finish rule for results without the flag ----------------------------------------
print("5) incomplete_reason / _truncated")
short = {"available": True, "path": [{"lat": 44.0, "lon": -82.0}, {"lat": 44.0, "lon": -81.8}]}
check("path ending >3 nm from the finish → reason", "short of the finish" in
      (PB._truncated(short, (44.2, -81.7)) or ""))
check("unavailable → reason", PB._truncated({"available": False, "note": "x"}, None) == "x")
check("no finish point + no flag → None (can't judge)", PB._truncated(short, None) is None)

# ---- 6) playbook gear-loss: incomplete → retried then dropped -------------------------------------
print("6) playbook gear-loss play never built from an incomplete route")
_orig = PB.optimizer.optimize_course
calls = []


def _fake(definition, course_id, start_epoch, wf, **kw):
    calls.append(kw)
    if kw.get("exclude_sails"):
        return {"available": True, "complete": False, "degraded": True, "total_hours": 1.0,
                "incomplete": {"to": "Finish", "short_nm": 40.0, "reason": "time budget"},
                "path": [{"lat": 44.0, "lon": -82.0, "t": 0}], "legs": [], "sail_plan": []}
    return dict(clear)


try:
    PB.optimizer.optimize_course = _fake
    cons = dict(clear)
    cons["sail_plan"] = [{"sail": "A3"}]
    marks = [(1, "Start", 44.0, -82.0), (2, "M1", 44.0, -81.7), (3, "Finish", 44.2, -81.7)]
    log = []
    routes, robust = PB._internal_fan(THREE, "x", 0, WF(), cons, None, None, marks,
                                      per_budget_s=10, log=log)
finally:
    PB.optimizer.optimize_course = _orig
gl = [kw for kw in calls if kw.get("exclude_sails")]
check("gear-loss retried once at double budget", len(gl) == 2
      and gl[1]["time_budget_s"] == 2 * gl[0]["time_budget_s"])
check("no gear-loss play from the incomplete route",
      not any(x["kind"] == "gear_loss" for x in routes))
check("the drop is logged by name", any("gear-loss A3" in m and "skipped" in m for m in log))

# ---- 7) judge: an oracle that didn't finish → tactics unavailable by name -------------------------
print("7) judge tactics half refuses a partial oracle")
from app import judge as JD   # noqa: E402
_o_wf, _o_opt = JD._oracle_windfield, JD.optimizer.optimize_course
try:
    JD._oracle_windfield = lambda *a, **k: (WF(), "live")
    JD.optimizer.optimize_course = lambda *a, **k: {
        "available": True, "complete": False, "degraded": True, "total_hours": 2.0,
        "incomplete": {"to": "M1", "short_nm": 9.5, "reason": "time budget"},
        "path": [{"lat": 44.0, "lon": -82.0, "t": 0}, {"lat": 44.0, "lon": -81.9, "t": 3600}]}
    tac = JD._tactics(THREE, "r", {"course_id": "x", "start_epoch": 1.0e9}, None, lambda *_: None)
    print(f"     {tac.get('note')}")
    check("tactics unavailable", tac.get("available") is False)
    check("note names the leg and the shortfall", "M1" in (tac.get("note") or "")
          and "9.5" in (tac.get("note") or ""))
    check("no regret scored", "regret" not in tac)
    check("the wind field still rides along for the own-speed scoring", tac.get("wf") is not None)
    # the plain 3 nm rule — an oracle without the flag that ends short of the finish
    JD.optimizer.optimize_course = lambda *a, **k: {
        "available": True, "total_hours": 2.0,
        "path": [{"lat": 44.0, "lon": -82.0, "t": 0}, {"lat": 44.0, "lon": -81.8, "t": 3600}]}
    tac = JD._tactics(THREE, "r", {"course_id": "x", "start_epoch": 1.0e9}, None, lambda *_: None)
    check("unflagged oracle ending short of the finish also refused",
          tac.get("available") is False and "short of the finish" in (tac.get("note") or ""))
finally:
    JD._oracle_windfield, JD.optimizer.optimize_course = _o_wf, _o_opt

print("7b) incomplete-oracle debrief scores own speed through the water, like a complete one")
from app import current as CURMOD, wave as WAVEMOD   # noqa: E402


class FollowCur:
    """A 2 kn current setting due north — the boat's own direction."""
    loaded = True

    def current_at(self, lat, lon, t):
        return (0.0, 2.0)

    def status(self):
        return {"loaded": True}


class Sea:
    loaded = True

    def wave_at(self, lat, lon, t):
        return 1.5

    def status(self):
        return {"loaded": True}


NS = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                   "finish": {"points": [{"lat": 44.1333, "lon": -82.0}]}}]}
T0 = 1.75e9
# 6 kn through the water due north + the 2 kn following current = 8 kn over the ground, 60 fixes
fixes = [{"lat": 44.0 + (8.0 / 60.0) * (i / 60.0) / 1.0, "lon": -82.0, "t": T0 + 60 * i,
          "sog": 8.0, "cog": 0.0} for i in range(61)]
gpx = {"fixes": fixes, "source": "gpx", "boat": "C4"}
beam = WF(tws=12.0, twd=270.0)
full_path = [{"lat": 44.0, "lon": -82.0, "t": T0}, {"lat": 44.1333, "lon": -82.0, "t": T0 + 3600}]
ORACLES = {
    "complete": {"available": True, "complete": True, "total_hours": 1.0, "total_sailed_nm": 8.0,
                 "path": full_path, "legs": []},
    "incomplete": {"available": True, "complete": False, "degraded": True, "total_hours": 0.5,
                   "incomplete": {"to": "Finish", "short_nm": 4.0, "reason": "time budget"},
                   "path": full_path[:1] + [{"lat": 44.0667, "lon": -82.0, "t": T0 + 1800}]},
    "failed": {"available": False, "note": "no polars loaded"},
}
_saved = (JD._oracle_windfield, JD.optimizer.optimize_course, CURMOD.build_currentfield,
          WAVEMOD.build_wavefield)
scores = {}
try:
    JD._oracle_windfield = lambda *a, **k: (beam, "archive")
    CURMOD.build_currentfield = lambda *a, **k: FollowCur()
    WAVEMOD.build_wavefield = lambda *a, **k: Sea()
    for kind, orc in ORACLES.items():
        JD.optimizer.optimize_course = lambda *a, _o=orc, **k: _o
        tac = JD._tactics(NS, "r", {"course_id": "x", "start_epoch": T0}, None, lambda *_: None)
        check(f"{kind}: tactics {'available' if kind == 'complete' else 'refused'}",
              bool(tac.get("available")) == (kind == "complete"))
        check(f"{kind}: current + sea-state fields ride along", tac.get("cur") is not None
              and tac.get("wave") is not None and tac.get("wf") is not None)
        # exactly the judge's call
        scores[kind] = JD._score_actual_track(gpx, tac.get("oracle") or {}, tac.get("marks") or [],
                                              tac.get("start_epoch"), tac.get("wf"), tac.get("cur"),
                                              tac.get("wave"))
    raw = JD._score_actual_track(gpx, {}, [], T0, beam, None, None)    # the pre-fix shape

    class _NoWind(WF):
        loaded = False
    JD._oracle_windfield = lambda *a, **k: (_NoWind(), "live")
    tac = JD._tactics(NS, "r", {"course_id": "x", "start_epoch": T0}, None, lambda *_: None)
    check("no wind field: refused, but current + sea state still ride along (measured-wind tracks)",
          tac.get("available") is False and tac.get("cur") is not None and tac.get("wave") is not None)
finally:
    (JD._oracle_windfield, JD.optimizer.optimize_course, CURMOD.build_currentfield,
     WAVEMOD.build_wavefield) = _saved
target = OPT._polar_speed(P, 12.0, 90.0)
want = round(100 * 6.0 / target)
c, i_, f_ = scores["complete"], scores["incomplete"], scores["failed"]
print(f"     polar target {target:.2f} kn → expect ~{want}% through water; complete "
      f"{c.get('polar_pct')}% / incomplete {i_.get('polar_pct')}% / oracle-failed "
      f"{f_.get('polar_pct')}% / no current {raw.get('polar_pct')}%")
check("complete: polar % is the through-water 6 kn", abs((c.get("polar_pct") or 0) - want) <= 2
      and c.get("current_corrected") and c.get("wave_corrected"))
for kind in ("incomplete", "failed"):
    sc = scores[kind]
    check(f"{kind}: same polar % / helm % as the complete debrief",
          sc.get("polar_pct") == c.get("polar_pct") and sc.get("helm_pct") == c.get("helm_pct"))
    check(f"{kind}: current- and wave-corrected", sc.get("current_corrected") and sc.get("wave_corrected"))
    check(f"{kind}: learning bins identical to the complete path",
          sc.get("perf_bins") and sc.get("perf_bins") == c.get("perf_bins"))
check("(control) without the current the same track reads ~2 kn fast",
      (raw.get("polar_pct") or 0) >= (c.get("polar_pct") or 0) + 20)

# ---- 8) synthesis._downsample: marks kept, never across an obstacle ------------------------------
print("8) synthesis._downsample")
island = Box(44.05, 44.15, -82.03, -81.97)
leg = OPT.route_leg(WF(tws=12.0, twd=90.0), P, 44.0, -82.0, 0.0, 44.2, -82.0, obstacles=island,
                    dt_cap=0.05)
path = leg["path"]
mid_mark_t = path[len(path) // 3]["t"]
# pad to >200 points by densifying each segment (the shape is unchanged)
dense = []
for a, b in zip(path, path[1:]):
    for k in range(12):
        f = k / 12.0
        dense.append({"lat": a["lat"] + (b["lat"] - a["lat"]) * f,
                      "lon": a["lon"] + (b["lon"] - a["lon"]) * f,
                      "t": a["t"] + (b["t"] - a["t"]) * f})
dense.append(path[-1])
mark_i = min(range(len(dense)), key=lambda i: abs(dense[i]["t"] - mid_mark_t))
dense[mark_i]["t"] = mid_mark_t
legs = [{"eta_epoch": mid_mark_t}, {"eta_epoch": path[-1]["t"]}]
print(f"     {len(dense)} points around the island")
check("dense path itself is clear", not any(island.crosses(a["lat"], a["lon"], b["lat"], b["lon"])
                                            for a, b in zip(dense, dense[1:])))
for budget in (200, 20, 6):
    out = SYN._downsample(dense, budget, legs=legs, obstacles=island)
    cut = sum(island.crosses(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(out, out[1:]))
    check(f"budget {budget}: {len(out)} pts, no shortcut across the island", cut == 0)
    check(f"budget {budget}: endpoints + mark vertex kept",
          out[0] is dense[0] and out[-1] is dense[-1] and any(p is dense[mark_i] for p in out))
thin = SYN._thin_route({"path": dense, "legs": legs, "leg_end_idx": [mark_i, len(dense) - 1]},
                       island, max_pts=20)
check("_thin_route remaps leg_end_idx onto the thinned path",
      thin["path"][thin["leg_end_idx"][0]] is dense[mark_i]
      and thin["leg_end_idx"][-1] == len(thin["path"]) - 1)
naive = SYN._downsample(dense, 6, legs=legs)
check("(control) without the obstacle check a coarse thin DOES cut the island",
      any(island.crosses(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(naive, naive[1:])))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
