"""#303 — the Lab routes, plays and debriefs every boat on ITS OWN polar, never sr33's.

Pinned with a second boat (vps/lab/boats/bravo.json: an ORC-cert envelope, no per-sail curves, no
crossover table) and a synthetic boat with no polar at all:
  * the resolver: sr33 names its own four files; Bravo's polar is its inline cert; no polar → None;
  * an edited profile saved before #303 (no `polar_files`) still gets its seed's files;
  * a route, a playbook and a route run for a boat with no polar refuse ("No polar for this boat.");
  * Bravo's route, sail what-if, sail plays, bundle boat model, debrief oracle kwargs, track score,
    Polar tab and Deploy polar block are all Bravo's — and NOT ONE sr33 polar file is opened;
  * no Lab code calls the optimizer / sail plan / polar helpers without a polar (a static guard).
"""
import ast
import asyncio
import builtins
import io
import json
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ.pop("ANTHROPIC_API_KEY", None)
HERE = os.path.dirname(os.path.abspath(__file__))

from shared import boat_polar as BP  # noqa: E402
from app import boats, judge, optimizer, orinassets, playbook, polars, routerun  # noqa: E402
from app import sailplan, synthesis  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


SR33 = boats.get_boat("sr33")
BRAVO = boats.get_boat("bravo")
SR33_FILES = set((SR33.get("polar_files") or {}).values())
NOPOLAR = {"boat_id": "nopolar", "name": "No Polar", "draft_m": 1.5}

print("1) the resolver: each boat's own polar")
check("sr33's profile names its own four polar files",
      SR33_FILES == {"polars_sr33.sql", "sr33_sail_polars.json", "sr33_crossovers.json",
                     "sr33_speed_guide.md"})
P33 = boats.polar_for(SR33)
check("sr33 → its files: envelope, per-sail curves, crossovers, speed guide",
      P33 and P33["source"] == "files" and P33["envelope"] and P33["sail_polars"]
      and P33["crossovers"] and P33["speed_guide"] and P33["boat_id"] == "sr33")
PB = boats.polar_for(BRAVO)
check("Bravo → its ORC cert envelope, no sail curves or crossovers",
      PB and PB["source"] == "cert" and PB["boat_id"] == "bravo" and not PB["sail_polars"]
      and PB["crossovers"] is None
      and len(PB["envelope"]) == len(BRAVO["cert_polar"]))
check("Bravo's envelope is its cert, not sr33's",
      polars.polars_stw(PB) == [tuple(map(float, r)) for r in BRAVO["cert_polar"]]
      and polars.polars_stw(PB) != polars.polars_stw(P33))
check("a boat with no polar → None", boats.polar_for(NOPOLAR) is None and boats.polar_for(None) is None)
check("a profile naming a file that isn't there → None, not a default",
      BP.resolve(dict(NOPOLAR, polar_files={"envelope": "polars_nopolar.sql"})) is None)
check("a polar file name with a path is refused",
      BP.files_errors({"envelope": "../../etc/passwd"}) and BP.resolve(
          dict(NOPOLAR, polar_files={"envelope": "../seed/polars_sr33.sql"})) is None)
_bad = tempfile.mkdtemp()
for _n, _t in (("bad_env.json", "42"), ("bad_sails.json", '{"sails": ["J1"]}'),
               ("ok_env.json", "[[10, 90, 7.0]]")):
    with open(os.path.join(_bad, _n), "w") as _f:
        _f.write(_t)
check("malformed JSON polar files → no polar / no sail curves, never a crash",
      BP.resolve(dict(NOPOLAR, polar_files={"envelope": "bad_env.json"}), dirs=[_bad]) is None
      and BP.resolve(dict(NOPOLAR, polar_files={"envelope": "ok_env.json",
                                                "sail_polars": "bad_sails.json"}),
                     dirs=[_bad])["sail_polars"] == {})
check("the carried block checks clean and is versioned", BP.check(P33, "sr33") == []
      and BP.check(PB, "bravo") == [] and BP.check(PB, "sr33") != [])
check("a tampered block fails its version", BP.check(dict(PB, label="x"), "bravo") != [])

print("2) an edited profile saved before #303 keeps its seed's polar files")
old_edit = {k: v for k, v in SR33.items() if k != "polar_files"}
old_edit["polars_file"] = ""
old_edit["helm_factor"] = 0.93
boats.save_boat(dict(old_edit))
got = boats.get_boat("sr33")
check("the edited copy wins (helm 0.93) and inherits the seed's polar_files",
      got["helm_factor"] == 0.93 and got.get("polar_files") == SR33["polar_files"])
check("…so it routes on sr33's own polar", boats.polar_for(got)["version"] == P33["version"])
os.remove(os.path.join(boats.EDITED_DIR, "sr33.json"))


