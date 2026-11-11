"""Race-log sessions + archiver retention prune — unit test. Sessions use a stub datasource
(the racelog module's logic); the prune runs against REAL temp SQLite archive + engine DBs
(the archiver's actual SQL).

Run:  PYTHONPATH=vps/agent:.:pi/archiver python3 vps/agent/test_racelog.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

from app import racelog

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---------------------------------------------------------------- 1) sessions via racelog
class StubDS:
    def __init__(self):
        self.rows = []

    def session_start(self, name, race_id, kind, start_ts, boat_id=None, boat_name=None):
        cur = self.session_current()
        if cur:
            self.session_end(start_ts, session_id=cur["id"])
        self.rows.append({"id": len(self.rows) + 1, "name": name, "race_id": race_id,
                          "kind": kind, "start_ts": start_ts, "end_ts": None,
                          "boat_id": boat_id, "boat_name": boat_name})
        return self.session_current()

    def session_end(self, end_ts, session_id):
        for r in self.rows:
            if r["end_ts"] is None and r["id"] == session_id:
                r["end_ts"] = end_ts

    def session_current(self):
        live = [r for r in self.rows if r["end_ts"] is None]
        return dict(live[-1]) if live else None

    def sessions_list(self, limit=20):
        return [dict(r) for r in reversed(self.rows)][:limit]


print("1) racelog sessions — one-tap start, defaults, no Lab prep")
ds = StubDS()
racelog.datasource.active = lambda: ds
racelog.deviation._load_playbook = lambda: None            # nothing aboard — still works
r = racelog.start()
check("start with NOTHING (no playbook/name/race/kind) works, and is a RACE (fails closed)",
      r["ok"] and r["active"]["kind"] == "race" and r["active"]["name"].startswith("Race "))
r2 = racelog.start(name="Wed beer can", kind="race")
check("starting again auto-ends the previous window",
      r2["ok"] and ds.rows[0]["end_ts"] is not None and r2["active"]["name"] == "Wed beer can")
racelog.deviation._load_playbook = lambda: {"race_id": "bayview-mackinac-2026"}
racelog.end()
r3 = racelog.start()
check("playbook aboard -> race_id picked up + kind=race",
      r3["active"]["race_id"] == "bayview-mackinac-2026" and r3["active"]["kind"] == "race")
st = racelog.status()
check("status: active + recent", st["active"]["id"] == 3 and len(st["recent"]) == 3)
racelog.end()
check("end closes it", racelog.status()["active"] is None)
check("end with nothing running refuses politely", racelog.end()["ok"] is False)
rp = racelog.start(kind="practice")
check("an explicit practice start is a practice ('Sail …')",
      rp["ok"] and rp["active"]["kind"] == "practice" and rp["active"]["name"].startswith("Sail "))
check("an unknown kind is refused", racelog.start(kind="regatta")["ok"] is False)
racelog.end()

print("1a) end is scoped to one session, under the session lock")
import threading  # noqa: E402

ds = StubDS()
racelog.datasource.active = lambda: ds
racelog.deviation._load_playbook = lambda: None
a1 = racelog.start(name="A", kind="practice")["active"]
racelog.end()
b1 = racelog.start(name="B", kind="race")["active"]
r = racelog.end(session_id=a1["id"])
check("ending A (the id the iPad showed) while race B runs: 'Already ended', B still open",
      r == {"ok": False, "note": "Already ended"} and ds.session_current()["id"] == b1["id"])
r = racelog.end(session_id=b1["id"])
check("ending B by its id ends B", r["ok"] and r["ended"]["id"] == b1["id"]
      and ds.session_current() is None)


class SlowRead(StubDS):
    """session_current() stalls once while end() is reading it."""
    def __init__(self):
        super().__init__()
        self.reading, self.go = threading.Event(), threading.Event()
        self.slow = False

    def session_current(self):
        if self.slow:
            self.slow = False
            self.reading.set()
            self.go.wait(5)
        return super().session_current()


ds = SlowRead()
racelog.datasource.active = lambda: ds
a2 = racelog.start(name="A", kind="practice")["active"]
ds.slow = True
t_end = threading.Thread(target=lambda: racelog.end())
t_end.start()
ds.reading.wait(5)                                   # end() has read the current session (A)
t_start = threading.Thread(target=lambda: racelog.start(name="B", kind="race"))
t_start.start()
t_start.join(0.3)
check("a race start waits while an end is between its read and its close", t_start.is_alive())
ds.go.set()
t_end.join(5)
t_start.join(5)
cur = ds.session_current()
check("…so the end closes A only, and race B is open after it",
      cur is not None and cur["name"] == "B" and cur["end_ts"] is None
      and [r["end_ts"] is not None for r in ds.rows] == [True, False])
racelog.end()

print("1b) auto-close — one mis-tap must not suspend retention forever")
# The Jul 18 shape: a session started at the kite hoist and never stopped. Retention keeps every
# session window, and an open one runs to now + 1 day, so ONE open session suspends the prune for
# the whole archive — which is what has been happening on the boat since 2026-07-18.
NOW = time.time()


class MotionDS(StubDS):
    """StubDS + an archive series. `plan` is [(hours_ago, sog_kn)], newest last."""

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def series(self, path, minutes):
        cut = NOW - float(minutes) * 60.0
        return [(NOW - h * 3600.0, kn / 1.943844) for h, kn in self.plan
                if NOW - h * 3600.0 >= cut]


def parked(underway_h, still_h, kn=6.0):
    """Under way until `underway_h` ago, then stopped, one sample a minute."""
    rows = [(underway_h + m / 60.0, kn) for m in range(240, 0, -1)]
    rows += [(still_h - m / 60.0, 0.1) for m in range(int(still_h * 60))]
    return sorted(rows, key=lambda r: -r[0])


def fresh(ds, started_h_ago=30.0):
    """A session opened `started_h_ago` and never closed — the Jul 18 state."""
    racelog.datasource.active = lambda: ds
    ds.session_start("Race", None, "race", NOW - started_h_ago * 3600.0)
    racelog._last_check[0] = 0.0
    return ds


ds = fresh(MotionDS(parked(underway_h=20, still_h=19)))       # stopped 19 h, moving before that
r = racelog.autoclose(now=NOW)
check("stopped for 19 h -> closed at the LAST under-way moment, not at 'now'",
      r["closed"] and abs((NOW - r["end_ts"]) / 3600.0 - 20) < 0.1)
check("...and the session really is closed in the store", ds.session_current() is None)

ds = fresh(MotionDS(parked(underway_h=4, still_h=3)), started_h_ago=8)   # only 3 h stopped
check("stopped for 3 h -> left open (a lunch stop is not the end of a sail)",
      racelog.autoclose(now=NOW)["closed"] is False and ds.session_current() is not None)

ds = fresh(MotionDS([(h, 0.0) for h in (30.0, 29.0, 28.0)]))  # three samples, all stopped
r = racelog.autoclose(now=NOW)
check("a nearly-silent bus is NOT read as a parked boat — silence is not evidence",
      r["closed"] is False and "not closing" in r["note"] and ds.session_current() is not None)

ds = fresh(MotionDS([(m / 60.0, 0.1) for m in range(1, 2400)]))  # plenty of samples, never moved
r = racelog.autoclose(now=NOW)
check("no under-way moment anywhere in the look-back -> refuses, and says a human must close it",
      r["closed"] is False and "from the iPad" in r["note"])

# REC tapped at the dock AFTER the sail: the only under-way moments precede the session.
ds = fresh(MotionDS(parked(underway_h=20, still_h=19)), started_h_ago=6)
r = racelog.autoclose(now=NOW)
check("under-way moments that PREDATE the session can't end it (end_ts < start_ts is a "
      "negative window, and the prune reads these)",
      r["closed"] is False and ds.session_current() is not None)

ds = fresh(MotionDS(parked(underway_h=20, still_h=19)))
racelog._last_check[0] = NOW                                   # a check just ran
check("throttled between checks (the REC poll is every few seconds)",
      racelog.autoclose(now=NOW)["note"] == "throttled" and ds.session_current() is not None)

ds = fresh(MotionDS(parked(underway_h=20, still_h=19)))
st = racelog.status()
check("status() drives it, and TELLS the crew their session was ended for them",
      st["active"] is None and st["auto_closed"]["name"] == "Race")

# M14: the crew taps REC (a new RACE session) while the slow series read is running. The close
# must not end the new session at the old session's last under-way moment (that reopened the
# homework gate mid-race).
class TapMidRead(MotionDS):
    def series(self, path, minutes):
        out = super().series(path, minutes)
        if not self.tapped:
            self.tapped = True
            racelog.start(name="Today", kind="race")        # takes SESSION_LOCK, then returns
        return out


ds = TapMidRead(parked(underway_h=20, still_h=19))
ds.tapped = False
fresh(ds)
r = racelog.autoclose(now=NOW)
cur = ds.session_current()
check("M14: a race session started during the check is NOT closed by it",
      r["closed"] is False and cur is not None and cur["name"] == "Today"
      and cur["end_ts"] is None)
check("M14: no session ends before it starts", all(
    x["end_ts"] is None or x["end_ts"] >= x["start_ts"] for x in ds.rows))

racelog.AUTOCLOSE_IDLE_H = 0.0
ds = fresh(MotionDS(parked(underway_h=20, still_h=19)))
check("RACELOG_AUTOCLOSE_IDLE_H=0 disables it entirely",
      racelog.autoclose(now=NOW)["closed"] is False and ds.session_current() is not None)
racelog.AUTOCLOSE_IDLE_H = 12.0

print("1c) #241: a hole in the record never counts toward closing")
# Under way until 20 h ago, stopped 19 h..13 h ago, NOTHING for 11 h (bus down), stopped again
# for the last 2 h. The old rule (30+ readings in the last 12 h, none under way) closed this.
holed = [(20 + m / 60.0, 6.0) for m in range(240, 0, -1)]
holed += [(19 - m / 60.0, 0.1) for m in range(6 * 60)]
holed += [(2 - m / 60.0, 0.1) for m in range(2 * 60)]
ds = fresh(MotionDS(holed))
r = racelog.autoclose(now=NOW)
check("stopped 19 h on paper, but with an 11 h hole: NOT closed (only 2 h unbroken)",
      r["closed"] is False and ds.session_current() is not None)
check("still_since: the newest reading under way -> None",
      racelog.still_since([(NOW - 60, 0.1), (NOW - 1, 5.0)], NOW) is None)
check("still_since: the newest reading older than the max gap -> None (no evidence now)",
      racelog.still_since([(NOW - 3600, 0.1)], NOW) is None)
check("still_since: a hole restarts the stretch",
      racelog.still_since([(NOW - 4000, 0.1), (NOW - 100, 0.1), (NOW - 10, 0.1)], NOW) == NOW - 100)


class RaceDS(MotionDS):
    """MotionDS + the persisted racing state (the engine kv)."""

    def __init__(self, plan):
        super().__init__(plan)
        self.race = None

    def race_state(self):
        return dict(self.race) if self.race is not None else None

    def save_race_state(self, blob, only_if_absent=False):
        if only_if_absent and self.race is not None:
            return
        self.race = dict(blob)


def racing_ds(plan, race_h_ago):
    """A race started `race_h_ago` (the gun tap), with an SOG record `plan`."""
    ds = RaceDS(plan)
    racelog.datasource.active = lambda: ds
    racelog.deviation._load_playbook = lambda: None
    racelog.start(name="Race", kind="race")
    ds.race["since"] = NOW - race_h_ago * 3600.0
    ds.rows[-1]["start_ts"] = ds.race["since"]
    racelog._race_check.update(t=0.0, since=None, still_h=0.0)
    racelog._last_check[0] = 0.0
    return ds


from app import racegate  # noqa: E402

print("1d) #241: racing is its own state; the crew ends it")
ds = racing_ds(parked(underway_h=20, still_h=19), race_h_ago=30)
check("a race LOG start starts racing", racegate.racing() and ds.race["racing"] is True)
r = racelog.autoclose(now=NOW)
check("the recording session auto-closes (retention) ...", r["closed"] and ds.session_current() is None)
check("... but racing does NOT end with it (shore loading stays closed)", racegate.racing())
racelog.end()
check("LOG end doesn't end racing either", racegate.racing())
r = racelog.race_over(since=ds.race["since"] - 1)
check("Race over for another race start: 'Already ended'", r == {"ok": False, "note": "Already ended"}
      and racegate.racing())
r = racelog.race_over(since=ds.race["since"])
check("Race over ends racing", r["ok"] and not racegate.racing() and ds.race["ended_by"] == "crew")
check("Race over twice: 'Not racing'", racelog.race_over() == {"ok": False, "note": "Not racing"})
ds = racing_ds([], race_h_ago=1)
check("abandoned race (no movement record at all): Race over still ends it",
      racelog.race_over()["ok"] and not racegate.racing())
ds = racing_ds([], race_h_ago=1)
ds.race_state = lambda: (_ for _ in ()).throw(OSError("db locked"))
check("racing state unreadable -> racing (fail closed), in the gate and on the iPad",
      racegate.racing() and racelog.race_status(now=NOW)["racing"] is True)

print("1e) #241: 'Race over?' is only a suggestion; 24 h of unbroken stillness is the fallback")
ds = racing_ds(parked(underway_h=3, still_h=2), race_h_ago=6)
st = racelog.race_status(now=NOW)
check("stopped 2 h -> the iPad asks 'Race over?', still racing",
      st["racing"] and st["suggest_over"] and racegate.racing())
ds = racing_ds(parked(underway_h=1, still_h=0.5), race_h_ago=6)
st = racelog.race_status(now=NOW)
check("stopped 30 min -> no question yet", st["racing"] and not st["suggest_over"])
calm = [(4 + m / 60.0, 5.0) for m in range(60, 0, -1)]
calm += [(4 - m / 60.0, 0.1) for m in range(30)] + [(0.5 - m / 60.0, 0.1) for m in range(30)]
ds = racing_ds(calm, race_h_ago=6)
st = racelog.race_status(now=NOW)
check("30 min stopped, a 3 h hole, 30 min stopped -> no question (the hole doesn't count)",
      st["racing"] and not st["suggest_over"] and st["still_h"] < 1)
ds = racing_ds([(h, 0.1) for h in (3.0, 2.5, 2.0)], race_h_ago=6)
st = racelog.race_status(now=NOW)
check("boat powered down (no recent readings) -> no question, still racing",
      st["racing"] and not st["suggest_over"])
ds = racing_ds(parked(underway_h=26, still_h=25), race_h_ago=30)
st = racelog.race_status(now=NOW)
check("stopped 25 h unbroken -> the boat ends racing itself, and says so",
      st["racing"] is False and st["ended_by"] == "auto" and not racegate.racing())
ds = racing_ds(parked(underway_h=26, still_h=25), race_h_ago=30)
ds.save_race_state = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
st = racelog.race_status(now=NOW)
check("24 h fallback can't write: no error, still racing", st["racing"] is True)
del ds.save_race_state                                  # storage back
ds.plan = [(m / 60.0, 6.0) for m in range(60, 0, -1)]   # and the boat moving again
st = racelog.race_status(now=NOW + 1)
check("…the next poll checks afresh: moving, so still racing", st["racing"] and racegate.racing())
long_hole = [(31 + m / 60.0, 6.0) for m in range(60, 0, -1)]
long_hole += [(30 - m / 60.0, 0.1) for m in range(14 * 60)]
long_hole += [(15 - m / 60.0, 0.1) for m in range(15 * 60)]     # 1 h hole at 16..15 h ago
ds = racing_ds(long_hole, race_h_ago=32)
st = racelog.race_status(now=NOW)
check("30 h 'stopped' with a 1 h hole -> NOT ended (15 h unbroken), only asked",
      st["racing"] and st["suggest_over"] and racegate.racing())
ds = racing_ds(parked(underway_h=40, still_h=39), race_h_ago=10)
st = racelog.race_status(now=NOW)
check("stillness before the gun doesn't count (raced 10 h, all stopped): asked, not ended",
      st["racing"] and 9 < st["still_h"] <= 10.1)
ds = racing_ds(parked(underway_h=26, still_h=25), race_h_ago=30)
racelog.RACE_OVER_FALLBACK_H = 0.0
st = racelog.race_status(now=NOW)
check("RACE_OVER_FALLBACK_H=0 turns the fallback off", st["racing"] and racegate.racing())
racelog.RACE_OVER_FALLBACK_H = 24.0
ds = racing_ds(parked(underway_h=3, still_h=2), race_h_ago=6)
check("status() carries the racing state for the iPad",
      racelog.status()["race"]["suggest_over"] is True)

print("1f) #241: upgraded boat — an open race session with no racing state yet")
ds = RaceDS([])
racelog.datasource.active = lambda: ds
ds.session_start("Old race", None, "race", NOW - 3600)
check("reads as racing, and is written down", racegate.racing() and ds.race["racing"] is True)
racelog.end()
check("closing that session doesn't end racing", racegate.racing())
ds = RaceDS([])
racelog.datasource.active = lambda: ds
check("no session, no state: not racing", not racegate.racing())


def legacy(plan=()):
    """An upgraded boat: a race session open 30 h, no racing state written yet, nothing read."""
    ds = RaceDS(list(plan))
    racelog.datasource.active = lambda: ds
    ds.session_start("Old race", None, "race", NOW - 30 * 3600)
    racelog._last_check[0] = 0.0
    racelog._race_check.update(t=0.0, since=None, still_h=0.0)
    return ds


ds = legacy()
racelog.end()
check("LOG end before the first racing read: still racing", racegate.racing())
ds = legacy()
racelog.start(name="Sail", kind="practice")
check("practice start before the first racing read: still racing", racegate.racing())
ds = legacy()
ds.save_race_state = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
try:
    racelog.end()
except OSError:
    pass
check("racing state can't be written: LOG end does NOT close the race session",
      ds.session_current() is not None)
ds = legacy(parked(underway_h=20, still_h=13))
st = racelog.status()
check("auto-close on the first poll (13 h stopped): session closed, still racing",
      st["active"] is None and st["race"]["racing"] and racegate.racing())

print("2) archiver retention prune — sessions kept, day sails erased, fail-safe")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pi", "archiver"))
os.environ.setdefault("ARCHIVE_DB", os.path.join(tempfile.mkdtemp(), "archive.db"))
import archiver  # noqa: E402


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


tmp = tempfile.mkdtemp()
arc = sqlite3.connect(os.path.join(tmp, "a.db"), check_same_thread=False)
arc.executescript(archiver.SCHEMA)
eng = os.path.join(tmp, "engine.db")
ec = sqlite3.connect(eng)
ec.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, race_id TEXT, "
           "kind TEXT, start_ts REAL, end_ts REAL)")
now = time.time()
old_race = (now - 30 * 86400, now - 30 * 86400 + 7200)     # a race a month ago (2h)
ec.execute("INSERT INTO sessions (name, kind, start_ts, end_ts) VALUES ('old race','race',?,?)",
           old_race)
ec.commit()
rows = [
    (iso(old_race[0] + 600), "sr33", "s", "p", 1.0, None),      # inside the old race — KEEP
    (iso(now - 31 * 86400), "sr33", "s", "p", 2.0, None),       # old day sail — PRUNE
    (iso(now - 20 * 86400), "sr33", "s", "p", 3.0, None),       # old delivery — PRUNE
    (iso(now - 3600), "sr33", "s", "p", 4.0, None),             # recent (inside retention) — KEEP
]
arc.executemany("INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
arc.commit()
deleted = archiver.prune(arc, engine_db=eng, retain_days=14)
left = [r[0] for r in arc.execute("SELECT value FROM readings ORDER BY value")]
check("out-of-session old rows pruned (day sail + delivery)", deleted == 2 and left == [1.0, 4.0])
check("in-session row from a MONTH ago survives", 1.0 in left)
check("recent out-of-session row survives (inside retention)", 4.0 in left)
deleted = archiver.prune(arc, engine_db=os.path.join(tmp, "nope.db"), retain_days=14)
check("engine DB unreadable -> prune SKIPPED (never delete blind)", deleted is None)
check("retention 0 -> disabled", archiver.prune(arc, engine_db=eng, retain_days=0) is None)
# an OPEN session protects its window
ec.execute("INSERT INTO sessions (name, kind, start_ts, end_ts) VALUES ('live','race',?,NULL)",
           (now - 40 * 86400,))
ec.commit()
arc.execute("INSERT INTO readings (time, boat_id, source, path, value) VALUES (?,?,?,?,?)",
            (iso(now - 35 * 86400), "sr33", "s", "p", 5.0))
arc.commit()
deleted = archiver.prune(arc, engine_db=eng, retain_days=14)
check("an OPEN session's window is never pruned", deleted == 0 and
      5.0 in [r[0] for r in arc.execute("SELECT value FROM readings")])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
