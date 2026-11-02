"""Forecast plume — the snapshot store + plume builder (vps/lab/app/plume.py).

Pins: the boat's position along the planned route at a time; sampling a model's frames there
(never past the run's own horizon); one snapshot per NEW cycle (same cycle / same numbers are not
re-recorded); the 48 h history prune; the payload's newest-first runs clipped to the view window;
a second recorder pass downloading nothing when no newer cycle is due; target resolution from a
pinned race + playbook (route) and the race-start fallback; and the push-to-boat bookkeeping.
No network: frames and fetches are fakes.

Run in-container (make ci does this): python3 /repo/vps/lab/test_plume.py (workdir /srv).
"""
import math
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLUME_DIR"] = os.path.join(_tmp, "plume")
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"

from app import labstate, plume as P  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file
P.PLUME_DIR = os.environ["PLUME_DIR"]

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


H = 3600
NOW = 1_800_000_000 + 1800          # half past an hour


class Frame:
    """A uniform-wind frame: (tws kn, twd deg FROM) everywhere at `valid_time`."""
    def __init__(self, t, tws, twd):
        self.valid_time = t
        ms = tws / 1.9438445
        r = math.radians(twd)
        self.uv = (-ms * math.sin(r), -ms * math.cos(r))
        self.calls = []

    def sample_uv(self, lat, lon):
        self.calls.append((round(lat, 4), round(lon, 4)))
        return self.uv


# ---- the planned track -----------------------------------------------------------------------
print("position along the route:")
path = P.clean_path([{"lat": 45.0, "lon": -82.0, "t": NOW + 2 * H},
                     {"lat": 44.0, "lon": -82.0, "t": NOW},
                     {"lat": 46.0, "lon": -82.0, "t": NOW + 4 * H},
                     {"lat": None, "lon": -82.0, "t": NOW + 5 * H}])
check("points sorted by ETA, ones without a position dropped", [p["lat"] for p in path] == [44, 45, 46])
check("before the start the boat is at the start", P.position_at(path, NOW - 9 * H) == (44.0, -82.0))
check("after the finish it is at the finish", P.position_at(path, NOW + 9 * H) == (46.0, -82.0))
check("halfway between ETAs → halfway along", P.position_at(path, NOW + H) == (44.5, -82.0))

times = P.sample_times(NOW, back_h=3, ahead_h=24)
check("hourly times, 3 h back to 24 h ahead of the current hour",
      len(times) == 28 and times[0] == NOW - 1800 - 3 * H and times[-1] == NOW - 1800 + 24 * H)

# ---- sampling one run ------------------------------------------------------------------------
print("sampling a model run:")
t0 = NOW - 1800
frames = [Frame(t0, 10, 350), Frame(t0 + 3 * H, 16, 10)]
pts = P.sample_frames(frames, lambda t: P.position_at(path, t), [t0 - H, t0, t0 + H, t0 + 3 * H, t0 + 6 * H])
check("only times inside the run's frames are sampled", [p[0] for p in pts] == [t0, t0 + H, t0 + 3 * H])
same_dir = P.sample_frames([Frame(t0, 10, 200), Frame(t0 + 3 * H, 16, 200)], lambda t: (44, -82), [t0 + H])
check("speed interpolates in time", same_dir == [[t0 + H, 12.0, 200]])
check("direction comes out FROM-degrees and wraps through north", pts[0][2] == 350 and pts[2][2] == 10
      and (pts[1][2] >= 350 or pts[1][2] <= 10))
check("each time is sampled where the boat will be then",
      (44.0, -82.0) in frames[0].calls and (44.25, -82.0) in frames[0].calls)

# ---- the store -------------------------------------------------------------------------------
print("one snapshot per new cycle:")
st = {"key": "r1__pb", "runs": {}}
a = [[t0, 10.0, 200], [t0 + H, 11.0, 205]]
check("first run recorded", P.add_run(st, "gfs", t0 - 4 * H, a, NOW))
check("same cycle again is not re-recorded", not P.add_run(st, "gfs", t0 - 4 * H, a, NOW + 900))
check("a newer cycle is", P.add_run(st, "gfs", t0 + 2 * H, [[t0, 12.0, 210]], NOW + H))
check("estimated-cycle source serving the same numbers → not a new run",
      P.add_run(st, "icon", t0, a, NOW, estimated=True)
      and not P.add_run(st, "icon", t0 + 6 * H, [list(p) for p in a], NOW + H, estimated=True))
check("...but different numbers are", P.add_run(st, "icon", t0 + 6 * H, [[t0, 14.0, 220]], NOW + 2 * H,
                                                estimated=True))
