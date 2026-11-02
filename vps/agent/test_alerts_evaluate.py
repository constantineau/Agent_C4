"""Crew alerts through evaluate(): raise slow, clear fast, one row per alert (app/alerts.py).

test_alerts.py pins which conditions fire; test_alerts_race_mode.py pins the RRS 41 gate. This one
drives the debounce clock and the alerts-table writes over successive ticks, with a fake Postgres
that keeps the rows and a fake monotonic clock:

  * a condition must hold for its whole debounce window before it raises (depth 10 s, AIS 30 s);
  * a blip resets the clock: true, false, true again starts the window over;
  * still firing with the same text: no new row, no push;
  * worse (warn → danger) or new text: the row is updated in place, pushed as "updated";
  * gone: cleared on the very next tick (no clear debounce), the row kept as history;
  * back again after a clear: debounced again from zero, and a NEW row;
  * after a restart, an alert still on record whose condition still holds is neither cleared nor
    raised twice while its debounce re-accumulates;
  * a rule that throws does not clear the other rules' alerts.

No DB, engine or network. Run (make ci):  PYTHONPATH=vps/agent:. python3 vps/agent/test_alerts_evaluate.py
"""
import itertools
import time
import types

from app import alerts, race_mode, tools

race_mode.racing = lambda: False         # practice: every rule may fire (race mode: other file)

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- a fake Postgres that keeps the alerts table ---------------------------------------------
class DB:
    def __init__(self):
        self.rows, self.ids = [], itertools.count(1)

    def active(self):
        return [r for r in self.rows if r["cleared_at"] is None]


class Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class Conn:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self

    def commit(self):
        pass

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT"):
            return Result([dict(r, raised_epoch=r["raised_at"]) for r in self.db.active()])
        if s.startswith("INSERT INTO alerts"):
            _boat, key, kind, sev, msg = params
            self.db.rows.append({"id": next(self.db.ids), "key": key, "kind": kind, "severity": sev,
                                 "message": msg, "raised_at": time.time(), "cleared_at": None})
        elif s.startswith("UPDATE alerts SET severity"):
            sev, msg, rid = params
            r = next(r for r in self.db.rows if r["id"] == rid)
            r.update(severity=sev, message=msg)
        elif s.startswith("UPDATE alerts SET cleared_at"):
            (rid,) = params
            next(r for r in self.db.rows if r["id"] == rid)["cleared_at"] = time.time()
        else:
            raise AssertionError(f"unexpected SQL: {s}")
        return Result([])


class Pool:
    def __init__(self, db):
        self.db = db

    def connection(self):
        return Conn(self.db)


CLOCK = {"t": 1000.0}
alerts.time = types.SimpleNamespace(monotonic=lambda: CLOCK["t"], time=time.time)


def reset():
    db = DB()
    alerts.pool = Pool(db)
    alerts._pending.clear()
    CLOCK["t"] = 1000.0
    return db


# --- the world: stub the tool reads -------------------------------------------------------
WORLD = {}


def world(depth=30.0, ais=(), strip_boom=False):
    WORLD.update(depth=depth, ais=list(ais), strip_boom=strip_boom)


def _strip():
    if WORLD["strip_boom"]:
        raise RuntimeError("engine down")
    return {"available": True, "data_age_seconds": 2, "depth": WORLD["depth"],
            "stw": 6.0, "tws": 12.0, "twa": 45.0}


tools.get_strip = _strip
tools.get_ais_targets = lambda *a, **k: {"own_fix": True, "targets": WORLD["ais"]}
tools.get_polar_target = lambda *a, **k: {"target_stw": 6.2}
tools.get_tactics = lambda *a, **k: {"available": True, "wind": {"persistent": False}}
tools.get_fatigue = lambda *a, **k: {"available": True, "level": "ok", "index": 10}

SHIP = {"mmsi": "366000001", "name": "LAKER", "closing": True, "cpa_nm": 0.8, "tcpa_min": 15,
        "bearing": 270}


def tick(at):
    CLOCK["t"] = 1000.0 + at
    return [(c["event"], c["alert"]["key"], c["alert"]["severity"]) for c in alerts.evaluate()]