# ---- a deterministic course + wind --------------------------------------------------------------
class Const:
    def __init__(self, tws=12.0, twd=200.0):
        self.tws, self.twd = tws, twd
        self.loaded = True

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def status(self):
        return {"loaded": True}

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 0.7}


T0 = 1_793_534_400
COURSE = {"race_id": "t", "name": "Test race", "courses": [{
    "id": "x", "start": {"lat": 43.9, "lon": -82.4},
    "marks": [{"name": "Gate", "type": "buoy", "lat": 44.2, "lon": -82.3, "rounding": "port"}],
    "finish": {"points": [{"lat": 44.0, "lon": -82.6}]}}]}

print("3) no polar → plain refusal, everywhere")
r = optimizer.optimize_course(COURSE, "x", T0, Const(), avoid=False, time_budget_s=20)
check("optimize_course with no polar refuses by name", r.get("available") is False
      and r.get("note") == BP.NO_POLAR)
called = []
rr = routerun.run_course(COURSE, "x", T0, ["gfs"], boat=NOPOLAR, briefing=False, grids=False,
                         on_progress=called.append)
check("a route run for a boat with no polar refuses before any weather",
      rr.get("available") is False and rr.get("note") == BP.NO_POLAR and not called)
pbk = playbook.build_playbook(COURSE, "x", T0, ["gfs"], boat_polar=None)
check("a playbook with no polar refuses", pbk.get("available") is False
      and pbk.get("note") == BP.NO_POLAR)
check("the route run's boat inputs carry no polar for that boat (nothing to fall back on)",
      routerun.boat_inputs(NOPOLAR)["boat_polar"] is None
      and "polar" not in routerun.boat_inputs(NOPOLAR))

print("4) Bravo end to end — and no sr33 polar file is ever opened")
opened = []
_open, _io_open = builtins.open, io.open


def spy_open(file, *a, **k):
    opened.append(os.path.basename(str(file)))
    return _open(file, *a, **k)


builtins.open = spy_open
io.open = spy_open
try:
    PB2 = boats.polar_for(boats.get_boat("bravo"))
    rb = optimizer.optimize_course(COURSE, "x", T0, Const(), avoid=False, time_budget_s=30,
                                   boat_polar=PB2, jib_crossovers=BRAVO.get("jib_crossovers"),
                                   sail_config=boats.sail_config(BRAVO))
    ins = routerun.boat_inputs(BRAVO)
    leg_sails = [lg.get("sail") for lg in rb.get("legs") or []]
    bm = synthesis._boat_model(BRAVO, PB2)
    plays = synthesis._sail_guidance_plays({"legs": [{"sail": "J1", "wind": {"tws": 12},
                                                      "point_of_sail": "beat"}]}, [], None, PB2)
    kw, _from = judge._oracle_boat_kwargs(BRAVO, {})
    track = {"fixes": [{"t": T0 + i * 10, "lat": 43.9 + i * 1e-4, "lon": -82.4, "sog": 6.0,
                        "cog": 0.0} for i in range(50)]}
    scored = judge._score_actual_track(track, {}, [], T0, None, None, None, wave_coeffs=None,
                                       boat=BRAVO)
    boats.set_active("bravo")
    from app import main
    grid = asyncio.run(main.polars_grid())
    xo_view = asyncio.run(main.crossovers())
    _bc = orinassets.build_chart
    orinassets.build_chart = lambda *a, **k: (None, None)
    pay, probs = orinassets.build(COURSE, "x", bundle={"boat_model": {"boat_id": "bravo"}})
    orinassets.build_chart = _bc
finally:
    builtins.open, io.open = _open, _io_open

check("Bravo's route is computed (on its cert polar)", rb.get("available", True) and rb.get("legs"))
check("…and makes no sail calls from sr33's crossover table", all(s is None for s in leg_sails))
check("the route run hands the optimizer Bravo's polar", ins["boat_polar"]["boat_id"] == "bravo"
      and ins["boat_polar"]["source"] == "cert")
check("bundle boat model: Bravo, its polar named, no sr33 zones",
      bm["boat_id"] == "bravo" and bm["polar"]["source"] == "cert" and not bm["crossovers"]
      and not bm["overlaps"])
check("sail-guidance plays: none without Bravo's own crossover table", plays == [])
check("debrief oracle routes on Bravo's polar", kw["boat_polar"]["boat_id"] == "bravo")
check("the track is scored (on Bravo's polar)", scored.get("available", True) is not False
      or scored.get("note") != BP.NO_POLAR)
check("Polar tab shows Bravo's cert", grid["n_points"] == len(BRAVO["cert_polar"])
      and grid["polar"]["boat_id"] == "bravo")