check("empty runs never stored", not P.add_run(st, "nam", t0, [], NOW))
check("latest cycle per model", P.latest_cycle(st, "gfs") == t0 + 2 * H and P.latest_cycle(st, "nam") is None)
P.prune(st, NOW + 48 * H + 1800)
check("history older than 48 h pruned", [r["cycle"] for r in st["runs"]["gfs"]] == [t0 + 2 * H])

# ---- the payload ----------------------------------------------------------------------------
print("the plume payload:")
st = {"key": "r1__pb", "race_id": "r1", "label": "along the plan", "basis": "route", "runs": {}}
P.add_run(st, "hrrr", t0 - 2 * H, [[t0 - 8 * H, 5.0, 100], [t0, 8.0, 120]], NOW - 2 * H)
P.add_run(st, "hrrr", t0, [[t0, 9.0, 125], [t0 + 30 * H, 9.0, 125]], NOW)
P.add_run(st, "gfs", t0 - 6 * H, [[t0 - 10 * H, 5.0, 100]], NOW - 6 * H)     # all outside window
P.add_run(st, "zzz", t0, [[t0, 1.0, 1]], NOW)
pl = P.build_plume(st, NOW, back_h=6, ahead_h=24, models=("gfs", "hrrr", "zzz"))
names = [m["model"] for m in pl["models"]]
check("schema + race + basis carried", pl["schema"] == "c4.plume/v1" and pl["race_id"] == "r1"
      and pl["basis"] == "route")
check("only supported model ids are carried", names == ["hrrr"])
hr = pl["models"][0]["runs"]
check("runs newest first", [r["cycle"] for r in hr] == [t0, t0 - 2 * H])
check("points clipped to now−6 h .. now+24 h", hr[0]["pts"] == [[t0, 9.0, 125]] and hr[1]["pts"] == [[t0, 8.0, 120]])
check("a run with nothing in the window is dropped (gfs)", "gfs" not in names)
check("built_at = newest run recorded", pl["built_at"] == NOW)
check("payload stays compact", len(__import__("json").dumps(pl)) < 2000)

# ---- record(): only new cycles are fetched --------------------------------------------------
print("recorder pass:")
calls = []


def fake_fetch(src, bbox, times, now, parser):
    calls.append(src.name)
    cyc = src.pick_cycle(__import__("datetime").datetime.fromtimestamp(now, __import__("datetime").timezone.utc))
    return cyc.timestamp(), [Frame(times[0], 10, 270), Frame(times[-1], 20, 300)], src.name == "icon"


spec = {"key": "r1__route", "race_id": "r1", "basis": "route", "label": "along the plan",
        "path": [{"lat": 44.0, "lon": -82.0, "t": NOW}, {"lat": 45.0, "lon": -82.5, "t": NOW + 20 * H}]}
st1 = P.record(spec, now=NOW, models=("gfs", "hrrr", "icon", "gefs"), fetch=fake_fetch)
check("each deterministic model fetched once; the ensemble skipped", calls == ["gfs", "hrrr", "icon"])
check("snapshots stored per model", all(len(st1["runs"][m]) == 1 for m in ("gfs", "hrrr", "icon")))
check("a snapshot is a few KB at most", len(__import__("json").dumps(st1["runs"]["gfs"][0])) < 1500)
calls.clear()
st2 = P.record(spec, now=NOW + 600, models=("gfs", "hrrr", "icon"), fetch=fake_fetch)
check("10 min later nothing newer is due → nothing downloaded", calls == [])
check("store persisted on disk", P.load_store("r1__route")["runs"]["hrrr"] == st2["runs"]["hrrr"])
calls.clear()
P.record(spec, now=NOW + 2 * H, models=("gfs", "hrrr"), fetch=fake_fetch)
check("two hours later the hourly HRRR has a new cycle, GFS (6-hourly) may not",
      "hrrr" in calls and len(P.load_store("r1__route")["runs"]["hrrr"]) == 2)


def broken_fetch(src, *a):
    raise OSError("NOMADS down")


st3 = P.record(dict(spec, key="r1__broken"), now=NOW, models=("gfs",), fetch=broken_fetch)
check("a failing model doesn't break the pass", st3["runs"].get("gfs") in (None, []))

# ---- targets ---------------------------------------------------------------------------------
print("which route the plume follows:")
from app import pbstore, store  # noqa: E402

RACE = {"race_id": "r1", "name": "Race One", "courses": [{"id": "c1", "marks": [
    {"name": "Start", "lat": 43.0, "lon": -82.4}, {"name": "Finish", "lat": 45.8, "lon": -84.6}]}]}