check("debounce windows are what this test assumes (depth 10 s, AIS 30 s)",
      alerts.DEBOUNCE_S["depth_shoaling"] == 10 and alerts.DEBOUNCE_S["ais"] == 30)

print("raise slow — the whole debounce window first:")
db = reset()
world(depth=6.0)                                  # warn band (≤ 8 m)
check("t=0: shallow seen, nothing raised", tick(0) == [] and db.rows == [])
check("t=9: still inside the 10 s window", tick(9) == [] and db.rows == [])
check("t=10: raised as new, warn", tick(10) == [("new", "depth_shoaling", "warn")])
check("…one row in the table", len(db.active()) == 1 and db.active()[0]["message"] ==
      "Getting shallow: 6.0 m.")
check("t=20: same condition, same text → no push, no second row",
      tick(20) == [] and len(db.rows) == 1)

print("worse — updated in place:")
world(depth=3.0)
check("t=21: 3 m → updated to danger", tick(21) == [("updated", "depth_shoaling", "danger")])
check("…the same row, new severity and text", len(db.rows) == 1 and
      db.rows[0]["severity"] == "danger" and db.rows[0]["message"] == "Shallow: 3.0 m.")

print("clear fast — on the next tick, history kept:")
world(depth=30.0)
check("t=22: deep again → cleared at once", tick(22) == [("cleared", "depth_shoaling", "danger")])
check("…row kept as history, not active", len(db.rows) == 1 and db.active() == []
      and db.rows[0]["cleared_at"] is not None)
check("t=23: nothing more to clear", tick(23) == [])

print("back again — debounced from zero, a new row:")
world(depth=6.0)
check("t=30: shallow again, not raised yet", tick(30) == [])
check("t=39: still inside the window", tick(39) == [])
check("t=40: raised, as a second row", tick(40) == [("new", "depth_shoaling", "warn")]
      and len(db.rows) == 2 and len(db.active()) == 1)

print("a blip resets the clock (AIS, 30 s):")
db = reset()
world(ais=[SHIP])
tick(0)
check("t=25: closing for 25 s, nothing yet", tick(25) == [])
world(ais=[])
check("t=26: target gone for one tick", tick(26) == [])
world(ais=[SHIP])
check("t=27: back — the window starts over", tick(27) == [])
check("t=56: 29 s since it came back: still nothing", tick(56) == [])
check("t=57: 30 s continuous → raised", tick(57) == [("new", "ais:366000001", "warn")])
check("…with the crew's text", db.active()[0]["message"].startswith("LAKER closing: passes 0.8 nm"))

print("two alerts at once: each on its own clock, each cleared on its own:")
world(ais=[SHIP], depth=6.0)
check("t=60: depth starts its window; AIS unchanged", tick(60) == [])
check("t=70: depth raised, AIS untouched", tick(70) == [("new", "depth_shoaling", "warn")])
world(ais=[], depth=6.0)
check("t=71: only AIS clears", tick(71) == [("cleared", "ais:366000001", "warn")]
      and [r["key"] for r in db.active()] == ["depth_shoaling"])

print("a rule that throws clears its own alerts, not the others':")
db = reset()
world(ais=[SHIP], depth=6.0)
tick(0)
tick(30)
check("both raised", {r["key"] for r in db.active()} == {"ais:366000001", "depth_shoaling"})
world(ais=[SHIP], strip_boom=True)
out = tick(31)
check("strip throwing: AIS stays up", "ais:366000001" in {r["key"] for r in db.active()}
      and not any(k == "ais:366000001" for _e, k, _s in out))

print("after a restart — an alert on record whose condition still holds:")
db = reset()
world(depth=6.0)
tick(0)
tick(10)
alerts._pending.clear()                           # the process restarted; the table did not
CLOCK["t"] = 5000.0
check("first tick after restart: not cleared, not raised again", tick(4000) == []
      and len(db.rows) == 1 and len(db.active()) == 1)
check("…and still not duplicated once the window re-accumulates", tick(4010) == []
      and len(db.rows) == 1)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