check("crossover view: none for Bravo, said plainly", not xo_view["crossovers"]
      and xo_view.get("note") and xo_view["boat_id"] == "bravo")
check("Deploy carries Bravo's own polar block", pay["polar"]["boat_id"] == "bravo"
      and BP.check(pay["polar"], "bravo") == [] and not [p for p in probs if "polar" in p.lower()])
leaked = sorted(set(opened) & SR33_FILES)
check(f"no sr33 polar file opened for Bravo (opened: {leaked or 'none'})", not leaked)

orinassets.build_chart = lambda *a, **k: (None, None)
pay3, probs3 = orinassets.build(COURSE, "x", bundle={"boat_model": {"boat_id": "bravo"},
                                                     "routing": {"polar": {"version": "0" * 12}}})
pay4, probs4 = orinassets.build(COURSE, "x", bundle={"boat_model": {"boat_id": "bravo"},
                                                     "routing": {"polar": BP.summary(PB)}})
orinassets.build_chart = _bc
check("Deploy: a playbook routed on another polar → no polar sent, step says rebuild",
      pay3["polar"] is None and orinassets.POLAR_CHANGED in probs3)
check("Deploy: the playbook's own polar → sent", pay4["polar"]["version"] == PB["version"]
      and orinassets.POLAR_CHANGED not in probs4)

print("5) a boat with no polar: Polar tab, Deploy and track scoring say so")
boats.save_boat(dict(NOPOLAR))
boats.set_active("nopolar")
g2 = asyncio.run(main.polars_grid())
check("Polar tab: empty with the plain note", g2["n_points"] == 0 and g2["note"] == BP.NO_POLAR)
orinassets.build_chart = lambda *a, **k: (None, None)
pay2, probs2 = orinassets.build(COURSE, "x", bundle={"boat_model": {"boat_id": "nopolar"}})
orinassets.build_chart = _bc
check("Deploy: no polar block, and the step says why", pay2["polar"] is None
      and orinassets.NO_POLAR in probs2)
s2 = judge._score_actual_track(track, {}, [], T0, boat=NOPOLAR, wave_coeffs=None)
check("track scoring refuses by name", s2.get("available") is False and s2["note"] == BP.NO_POLAR)

print("5b) a playbook job keeps the boat it was asked for, with its polar")
boats.set_active("sr33")
seen = {}
_bp = synthesis.pb.build_playbook


def fake_build(*a, **k):
    seen["polar"] = k.get("boat_polar")
    return {"available": False, "note": "stop"}


synthesis.pb.build_playbook = fake_build
try:
    synthesis.synthesize(COURSE, "x", T0, ["gfs"], boat=BRAVO, boat_polar=PB)
finally:
    synthesis.pb.build_playbook = _bp
check("routes on the snapshot boat's polar (Bravo), not the now-active sr33",
      (seen.get("polar") or {}).get("boat_id") == "bravo")

print("6) static guard: no Lab code calls the optimizer, sail plan or polar helpers polar-less")
APP = os.path.join(os.path.dirname(boats.__file__))
NEED = {"optimize_course": ("boat_polar", "polar"), "route_leg": ("xo",),
        "optimal_sail": ("xo",), "crossovers_specialized": ("xo",),
        "overlaps_specialized": ("xo",)}
bad = []
for fn in sorted(os.listdir(APP)):
    if not fn.endswith(".py"):
        continue
    tree = ast.parse(open(os.path.join(APP, fn)).read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        kws = {k.arg for k in node.keywords}
        if name in NEED and None not in kws and not (kws & set(NEED[name])):
            bad.append(f"{fn}:{node.lineno} {name}")
        if name in ("polars_stw", "sail_polars") and not node.args and not node.keywords:
            bad.append(f"{fn}:{node.lineno} {name}()")
check(f"every call passes the boat's polar ({', '.join(bad) or 'all do'})", not bad)
# …and never to a function that doesn't take it (a `boat_polar=` to build_leg_fields raised a
# TypeError that a broad except swallowed: the rejoin table lost its obstacles — Codex on #319)
takes, passes = set(), []
for fn in sorted(os.listdir(APP)):
    if fn.endswith(".py"):
        for node in ast.walk(ast.parse(open(os.path.join(APP, fn)).read())):
            if isinstance(node, ast.FunctionDef) and any(
                    a.arg == "boat_polar" for a in node.args.args + node.args.kwonlyargs):
                takes.add(node.name)
            if isinstance(node, ast.Call) and any(k.arg == "boat_polar" for k in node.keywords):
                nm = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                passes.append((fn, node.lineno, nm))
wrong = [f"{f}:{n} {nm}" for f, n, nm in passes if nm not in takes]
check(f"boat_polar only goes to functions that take it ({', '.join(wrong) or 'yes'})", not wrong)

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
