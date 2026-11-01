"""Onboard forecast-plume cache (vps/agent/app/plume.py, served by the Pi engine's /plume).

Pins: a pushed plume lands on disk and comes back with its age; it survives a "restart" (a fresh
read of the file, nothing held in memory); a bad payload is refused and never replaces the good
one; no plume aboard is an honest `available: false`.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_plume_cache.py
"""
import json
import os
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from app import plume  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


NOW = 1_800_000_000
GOOD = {"schema": "c4.plume/v1", "key": "r1__pb", "race_id": "r1", "basis": "route",
        "race_start": NOW + 10 * 86400, "race_end": None, "built_at": NOW - 600, "window": [NOW - 6 * 3600, NOW + 24 * 3600],
        "models": [{"model": "hrrr", "runs": [{"cycle": NOW - 7200, "at": NOW - 600,
                                               "pts": [[NOW, 11.2, 240], [NOW + 3600, 12.0, 245]]}]}]}

print("nothing aboard:")
g = plume.get(NOW)
check("available false with a reason", g["available"] is False and "internet" in g["note"])

print("push → disk → read:")
r = plume.load(GOOD, now=NOW - 300)
check("load accepted", r["loaded"] and r["models"] == 1 and r["built_at"] == NOW - 600)
path = os.path.join(_tmp, "engine", "plume.json")
check("written next to the engine store", os.path.exists(path))
g = plume.get(NOW)
check("served back with the data age (since the Lab recorded it)", g["available"] and g["age_s"] == 600)
check("...and how long since the boat got it", g["received_age_s"] == 300)
check("the runs come back intact", g["models"][0]["runs"][0]["pts"][1] == [NOW + 3600, 12.0, 245])

print("survives a restart / offline:")
with open(path) as f:
    on_disk = json.load(f)
check("the file alone holds the plume (nothing in memory)", on_disk["plume"]["key"] == "r1__pb")
g = plume.get(NOW + 6 * 3600)
check("six hours offline: still served, age grows", g["available"] and g["age_s"] == 6 * 3600 + 600)

print("bad payloads refused:")
for name, bad in (("wrong schema", dict(GOOD, schema="x")),
                  ("no models", dict(GOOD, models=[])),
                  ("bad points", dict(GOOD, models=[{"model": "gfs", "runs": [{"pts": [[1, 2]]}]}])),
                  ("not a dict", ["nope"]),
                  ("unknown model id", dict(GOOD, models=[{"model": "xyz", "runs": GOOD["models"][0]["runs"]}])),
                  ("label not a string", dict(GOOD, label={"a": 1})),
                  ("infinite timestamp", dict(GOOD, built_at=float("inf"))),
                  ("infinite point", dict(GOOD, models=[{"model": "hrrr", "runs": [
                      dict(GOOD["models"][0]["runs"][0], pts=[[NOW, 1e309, 10]])]}])),
                  ("NaN race start", dict(GOOD, race_start=float("nan"))),
                  ("no race start (can't tell if its race is on)", dict(GOOD, race_start=None)),
                  ("cycle not a number", dict(GOOD, models=[{"model": "hrrr", "runs": [
                      dict(GOOD["models"][0]["runs"][0], cycle="12Z")]}]))):
    r = plume.load(bad, now=NOW)
    check(f"{name} → not loaded", r["loaded"] is False)
check("...and the good plume is still aboard, readable", plume.get(NOW)["key"] == "r1__pb"
      and plume.get(NOW)["age_s"] == 600)
big = dict(GOOD, models=[{"model": "gfs", "runs": [{"cycle": NOW, "at": NOW,
                                                   "pts": [[NOW + i, 1.0, 1] for i in range(60000)]}]}])
check("the big fixture is otherwise valid", plume.validate(big) == (True, ""))
r = plume.load(big, now=NOW)
check("an absurdly large payload is refused for its size", r["loaded"] is False and "too large" in r["detail"])

_ORIG_RS = plume.race_session


def _real_rs(cur):
    """Run the real race_session() against a stub race log returning `cur`."""
    import types
    from app import racelog
    saved = racelog._src
    racelog._src = lambda: types.SimpleNamespace(session_current=lambda: cur)
    try:
        return _ORIG_RS()
    finally:
        racelog._src = saved


SESSION = {"cur": None}


def fake_log():
    """Stands in for the race log: what racelog reports, filtered like race_session() does."""
    cur = SESSION["cur"]
    return cur if cur and cur.get("kind") == "race" else None


plume.race_session = fake_log
START = NOW + 3600
PRE = dict(GOOD, generated_at=NOW - 60, race_start=START, race_end=None)

print("a) the boat's race session is the authority:")
os.remove(path)
SESSION["cur"] = {"id": 7, "kind": "race", "start_ts": NOW - 600, "race_id": "r1"}
check("a race session running → any plume refused, even pre-start, any race",
      plume.load(PRE, now=NOW)["loaded"] is False
      and plume.load(dict(PRE, race_id="r9", key="r9__x"), now=NOW)["loaded"] is False)
SESSION["cur"] = {"id": 8, "kind": "practice", "start_ts": NOW - 600}
check("a practice session doesn't block", plume.load(PRE, now=NOW)["loaded"])
from app import racelog as _rl  # noqa: E402
_saved_src = _rl._src
_rl._src = lambda: (_ for _ in ()).throw(OSError("db locked"))
check("race log unreadable → treated as racing (fail closed)", bool(_ORIG_RS()))
_rl._src = _saved_src
check("the real race_session() ignores practice and reads race",
      _real_rs({"kind": "practice"}) is None and _real_rs({"kind": "race", "id": 1})["id"] == 1)
