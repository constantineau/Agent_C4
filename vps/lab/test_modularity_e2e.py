"""Modularity acceptance test (#315, map #316): a second boat on a race far from Lake Huron.

Agent C4 must work for ANY boat and ANY regatta. This runs Bravo (vps/lab/boats/bravo.json, its
own ORC cert polar) on a synthetic offshore race in the English Channel — tidal water, outside the
Great Lakes and outside US charts — through the Lab's route, playbook and debrief, and through the
Orin's onboard re-route. (The Pi engine half is vps/agent/test_modularity_e2e_onboard.py.)

What it asserts, for every surface:
  * no sr33 file is ever opened (polar, per-sail polars, crossovers, speed guide, sr33.json) —
    a Python audit hook watches every open() while the run is in progress;
  * no "sr33" or "bayview" default shows up in what the run returns;
  * regional data that doesn't exist here (currents, waves, depth charts) is said plainly in the
    result, never silently dropped.

No network: the forecast is a synthetic two-model wind field, the shoreline is a fixture box for
the Isle of Wight, the current/wave builders run for real (they reject the area without fetching).

Parts that fail on today's dev are strict expected failures, each naming the ticket that fixes it.
When a fix lands, its xfail turns into an unexpected pass and this file FAILS: the fixer removes
the marker (and may adapt the "deploy" step to the real interface, keeping the assertions).

Runs in the Lab image (tools/ci/run.sh):  python3 -m pytest -q /repo/vps/lab/test_modularity_e2e.py
"""
import contextlib
import copy
import importlib
import json
import os
import re
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ["COASTLINE_CACHE"] = os.path.join(_tmp, "coastline")
os.environ["ENC_CACHE"] = os.path.join(_tmp, "enc")
os.environ["REROUTE_DIR"] = os.path.join(_tmp, "reroute")
os.environ["PLUME_DIR"] = os.path.join(_tmp, "plume")
os.environ["GRIB_ISOLATE_PARSE"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("REROUTE_BOAT", None)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BOATS = next(p for p in (os.path.join(HERE, "boats"), "/srv/boats") if os.path.isdir(p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

# ---- the sr33 guard: every open() of an sr33 file while a run is watched ------------------------
_SR33_FILE = re.compile(r"sr33", re.I)
_watching = []          # stack of lists; the innermost records


def _audit(event, args):
    if event == "open" and _watching and args:
        path = args[0]
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        if isinstance(path, str) and _SR33_FILE.search(os.path.basename(path)):
            _watching[-1].append(path)


sys.addaudithook(_audit)


@contextlib.contextmanager
def watch():
    """Record every sr33 file opened inside the block. The Lab parses its polar / sail files once
    and keeps them in module caches, so those are dropped first: data read earlier in the process
    must not slip past the guard as an in-memory copy."""
    for mod, names in (("app.polars", ("_CACHE", "_SAIL_CACHE")), ("app.sailplan", ("_CACHE",))):
        m = sys.modules.get(mod)
        for n in names:
            if m is not None and hasattr(m, n):
                setattr(m, n, None)
    seen = []
    _watching.append(seen)
    try:
        yield seen
    finally:
        _watching.pop()


def no_sr33_text(obj):
    """The words 'sr33' / 'bayview' anywhere in a result (log lines excluded)."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k != "log"}
    text = json.dumps(obj, default=str).lower()
    return [w for w in ("sr33", "bayview") if w in text]


def says_missing(obj, *topics):
    """A plain sentence in the result (not the log) saying `topic` data is missing here."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k != "log"}
    text = json.dumps(obj, default=str).lower()
    for t in topics:
        if re.search(r"\b(no|without|missing|not (loaded|available|checked))\b[^\"]{0,60}" + t, text) \
                or re.search(t + r"[^\"]{0,60}\b(missing|not (loaded|available|checked)|none)\b", text):
            return True
    return False


# ---- the race: offshore English Channel, tidal, outside US water ---------------------------------
T0 = 1_811_224_800          # 2027-05-25 06:00Z
RID = "channel-offshore-test"
RACE = {
    "schema_version": "0.1", "race_id": RID, "name": "Channel Offshore Test", "year": 2027,
    "rules_profile": {"rrs_edition": "2025-2028", "tracker_permitted": False},
    "timezone": "Europe/London", "division_starts": {"IRC 2": "2027-05-25T07:00:00"},
    "own": {"division": "IRC 2"},
    "courses": [{
        "id": "c", "name": "Channel triangle",
        "start": {"type": "point", "lat": 50.30, "lon": -1.30},
        "marks": [{"name": "South", "type": "buoy", "lat": 50.10, "lon": -1.00, "rounding": "port"}],
        "finish": {"points": [{"lat": 50.32, "lon": -0.75}]},
    }],
}
MODELS = ["gfs", "ecmwf"]
# a box of land where the Isle of Wight is (north of the course): the global shoreline, offline
WIGHT = [[(-1.58, 50.57), (-1.07, 50.57), (-1.07, 50.77), (-1.58, 50.77), (-1.58, 50.57)]]


def load_bravo():
    with open(os.path.join(BOATS, "bravo.json")) as f:
        return json.load(f)


BRAVO = load_bravo()
assert BRAVO.get("cert_polar"), "bravo.json must carry its own polar"


# ---- the Lab, with no network ----------------------------------------------------------------
from app import boats, judge, synthesis, main, racestart, fleetimport, retro  # noqa: E402
from app import wind as WIND  # noqa: E402
from app.geo import coastline  # noqa: E402
from app.wind import grib, windfield  # noqa: E402
from shared import boat_profile, race_def  # noqa: E402


def _frames(model, u0, v0):
    lat = np.arange(49.0, 51.51, 0.25)
    lon = np.arange(-3.0, 1.01, 0.25)
    out = []
    for k in range(0, 52, 3):
        u = np.full((lat.size, lon.size), u0 + 0.03 * k) + np.linspace(0, 1, lon.size)[None, :]
        v = np.full((lat.size, lon.size), v0 - 0.02 * k) + np.linspace(0, 1, lat.size)[:, None]
        out.append(grib.GribFrame(model, "det", float(T0 + k * 3600), lat, lon, u, v, True))
    return out


_SERIES = {("gfs", "det"): _frames("gfs", 4.0, -3.0), ("ecmwf", "det"): _frames("ecmwf", 3.0, -4.0)}


def fake_windfield(bbox, t_start, t_end, models=(), ensemble_members=0, on_progress=None,
                   model_weights=None, model_bias=None, fallback_tries=None):
    meta = [{"model": m, "cycle": "2027-05-25 00Z", "members": 1, "frames": 18,
             "expected_frames": 18, "cycle_fallbacks": 0, "priority": 1.0,
             "kind": "deterministic"} for m in ("gfs", "ecmwf")]
    return windfield.WindField(dict(_SERIES), meta, bbox, t_start, t_end,
                               model_weights=model_weights, model_bias=model_bias)


WIND.build_windfield = fake_windfield
synthesis.pb.build_windfield = fake_windfield          # playbook imported it by name
coastline.ensure_global = lambda cache_dir=None: {}
coastline.layers_in_bbox = lambda bbox, cache_dir=None: {"land": [WIGHT], "lakes": [], "islands": []}
judge._oracle_windfield = lambda bbox, start, t_end, models, race_id, log: (
    fake_windfield(bbox, start, t_end), "forecast")
boats.set_active("bravo")


def bravo_track():
    """Bravo's recording of the race: the rhumb line start → South → finish at ~6 kn."""
    pts = [(50.30, -1.30), (50.10, -1.00), (50.32, -0.75)]
    fixes, t = [], T0
    for (a0, o0), (a1, o1) in zip(pts, pts[1:]):
        for i in range(60):
            f = i / 60.0
            fixes.append({"t": t, "lat": a0 + (a1 - a0) * f, "lon": o0 + (o1 - o0) * f,
                          "sog": 6.0, "cog": 140.0, "stw": 6.0, "tws": 11.0, "twa": 60.0,
                          "twd": 230.0})
            t += 120
    return {"source": "boatlog", "fixes": fixes, "window": {"start_ts": T0, "end_ts": t}}


@pytest.fixture(autouse=True)
def _known_fail_line(request):
    """Name each expected failure in run.sh's summary ([KNOWN-FAIL] lines), through pytest's capture.
    A strict xfail that passes fails the file, so these lines only stand on a green run."""
    yield
    m = request.node.get_closest_marker("xfail")
    if m:
        cap = request.config.pluginmanager.getplugin("capturemanager")
        with (cap.global_and_fixture_disabled() if cap else contextlib.nullcontext()):
            print(f"\n[KNOWN-FAIL] {request.node.name}: {m.kwargs.get('reason')}")


def xfail(ticket, why):
    return pytest.mark.xfail(strict=True, reason=f"{why} ({ticket})")


# ==== 1. Lab route (the Optimize button) =========================================================
@pytest.fixture(scope="module")
def route():
    with watch() as seen:
        r = main._run_optimize(RACE, "c", T0, MODELS, 0)
    return r, seen


def test_route_runs_for_bravo(route):
    r, _ = route
    assert r.get("available") is not False, r.get("note")
    assert r.get("complete") and r.get("total_hours")
    assert (r.get("boat") or {}).get("boat_id") == "bravo"


def test_route_text_has_no_sr33_or_bayview(route):
    assert not no_sr33_text(route[0])


def test_route_says_no_depth_chart(route):
    """Outside US charts: the route says depths weren't checked (G4 — plain today)."""
    probs = [p.get("text") or "" for p in (route[0].get("safety") or {}).get("problems") or []]
    assert any(re.search(r"no depth|depths? not checked", p.lower()) for p in probs), probs


@xfail("#303", "route opens sr33's crossover file for Bravo's leg sails")
def test_route_opens_no_sr33_file(route):
    assert route[1] == []


@xfail("#308", "route outside LMHOFS says nothing about the missing current")
def test_route_says_no_current(route):
    assert says_missing(route[0], "current")


@xfail("#308", "route outside GLWU says nothing about the missing sea state")
def test_route_says_no_waves(route):
    assert says_missing(route[0], "wave", "sea state")


@xfail("#303", "a boat with no polar is routed on sr33's polar, no note")
def test_boat_without_polar_refuses_plainly():
    """A profile with no polar must not borrow sr33's: the run says there is no polar."""
    from app import routerun
    charlie = dict(copy.deepcopy(BRAVO), boat_id="charlie", name="Charlie", cert_polar=[])
    with watch() as seen:
        r = routerun.run_course(RACE, "c", T0, MODELS, boat=charlie, chart_source="gshhg")
    assert seen == []
    assert r.get("available") is False and "polar" in (r.get("note") or "").lower()


# ==== 2. Lab playbook (Gameplan synthesis, the frozen bundle) ======================================
@pytest.fixture(scope="module")
def bundle():
    kw = dict(jib_crossovers=main._active_jibs(), sail_config=main._active_sails(),
              helm_factor=boats.active_helm_factor(), polar_adjustments=main._active_adjustments(),
              wave_coeffs=main._active_wave(), use_waves=True, fan_depth="quick",
              chart=main._playbook_chart())
    with watch() as seen:
        b = synthesis.synthesize(RACE, "c", T0, MODELS, 0, time_budget_s=60, **kw)
    return b, seen


def test_playbook_builds_for_bravo(bundle):
    b, _ = bundle
    assert b.get("available", True) is not False, b.get("note")
    assert b.get("variants") and b.get("consensus", {}).get("total_hours")


def test_playbook_bundle_is_bravos(bundle):
    assert (bundle[0].get("boat_model") or {}).get("boat_id") == "bravo"


@xfail("#303", "playbook routes, sail what-ifs and the bundle's sail model read sr33's files")
def test_playbook_opens_no_sr33_file(bundle):
    assert bundle[1] == []


@xfail("#303", "bundle's boat_model carries sr33's crossovers / polar source")
def test_playbook_text_has_no_sr33_or_bayview(bundle):
    assert not no_sr33_text(bundle[0])


@xfail("#308", "playbook outside LMHOFS/GLWU says nothing about the missing current and sea state")
def test_playbook_says_no_current_or_waves(bundle):
    assert says_missing(bundle[0], "current") and says_missing(bundle[0], "wave", "sea state")


# ==== 3. Lab debrief (performance half + tactics half) ============================================
@pytest.fixture(scope="module")
def scored():
    with watch() as seen:
        s = judge.score_recording(bravo_track())
    return s, seen


def test_debrief_scores_bravo(scored):
    s = scored[0]
    assert s.get("available", True) is not False, s.get("note")
    assert s.get("perf_bins"), "the speed half scored nothing"


@xfail("#303", "debrief scores Bravo's speed against sr33's polar")
def test_debrief_opens_no_sr33_file(scored):
    assert scored[1] == []


@pytest.fixture(scope="module")
def tactics(bundle):
    pb = bundle[0]
    with watch() as seen:
        tac = judge._tactics(RACE, RID, pb, None, lambda *_: None, boat=load_bravo())
    return tac, seen


def test_debrief_tactics_run(tactics):
    assert tactics[0].get("available"), tactics[0].get("note")


@xfail("#303", "debrief's best-route oracle routes Bravo on sr33's polar")
def test_debrief_tactics_open_no_sr33_file(tactics):
    assert tactics[1] == []


# ==== 4. Orin re-route (the boat's own optimizer) ================================================
def _orin_job(reroute_boat):
    """pi/orin/reroute/job.py, (re)loaded with REROUTE_BOAT set or unset."""
    for p in (os.path.join(REPO, "pi", "orin", "reroute"), os.path.join(REPO, "pi", "orin", "plume")):
        if p not in sys.path:
            sys.path.insert(0, p)
    if reroute_boat is None:
        os.environ.pop("REROUTE_BOAT", None)
    else:
        os.environ["REROUTE_BOAT"] = reroute_boat
    try:
        import job
        return importlib.reload(job)
    finally:
        os.environ.pop("REROUTE_BOAT", None)


def _orin_request():
    b = load_bravo()
    return {"position": {"lat": 50.28, "lon": -1.25}, "definition": copy.deepcopy(RACE),
            "course_id": "c", "next_index": None, "now": T0 + 1800, "device": "test",
            "assets": {"boat": b, "boat_version": boat_profile.version(b), "received_at": T0}}


def _orin_run(job):
    with watch() as seen:
        out = job.run(_orin_request(), is_online=lambda: False)
    return out, seen


@pytest.fixture(scope="module")
def orin_bravo():
    """The Orin set up for Bravo (REROUTE_BOAT=bravo), Bravo's profile pushed by Deploy."""
    return _orin_run(_orin_job("bravo"))


def test_orin_routes_bravo(orin_bravo):
    out, _ = orin_bravo
    assert out.get("available"), out.get("note")
    assert (out.get("boat") or {}).get("boat_id") == "bravo"
    assert out.get("legs")


def test_orin_says_no_chart(orin_bravo):
    """No chart aboard → the route says the shore isn't checked (plain today)."""
    assert any("check the shore" in w.lower() for w in orin_bravo[0].get("warnings") or [])


@xfail("#304", "Orin re-route labels Bravo's sails from sr33's crossover file")
def test_orin_opens_no_sr33_file(orin_bravo):
    assert orin_bravo[1] == []


@xfail("#308", "Orin re-route in tidal water says nothing about the missing current")
def test_orin_says_no_current(orin_bravo):
    assert says_missing(orin_bravo[0], "current")


@xfail("#307", "REROUTE_BOAT defaults to sr33: Bravo's Deploy is refused, sr33's profile used")
def test_orin_unconfigured_boat_never_falls_back_to_sr33():
    """No REROUTE_BOAT set: either the job refuses plainly, or it routes Bravo as Bravo."""
    try:
        job = _orin_job(None)
    except Exception:          # noqa: BLE001 — a required setting refusing to start is fine
        return
    out, seen = _orin_run(job)
    assert seen == []
    assert (out.get("boat") or {}).get("boat_id") in ("bravo", None)
    assert not no_sr33_text(out)


# ==== 5. Race defaults (wave 2) ==================================================================
@xfail("#310", "fleet retro defaults to the Bayview course when no definition is given")
def test_retro_has_no_bayview_default():
    import inspect
    p = inspect.signature(retro.run_fleet).parameters
    defaults = [str(p[k].default) for k in ("def_race_id", "course_id")]
    assert not any(re.search(r"bayview|cove_island", d, re.I) for d in defaults), defaults


@xfail("#311", "an ORC fleet for a race with no known ToT column gets the offshore number silently")
def test_fleet_ratings_say_which_number():
    rec = {"YachtName": "Bravo", "SailNo": "USA 5288", "GPH": 659.5, "TMF_Offshore": 0.6721,
           "Class": "C&C 35"}
    idx = {"by_sail": {fleetimport._norm_sail("USA 5288"): rec}, "by_name": {}, "n": 1}
    real = fleetimport._orc_dump
    fleetimport._orc_dump = lambda cc: idx
    try:
        res = fleetimport.enrich_from_orc([{"boat": "Bravo", "sail": "USA 5288"}], country="GBR",
                                          definition=RACE, course_id="c")
    finally:
        fleetimport._orc_dump = real
    assert res.get("ok") and res.get("matched") == 1
    head = {k: v for k, v in res.items() if k != "entries"}
    assert re.search(r"offshore|generic|no (known|race)", json.dumps(head).lower()), head


@xfail("#312", "a local gun time with no time zone is read as UTC, no note")
def test_gun_without_timezone_is_flagged():
    d = copy.deepcopy(RACE)
    d.pop("timezone")
    r = racestart.resolve(d, None, now=T0 - 86400)
    errs = race_def.validate(d)[0]
    assert ("zone" in (r.get("note") or "").lower() or r.get("source") != "race_start"
            or any("zone" in str(e).lower() for e in errs)), (r, errs)


def test_gun_with_timezone_is_local():
    """Europe/London in May is UTC+1: the 07:00 gun is 06:00Z."""
    assert racestart.resolve(RACE, None, now=T0 - 86400)["epoch"] == T0
