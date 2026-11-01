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
check("route already over → holds at the race start",
      sp["basis"] == "point" and sp["path"][0]["lat"] == 43.0 and sp["path"][0]["lon"] == -82.4)
P.set_target("r1", lat=44.5, lon=-82.1)
sp = P.resolve_target(NOW)
check("an explicit point target wins", sp["basis"] == "point" and sp["path"][0]["lat"] == 44.5)
P.set_target(None)
check("clearing the target falls back to the boat's push", P.resolve_target(NOW)["basis"] == "route")

# ---- push to the boat -----------------------------------------------------------------------
print("push to the boat:")
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
rec = P.push(pl, post=lambda u, b, timeout=25: (False, {"error": "timed out"}))
check("a failed push is recorded and retried next pass", not rec["ok"] and "timed out" in rec["error"]
      and P.needs_push(pl, P.last_push()))

# ---- frozen at the gun (RRS 41) ------------------------------------------------------------
print("frozen at the gun:")
GUN = NOW + 3 * H
RACE["division_starts"] = {"PHRF A": "2026-01-15 13:00"}
RACE["timezone"] = "UTC"
RACE["own"] = {"division": "PHRF A"}
from app import racestart  # noqa: E402
gun = racestart.race_start(RACE)["epoch"]
spec_g = {"definition": RACE, "course_id": "c1", "basis": "route",
          "path": [{"lat": 43.0, "lon": -82.4, "t": gun + 600}, {"lat": 45.8, "lon": -84.6, "t": gun + 30 * H}]}
spec_g["race_id"] = "r1"
win = P.race_window(spec_g)
check("race window starts at our gun", win[0] == gun)
check("no recorded finish → open-ended (never reopens on an estimate)", win[1] is None)
check("no gun → the route's first ETA",
      P.race_window({"basis": "route", "path": spec_g["path"]})[0] == gun + 600)
check("no gun, no route → unknown", P.race_window({"basis": "point", "path": [{"lat": 1, "lon": 1, "t": 0}]}) is None)
check("racing from the gun on", P.is_racing(win, gun) and not P.is_racing(win, gun - 1)
      and P.is_racing(win, gun + 30 * 86400))
P.mark_finished("r1", at=gun - 86400)
check("a finish recorded before this start (an old edition) doesn't count", P.race_window(spec_g)[1] is None)
P.mark_finished("r1", undo=True)

# run_once refuses to record or push while racing
P.set_target(None)
labstate.set("deploy_push:last_ok", {"race_id": "r1", "playbook_id": "r1__100", "ok": True})
BUNDLE["variants"][1]["route"]["path"] = spec_g["path"]
recorded, pushed = [], []
_orig_record, _orig_push, _orig_clock = P.record, P.push, P._clock
CLOCK = {"t": gun - H, "step": 0}
P._clock = lambda: CLOCK["t"]


def fake_record(spec, **kw):
    recorded.append(1)
    t = CLOCK["t"]
    CLOCK["t"] += CLOCK["step"]              # the pass takes this long
    return {"key": spec["key"], "runs": {"gfs": [
        {"cycle": t - 6 * H, "at": t, "pts": [[t, 10.0, 200], [t + H, 11.0, 210]]}]}}


P.record = fake_record
P.push = lambda plume, post=None, now=None: pushed.append(plume) or {"ok": True}
r = P.run_once(do_push=True)
check("before the gun: recorded + pushed, with the race start aboard and no end",
      recorded and pushed and pushed[-1]["race_start"] == int(gun) and pushed[-1]["race_end"] is None
      and not r.get("frozen"))
check("generated_at = when the computation finished", pushed[-1]["generated_at"] == int(gun - H))
recorded.clear(); pushed.clear()
CLOCK.update(t=gun - 60, step=120)           # starts a minute before the gun, finishes after it
r = P.run_once(do_push=True)
check("a pass that ends after the gun pushes nothing", r.get("frozen") and recorded and not pushed)
recorded.clear(); CLOCK.update(t=gun + 60, step=0)
r = P.run_once(do_push=True)
check("after the gun: nothing recorded, nothing pushed", r.get("frozen") and not recorded and not pushed)
CLOCK["t"] = gun + 40 * H
r = P.run_once(do_push=True)
check("long after the planned finish, still frozen (no estimate reopens it)",
      r.get("frozen") and not recorded and not pushed)
P.mark_finished("r1")
check("marked finished at the Lab's clock", P.finished_at("r1") == int(gun + 40 * H))
CLOCK["t"] = gun + 40 * H + 60
r = P.run_once(do_push=True)
check("race over: recording resumes and the plume carries the finish",
      not r.get("frozen") and recorded and pushed and pushed[-1]["race_end"] == int(gun + 40 * H))
P.record, P.push = _orig_record, _orig_push
P.mark_finished("r1", undo=True)
rec = P.push({"key": "k", "race_start": int(gun), "race_end": None, "models": [{"model": "gfs", "runs": []}]},
             post=lambda *a, **k: (True, {"loaded": True}))
check("push() itself refuses on the real clock once the race has started", rec["ok"] is False and rec.get("frozen"))
P._clock = _orig_clock

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