store.get_race = lambda rid: RACE if rid == "r1" else None
BUNDLE = {"race_id": "r1", "course_id": "c1", "recommended": "b",
          "variants": [{"id": "a", "route": {"path": [{"lat": 1, "lon": 1, "t": NOW}]}},
                       {"id": "b", "name": "Right", "route": {"path": [
                           {"lat": 43.0, "lon": -82.4, "t": NOW + H},
                           {"lat": 45.8, "lon": -84.6, "t": NOW + 30 * H}]}}]}
pbstore.get = lambda pid: BUNDLE if pid == "r1__100" else None
check("no target and nothing pushed → none", P.resolve_target(NOW) is None)
labstate.set("deploy_push:last_ok", {"race_id": "r1", "playbook_id": "r1__100", "ok": True})
sp = P.resolve_target(NOW)
check("follows what the boat holds: the recommended variant's route",
      sp and sp["basis"] == "route" and sp["path"][0]["lat"] == 43.0 and "Right" in sp["label"])
check("store key is race + playbook", sp["key"] == "r1__r1__100")
sp = P.resolve_target(NOW + 40 * H)
check("route over → holds at the race start",
      sp["basis"] == "point" and sp["path"][0]["lat"] == 43.0 and sp["path"][0]["lon"] == -82.4)
P.set_target("r1", lat=44.5, lon=-82.1)
sp = P.resolve_target(NOW)
check("an explicit point target wins", sp["basis"] == "point" and sp["path"][0]["lat"] == 44.5)
P.set_target(None)
check("clearing the target falls back to the boat's push", P.resolve_target(NOW)["basis"] == "route")

# ---- push to the boat -----------------------------------------------------------------------
print("push to the boat:")
rec = P.push(pl, post=lambda *a, **k: (True, {"loaded": True}), now=NOW)
check("a plume with no race start is never sent", rec["ok"] is False and "no race start" in rec["error"])
pl = dict(pl, race_start=NOW + 100 * H, race_end=None)
check("nothing to push without runs", not P.needs_push({"models": []}, None))
check("never pushed → push", P.needs_push(pl, None))
posted = []


def fake_post(url, body, timeout=25):
    posted.append(url)
    return True, {"loaded": True}


rec = P.push(pl, post=fake_post)
check("posts to the engine's /plume/load on the Deploy address",
      posted == ["http://pi:8200/plume/load"] and rec["ok"])
check("same plume confirmed → no re-push", not P.needs_push(pl, P.last_push()))
check("new runs → push again", P.needs_push(dict(pl, built_at=NOW + H), P.last_push()))
check("same runs, race start moved → push again", P.needs_push(dict(pl, race_start=NOW + 101 * H), P.last_push()))
rec = P.push(pl, post=lambda u, b, timeout=25: (False, {"error": "timed out"}))
check("a failed push is recorded and retried next pass", not rec["ok"] and "timed out" in rec["error"]
      and P.needs_push(pl, P.last_push()))

# ---- paused from the start (RRS 41) — the Lab side; the boat decides on receipt ------------
print("race start:")
from app import racestart  # noqa: E402
RACE["division_starts"] = {"PHRF A": "2026-01-15 13:00"}
RACE["timezone"] = "UTC"
RACE["own"] = {"division": "PHRF A"}
gun = racestart.race_start(RACE)["epoch"]
route = [{"lat": 43.0, "lon": -82.4, "t": gun + 600}, {"lat": 45.8, "lon": -84.6, "t": gun + 30 * H}]
check("our gun (route after it)", P.race_start_of({"definition": RACE, "basis": "route", "path": route}) == gun)
early = [dict(route[0], t=gun - 3600), route[1]]
check("a route planned from an EARLIER start wins over the gun (earliest = conservative)",
      P.race_start_of({"definition": RACE, "basis": "route", "path": early}) == gun - 3600)
DAY = {"race_id": "r5", "start_date": "2026-07-18", "timezone": "America/Detroit", "courses": []}
r1400 = [{"lat": 43.0, "lon": -82.4, "t": 1784397600}, {"lat": 45.8, "lon": -84.6, "t": 1784397600 + 30 * H}]
check("no gun configured, route planned from 14:00, real gun 13:00 → start date's first minute, not 14:00",
      P.race_start_of({"definition": DAY, "basis": "route", "path": r1400}) == 1784347200 < 1784394000)
