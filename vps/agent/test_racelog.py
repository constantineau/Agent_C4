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

    def session_start(self, name, race_id, kind, start_ts):
        if self.session_current():
            self.session_end(start_ts)
        self.rows.append({"id": len(self.rows) + 1, "name": name, "race_id": race_id,
                          "kind": kind, "start_ts": start_ts, "end_ts": None})
        return self.session_current()

    def session_end(self, end_ts):
        for r in self.rows:
            if r["end_ts"] is None:
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
check("one-tap start with NOTHING (no playbook/name/race) works",
      r["ok"] and r["active"]["kind"] == "practice" and r["active"]["name"].startswith("Sail "))
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
    rows += [(h, 0.1) for h in [still_h * (1 - i / 200.0) for i in range(200)]]
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
      r["closed"] is False and "quiet bus" in r["note"] and ds.session_current() is not None)

ds = fresh(MotionDS([(h / 10.0, 0.1) for h in range(1, 400)]))  # plenty of samples, never moved
r = racelog.autoclose(now=NOW)
check("no under-way moment anywhere in the look-back -> refuses, and says a human must close it",
      r["closed"] is False and "not closing on a guess" in r["note"])

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

racelog.AUTOCLOSE_IDLE_H = 0.0
ds = fresh(MotionDS(parked(underway_h=20, still_h=19)))
check("RACELOG_AUTOCLOSE_IDLE_H=0 disables it entirely",
      racelog.autoclose(now=NOW)["closed"] is False and ds.session_current() is not None)
racelog.AUTOCLOSE_IDLE_H = 12.0

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