SESSION["cur"] = {"id": 9, "kind": "race", "start_ts": NOW - 60}
g = plume.get(NOW)
check("cached plume shows frozen while a race session runs, at the session's start",
      g["frozen"] and g["frozen_at"] == NOW - 60)
SESSION["cur"] = None
nxt = dict(GOOD, race_id="r2", key="r2__pb", generated_at=NOW + 100, race_start=NOW + 86400, race_end=None)
check("session ended → the next race's pre-start plume is taken",
      plume.load(nxt, now=NOW + 200)["loaded"] and plume.get(NOW + 300)["race_id"] == "r2"
      and plume.get(NOW + 300)["frozen"] is False)

print("b-d) payload rules on the boat's clock:")
check("b) no race_start → refused", plume.load(dict(PRE, race_start=None), now=NOW)["loaded"] is False)
check("c) its race has started at receipt → refused (empty cache or not)",
      plume.load(dict(PRE, key="late"), now=START + 5)["loaded"] is False)
check("c) ...unless it carries a race_end already past",
      plume.load(dict(PRE, key="after", race_end=START + 100, generated_at=START + 200), now=START + 300)["loaded"])
check("d) computed after its start → refused",
      plume.load(dict(PRE, key="d", race_start=NOW - 10, generated_at=NOW - 5), now=NOW - 20)["loaded"] is False)
g = plume.get(START + 400)
check("no cache-dependent hold: any race's pre-start plume is taken when no session runs",
      plume.load(dict(nxt, generated_at=START + 500, race_start=START + 86400), now=START + 600)["loaded"])
plume.load(PRE, now=NOW)
g = plume.get(START + 60)
check("cached plume past its race_start shows frozen at that start", g["frozen"] and g["frozen_at"] == START)

print("a load queued at the gun is judged when it gets the lock:")
os.remove(path)
clock = {"t": START - 1}
_real_time = plume.time.time
plume.time.time = lambda: clock["t"]
res = {}
plume._LOCK.acquire()
th = threading.Thread(target=lambda: res.update(plume.load(dict(PRE, generated_at=START - 30))))
th.start()
time.sleep(0.2)
clock["t"] = START + 1                     # the gun goes while it waits for the lock
plume._LOCK.release()
th.join()
check("refused (fresh clock inside the lock)", res.get("loaded") is False)
SESSION["cur"] = None
clock["t"] = START - 100
res = {}
plume._LOCK.acquire()
th = threading.Thread(target=lambda: res.update(plume.load(dict(PRE, generated_at=START - 130))))
th.start()
time.sleep(0.2)
SESSION["cur"] = {"id": 10, "kind": "race", "start_ts": START - 50}   # crew taps race start meanwhile
plume._LOCK.release()
th.join()
check("a race session started while it waited → refused", res.get("loaded") is False)
SESSION["cur"] = None
plume.time.time = _real_time

print("a race session can't start between the check and the write:")
from app import racelog  # noqa: E402
if os.path.exists(path):
    os.remove(path)
seen = {}


def session_starts_meanwhile():
    """The check passes (no session) — and right then the crew taps race start."""
    def tap():
        with racelog.SESSION_LOCK:         # what racelog.start() takes
            seen["file_when_session_started"] = os.path.exists(path)
    th = threading.Thread(target=tap)
    th.start()
    time.sleep(0.15)                       # without the shared lock, the session would win this
    seen["thread"] = th
    return None


plume.race_session = session_starts_meanwhile
plume.load(dict(PRE, key="race"), now=NOW)
seen["thread"].join()
plume.race_session = fake_log
check("the session start waited until the load had finished writing",
      seen.get("file_when_session_started") is True)
import types  # noqa: E402
_saved_src = racelog._src
racelog._src = lambda: types.SimpleNamespace(session_start=lambda *a: {"id": 1}, session_current=lambda: None)
done = {}
racelog.SESSION_LOCK.acquire()
th = threading.Thread(target=lambda: done.update(r=racelog.start(race_id="r1", kind="race")))
th.start()
time.sleep(0.15)
blocked = "r" not in done
racelog.SESSION_LOCK.release()
th.join()
racelog._src = _saved_src
check("racelog.start() takes the same lock", blocked and done["r"]["ok"])

print("engine restart:")
plume.load(PRE, now=NOW)
import importlib  # noqa: E402
plume = importlib.reload(plume)
plume.race_session = fake_log
check("after a restart the cached plume is still there and frozen past its start",
      plume.get(START + 60)["frozen"] and plume.load(dict(PRE, key="x"), now=START + 70)["loaded"] is False)

print("concurrent loads:")
import threading  # noqa: E402
os.remove(path)
errs = []


def writer(i):
    for j in range(25):
        try:
            r = plume.load(dict(GOOD, key=f"w{i}_{j}"), now=NOW)
            if not r["loaded"]:
                errs.append(r)
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))


ts = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
check("200 concurrent loads, none failed", not errs)
with open(path) as f:
    last = json.load(f)
check("the file is one whole plume afterwards", last["plume"]["key"].startswith("w"))
check("no temp files left behind", not [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")])

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