DAYGUN = dict(DAY, division_starts={"X": "2026-07-18 13:00"}, own={"division": "X"})
check("gun 13:00 + start date + 14:00 route: the gun (race-morning updates still go out)",
      P.race_start_of({"definition": DAYGUN, "basis": "route", "path": r1400}) == 1784394000)
check("gun 13:00 + start date, no route: the gun",
      P.race_start_of({"definition": DAYGUN, "basis": "point", "path": [{"lat": 1, "lon": 1, "t": 0}]}) == 1784394000)
check("gun only: the gun", P.race_start_of({"definition": dict(DAYGUN, start_date=None), "basis": "point",
                                            "path": [{"lat": 1, "lon": 1, "t": 0}]}) == 1784394000)
NOGUN = {"race_id": "r3", "name": "No Guns", "start_date": "2026-07-18", "timezone": "America/Detroit",
         "courses": [{"id": "c1", "marks": [{"name": "S", "lat": 43.0, "lon": -82.4}, {"name": "F", "lat": 45.8, "lon": -84.6}]}]}
check("no gun → the route's first ETA", P.race_start_of({"definition": NOGUN, "basis": "route", "path": route}) == gun + 600)
check("no gun, no route → the first minute of the start date, venue time",
      P.race_start_of({"definition": NOGUN, "basis": "point", "path": [{"lat": 1, "lon": 1, "t": 0}]}) == 1784347200)
check("nothing known → None", P.race_start_of({"definition": {}, "basis": "point", "path": [{"lat": 1, "lon": 1, "t": 0}]}) is None)

print("run_once around the start:")
RACES = {"r1": RACE, "r3": NOGUN}
store.get_race = lambda rid: RACES.get(rid)
BUNDLE["variants"][1]["route"]["path"] = route
pbstore.get = lambda pid: BUNDLE if pid == "r1__100" else None
P.set_target(None)
labstate.set("deploy_push:last_ok", {"race_id": "r1", "playbook_id": "r1__100", "ok": True})
labstate.set("plume:push", None)
recorded, pushed = [], []
_orig_record, _orig_push, _orig_clock = P.record, P.push, P._clock
CLOCK = {"t": gun - H, "step": 0}
P._clock = lambda: CLOCK["t"]


def fake_record(spec, **kw):
    recorded.append(spec["key"])
    t = CLOCK["t"]
    CLOCK["t"] += CLOCK["step"]              # the pass takes this long
    st = {"key": spec["key"], "race_id": spec["race_id"], "label": spec.get("label"), "basis": spec.get("basis"),
          "runs": {"gfs": [{"cycle": int(t) - 6 * H, "at": int(t), "pts": [[int(t), 10.0, 200], [int(t) + H, 11.0, 210]]}]}}
    P.save_store(st)
    return st


P.record = fake_record
P.push = lambda plume, post=None, now=None: pushed.append(plume) or {"ok": True}
r = P.run_once(do_push=True)
check("before the start: recorded + sent, race_start aboard, generated_at = when it finished",
      recorded and pushed and pushed[-1]["race_start"] == int(gun) and pushed[-1]["generated_at"] == int(gun - H))
HELD = []
P.push = lambda plume, post=None, now=None: HELD.append(P._REC_LOCK.locked()) or {"ok": True}
labstate.set("plume:push", None)
P.run_once(do_push=True)
check("the push happens under the record lock (passes can't send out of order)", HELD == [True])
check("...and the lock is free afterwards", not P._REC_LOCK.locked())
P.push = lambda plume, post=None, now=None: pushed.append(plume) or {"ok": True}
recorded.clear(); pushed.clear()
CLOCK.update(t=gun - 60, step=120)           # starts a minute before the gun, finishes after it
r = P.run_once(do_push=True)
check("a pass that ends after the start sends nothing", r.get("paused") and recorded and not pushed)
recorded.clear(); CLOCK.update(t=gun + 60, step=0)
r = P.run_once(do_push=True)
check("after the start: paused — nothing recorded or sent", r.get("paused") and not recorded and not pushed)
CLOCK["t"] = gun + 40 * H
check("and stays paused for that race (no estimated end)", P.run_once(do_push=True).get("paused") and not recorded)
seq = iter([gun - 60, gun + 5, gun + 5, gun + 5])
P._clock = lambda: next(seq)
r = P.run_once(do_push=True)                 # checked before the start, got the record lock after it
check("the clock is re-checked inside the record lock", r.get("paused") and not recorded and not pushed)
P._clock = lambda: CLOCK["t"]
P.set_target("r3", lat=43.1, lon=-82.4)
NOGUN.pop("start_date")
CLOCK["t"] = gun - H
r = P.run_once(do_push=True)
check("no start known at all: records, never sends", recorded and not pushed and r["push"].get("skipped"))
NOGUN["start_date"] = "2026-07-18"
P.set_target(None)

print("push() on its own:")
P.push = _orig_push
rec = P.push({"key": "k", "race_start": int(gun), "models": [{"model": "gfs", "runs": []}]},
             post=lambda *a, **k: (True, {"loaded": True}), now=gun + 1)
check("refuses once the race has started (real clock at send)", rec["ok"] is False and rec.get("paused"))

print("paths that can record, send or change the target:")
os.environ["LAB_AUTH_ENABLED"] = "false"
from fastapi.testclient import TestClient  # noqa: E402
from app import main as M  # noqa: E402
client = TestClient(M.app)
P.push = lambda plume, post=None, now=None: pushed.append(plume) or {"ok": True}
recorded.clear(); pushed.clear()
CLOCK["t"] = gun + 2 * H                     # r1 (the boat's race) has started
client.post("/api/plume/refresh")
import time as _t  # noqa: E402
for _ in range(50):
    st = client.get("/api/plume/refresh/status").json()
    if st.get("state") != "running":
        break
    _t.sleep(0.05)
check("manual refresh after the start: paused, nothing recorded or sent",
      (st.get("result") or {}).get("paused") and not recorded and not pushed)
g = client.get("/api/plume").json()
check("GET /api/plume: says paused since the start, records nothing",
      g["paused"] and g["race_start"] == gun and not recorded)
GB = gun + 200 * H
RB = dict(RACE, race_id="rB", division_starts={"PHRF A": GB})
RACES["rB"] = RB
client.post("/api/plume/target", json={"race_id": "rB"})
r = P.run_once(do_push=True)
check("pinning a race that hasn't started: recorded and offered to the boat (the boat decides)",
      not r.get("paused") and pushed and pushed[-1]["race_id"] == "rB" and pushed[-1]["race_start"] == int(GB))
client.post("/api/plume/target", json={})
import importlib  # noqa: E402
P = importlib.reload(P)
P.PLUME_DIR = os.environ["PLUME_DIR"]
P._clock = lambda: CLOCK["t"]
check("after a Lab restart the started race is still paused (it's derived, not stored)",
      P.run_once(do_push=True).get("paused"))
_orig_record, _orig_push = P.record, P.push

# ---- end to end with the engine's real load() ---------------------------------------------
print("end to end with the engine's load():")
import importlib.util  # noqa: E402
import json as _json  # noqa: E402
os.environ["PLUME_CACHE"] = os.path.join(_tmp, "boat", "plume.json")
_spec = importlib.util.spec_from_file_location("engine_plume", "/repo/vps/agent/app/plume.py")
ENG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ENG)
SESSION = {"cur": None}
ENG.race_session = lambda: SESSION["cur"]
from app import deploy as _deploy  # noqa: E402
_deploy._post_json = lambda url, body, timeout=25: (True, ENG.load(_json.loads(_json.dumps(body)), now=CLOCK["t"]))
P.record = fake_record
labstate.set("plume:push", None)
GA = gun + 400 * H
RA = dict(RACE, race_id="rA", division_starts={"PHRF A": GA})
RB2 = dict(RACE, race_id="rB2", division_starts={"PHRF A": GA + 300 * H})
RACES.update(rA=RA, rB2=RB2)
P.set_target("rA")
CLOCK.update(t=GA - 2 * H, step=0)
r = P.run_once(do_push=True)
check("A before its start: taken aboard", r["push"]["ok"] and ENG.get(CLOCK["t"])["race_id"] == "rA")
CLOCK["t"] = GA + 60
SESSION["cur"] = {"id": 1, "kind": "race", "start_ts": GA}       # crew taps race start
check("A underway: Lab paused, boat shows A frozen", P.run_once(do_push=True).get("paused")
      and ENG.get(CLOCK["t"])["frozen"])
P.set_target("rB2")
r = P.run_once(do_push=True)
check("B pinned mid-race: offered, refused by the boat (race session running)",
      r["push"]["ok"] is False and ENG.get(CLOCK["t"])["race_id"] == "rA")
CLOCK["t"] = GA + 40 * H
SESSION["cur"] = None                                            # crew taps race end
CLOCK["t"] += 900
r = P.run_once(do_push=True)
g = ENG.get(CLOCK["t"])
check("session ended: B's pre-start plume is taken, no Lab-side handshake",
      r["push"]["ok"] and g["race_id"] == "rB2" and not g["frozen"])
P.record, P.push, P._clock = _orig_record, _orig_push, _orig_clock

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
